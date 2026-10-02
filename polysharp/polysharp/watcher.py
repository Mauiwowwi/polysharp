"""Real-time detection of tracked-wallet trades.

Two feeds, deduped against each other:
  1. RTDS websocket firehose (topic activity/trades) -> filtered to tracked wallets.
     Sub-second latency, includes proxyWallet on every fill.
  2. REST /activity polling per wallet -> backstop for websocket gaps / disconnects.
     Runs fast when the websocket is down, slow when it's healthy.

Fills on the same wallet/outcome/side are bundled for BUNDLE_SECONDS so an order
that sweeps the book produces one alert, not twenty.
"""
import asyncio
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import re
import logging
import time

import websockets

from .fees import analyze_fill, summarize
from .markets import is_live, sport_of
from .telegram import esc

log = logging.getLogger(__name__)


def normalize(raw, source):
    """Map a WS payload or REST activity row onto one schema."""
    size = float(raw.get("size") or 0)
    price = float(raw.get("price") or 0)
    usdc = raw.get("usdcSize")
    usdc = float(usdc) if usdc not in (None, "") else None
    ts = int(raw.get("timestamp") or time.time())
    if ts > 1e12:  # ms -> s
        ts //= 1000
    return {
        "wallet": (raw.get("proxyWallet") or "").lower(),
        "ts": ts, "asset": str(raw.get("asset") or ""),
        "condition_id": raw.get("conditionId") or "",
        "outcome": raw.get("outcome") or "", "title": raw.get("title") or "",
        "slug": raw.get("slug") or "", "event_slug": raw.get("eventSlug") or "",
        "side": (raw.get("side") or "").upper(), "size": size, "price": price,
        "usdc_raw": usdc, "usd": usdc if usdc is not None else size * price,
        "tx": raw.get("transactionHash") or "", "source": source,
        "fee_info": analyze_fill(size, price, usdc, (raw.get("side") or "").upper()),
    }


_SPREAD = re.compile(r"Spread:\s*(.+?)\s*\(([-+]?\d+(?:\.\d+)?)\)", re.I)
_TOTAL = re.compile(r"O/U\s*(\d+(?:\.\d+)?)", re.I)


def is_paid(fee):
    """True when at least half the bundle crossed the spread (they paid fees to get filled now)."""
    return bool(fee) and fee.get("coverage", 1) >= 0.5 and fee.get("taker_share", 0) >= 0.5


def pick_label(title, outcome, event=None):
    """The side the bettor actually holds, with its line.

    event: the game's title ("Norway vs. Wales") -> "Will Norway win?" + No
    becomes "Norway NO (playing Wales)".

    "Spread: Browns (-3.5)" + Steelers -> "Steelers +3.5"; + Browns -> "Browns -3.5"
    "...: O/U 39.5" + Under -> "Under 39.5"; moneyline / other -> outcome as-is.
    """
    title, outcome = title or "", (outcome or "").strip()
    m = _SPREAD.search(title)
    if m:
        team, line = m.group(1).strip(), float(m.group(2))
        if outcome.lower() != team.lower() and outcome.lower() not in team.lower() \
                and team.lower() not in outcome.lower():
            line = -line
        return f"{outcome} {line:+g}"
    m = _TOTAL.search(title)
    if m and outcome.lower() in ("over", "under"):
        return f"{outcome} {m.group(1)}"
    if outcome.lower() in ("yes", "no"):
        label = yes_no_label(title, outcome.lower() == "yes")
        opp = opponent_of(title, event)
        return f"{label} (playing {opp})" if opp else label
    return outcome


_VS = re.compile(r"\s+vs\.?\s+", re.I)


def opponent_of(title, event):
    """'Will Norway win…?' + event 'Norway vs. Wales' -> 'Wales' (None if unclear)."""
    m = _WIN.match((title or "").strip())
    if not m or not event:
        return None
    ev = re.sub(r"\s*-\s*(More Markets|Winner|Moneyline).*$", "", str(event), flags=re.I).strip()
    teams = [x.strip(" ?") for x in _VS.split(ev)]
    if len(teams) != 2 or not all(teams):
        return None
    subj = m.group(1).strip().lower()
    for x, y in ((teams[0], teams[1]), (teams[1], teams[0])):
        if x.lower() == subj and y.lower() != subj:
            return y

    def same(a):
        a = a.lower()
        return a == subj or a in subj or subj in a
    a, b = teams
    if same(a) and not same(b):
        return b
    if same(b) and not same(a):
        return a
    return None


_WIN = re.compile(r"^Will (.+?) win(?: on \d{4}-\d{2}-\d{2})?\s*\??$", re.I)
_DRAW = re.compile(r"^Will (.+?) vs\.? (.+?) end in a draw\s*\??$", re.I)
_EXACT = re.compile(r"^Exact Score:\s*(.+?)\s*\??$", re.I)
_BTTS = re.compile(r"both teams to score", re.I)
_WILL = re.compile(r"^Will (.+?)\s*\??$", re.I)


def yes_no_label(title, yes):
    """Yes/No soccer-style markets as '<subject> YES|NO'.

    Will Azerbaijan win on …?      -> "Azerbaijan YES" / "Azerbaijan NO"
    Will A vs. B end in a draw?    -> "Draw YES (A vs B)" / "Draw NO (A vs B)"
    Exact Score: A 1 - 1 B?        -> "A 1-1 B YES" / "… NO"
    …Both Teams to Score           -> "BTTS YES" / "BTTS NO"
    anything else                  -> "<question> YES" / "… NO"
    """
    side = "YES" if yes else "NO"
    t = (title or "").strip()
    m = _DRAW.match(t)
    if m:
        return f"Draw {side} ({m.group(1).strip()} vs {m.group(2).strip()})"
    m = _WIN.match(t)
    if m:
        return f"{m.group(1).strip()} {side}"
    m = _EXACT.match(t)
    if m:
        score = re.sub(r"\s*-\s*", "-", m.group(1).strip())
        return f"{score} {side}"
    if _BTTS.search(t):
        return f"BTTS {side}"
    m = _WILL.match(t)
    q = m.group(1) if m else t.rstrip("?")
    return f"{q} {side}"
# --- same game, different market (alt lines, ML vs spread, totals) -------------
_GAME = re.compile(r"^([a-z0-9]+-[a-z0-9]+-[a-z0-9]+-\d{4}-\d{2}-\d{2})")
_LINE = re.compile(r"^(.+?)\s+([-+]\d+(?:\.\d+)?)$")


def game_key(slug):
    """'cfb-stan-wake-2026-10-03' (also strips '-more-markets' etc.)."""
    slug = (slug or "").lower()
    m = _GAME.match(slug)
    return m.group(1) if m else slug


def lean(title, outcome):
    """What a position roots for: ('team', scope, name, line) | ('total', scope, 'over'|'under', line).

    Spread/ML/'Will X win' YES -> team; O/U -> total. scope separates 1st-half markets
    from full-game ones. Anything else (draw, BTTS, exact score, soccer NO) -> None."""
    title, outcome = title or "", (outcome or "").strip()
    scope = "1h" if re.search(r"1st half|first half|1h\b", title, re.I) else "fg"
    label = pick_label(title, outcome)
    if _TOTAL.search(title) and outcome.lower() in ("over", "under"):
        return ("total", scope, outcome.lower(), float(_TOTAL.search(title).group(1)))
    if _SPREAD.search(title):
        m = _LINE.match(label)
        if m:
            return ("team", scope, m.group(1).strip().lower(), float(m.group(2)))
        return None
    if outcome.lower() == "yes":
        m = _WIN.match(title.strip())
        return ("team", scope, m.group(1).strip().lower(), 0.0) if m else None
    if outcome.lower() == "no":
        return None
    if " vs" in title.lower() and ":" not in title:      # moneyline: outcome is the team
        return ("team", scope, outcome.lower(), 0.0)
    return None


def relate(new, old, new_name=None, old_name=None):
    """How an older position relates to the new bet.

    Returns (kind, note): kind 'same' (same side, other line), 'middle' (both can win),
    'gap' (both can lose) or 'opposite' (exact other side); None if unrelated
    (different market type or half). note: the window in words, e.g. 'Wake Forest by 14–16'."""
    if not new or not old or new[0] != old[0] or new[1] != old[1]:
        return None
    if new[0] == "team":
        _, _, a_team, a = new
        _, _, b_team, b = old
        if a_team == b_team:
            return ("same", None)
        # m = new team's margin. new covers if m > -a; old covers if -m + b > 0 -> m < b
        lo, hi = -a, b
        if lo == hi:
            return ("opposite", None)
        kind = "middle" if lo < hi else "gap"
        x, y = min(lo, hi), max(lo, hi)
        names = (new_name or a_team.title(), old_name or b_team.title())
        return (kind, _margin_window(names, x, y))
    _, _, a_side, a = new
    _, _, b_side, b = old
    if a_side == b_side:
        return ("same", None)
    over, under = (a, b) if a_side == "over" else (b, a)
    if over == under:
        return ("opposite", None)
    kind = "middle" if over < under else "gap"
    w = _int_window(min(over, under), max(over, under))
    return (kind, f"total lands {w}" if w else None)


def _team_name(label):
    """'Stanford +16.5' -> 'Stanford'; 'Wake Forest' -> 'Wake Forest'."""
    m = _LINE.match(label or "")
    return m.group(1).strip() if m else (label or "").replace(" YES", "").strip()


def _int_window(x, y):
    """Whole numbers strictly between x and y as '14–16' / '15' (None if none)."""
    import math
    a, b = math.floor(x) + 1, math.ceil(y) - 1
    if a > b:
        return None
    return f"{a}" if a == b else f"{a}–{b}"


def _margin_window(names, x, y):
    """Margins strictly between x and y (from the first team's view), said from the winner's side."""
    new_team, old_team = names
    if x >= 0:
        w = _int_window(x, y)
        return f"{new_team} by {w}" if w else None
    if y <= 0:
        w = _int_window(-y, -x)
        return f"{old_team} by {w}" if w else None
    return f"{new_team} by up to {_int_window(0, y) or 0} or {old_team} by up to {_int_window(0, -x) or 0}"


def short_pick(title, outcome):
    """Pick label for the compact tab format: totals keep the matchup, moneylines say ML."""
    title, outcome = title or "", (outcome or "").strip()
    label = pick_label(title, outcome)
    if _SPREAD.search(title) or outcome.lower() in ("yes", "no"):
        return label
    if _TOTAL.search(title) and outcome.lower() in ("over", "under"):
        matchup = title.split(":")[0].strip()
        return f"{matchup} — {label}" if matchup and matchup != title else label
    if " vs" in title.lower():
        return f"{label} ML"
    return label


def american(p):
    """Polymarket price -> American odds string (0.71 -> -245, 0.40 -> +150)."""
    if not 0 < p < 1:
        return "n/a"
    if p >= 0.5:
        return f"-{round(100 * p / (1 - p))}"
    return f"+{round(100 * (1 - p) / p)}"


class Watcher:
    def __init__(self, cfg, api, store, tg, markets=None):
        self.cfg, self.api, self.store, self.tg = cfg, api, store, tg
        self.markets = markets
        self.skipped_filtered = 0
        self.below_tier = 0
        self.wallets = {}          # address -> {name, source, stats}
        self.bundles = {}          # key -> {"fills": [...], "task": Task}
        self.last_poll_ts = {}     # address -> last seen activity ts
        self.ws_connected = False
        self.ws_last_msg = 0.0
        self.ws_msgs = 0
        self.alerts_sent = 0
        self.started = time.time()

    def reload_wallets(self):
        self.wallets = self.store.active_wallets()
        now = int(time.time())
        for a in self.wallets:
            self.last_poll_ts.setdefault(a, now)  # no backfill spam on startup/add
        log.info("Tracking %d wallets", len(self.wallets))

    @property
    def ws_healthy(self):
        return self.ws_connected and time.time() - self.ws_last_msg < 30

    # ------------------------------------------------------------------ intake
    async def ingest(self, t):
        if t["wallet"] not in self.wallets or t["side"] not in ("BUY", "SELL"):
            return
        base = f"{t['tx']}|{t['asset']}|{t['side']}"
        if t["source"] == "rest" and self.store.has_seen("ws-guard|" + base):
            # WS already delivered this tx -> skip to avoid double counting
            return
        if t["source"] == "ws":
            self.store.mark_seen("ws-guard|" + base)
        if not self.store.mark_seen(f"{t['source']}|{base}|{round(t['size'], 2)}"):
            return

        key = f"{t['wallet']}|{t['asset']}|{t['side']}"
        b = self.bundles.get(key)
        if b is None:
            b = self.bundles[key] = {"fills": []}
            b["task"] = asyncio.create_task(self._flush_later(key))
        b["fills"].append(t)

    async def _flush_later(self, key):
        await asyncio.sleep(self.cfg.bundle_seconds)
        b = self.bundles.pop(key, None)
        if b:
            try:
                await self.on_bundle(b["fills"])
            except Exception:
                log.exception("alert failed")

    # ------------------------------------------------------------------ alerts
    def min_usd(self):
        return float(self.store.get("min_alert_usd", self.cfg.min_alert_usd))

    def muted(self):
        return time.time() < float(self.store.get("muted_until", 0))

    def live_hedges_on(self):
        return bool(self.store.get("live_hedges", self.cfg.live_hedge_alerts))

    async def _meta(self, t):
        if self.markets is None:
            return None
        try:
            return (await self.markets.get(
                {t["condition_id"]: t["event_slug"] or t["slug"]}))[t["condition_id"]]
        except Exception as e:
            log.debug("market meta failed: %s", e)
            return None

    async def on_bundle(self, fills):
        f0 = fills[0]
        shares = sum(f["size"] for f in fills)
        notional = sum(f["size"] * f["price"] for f in fills)
        vwap = notional / shares if shares else f0["price"]
        side, wallet, cond = f0["side"], f0["wallet"], f0["condition_id"]

        if not cond or not f0["asset"] or not (f0["title"] or "").strip():
            # combo/parlay or malformed fill: no single market to label, filter or check
            self.skipped_filtered += 1
            log.info("skipping fill with no market (combo?) from %s", wallet)
            return
        meta = await self._meta(f0)
        live = bool(meta) and is_live(meta, f0["ts"])
        if self.cfg.sports_only_alerts and self.markets is not None and \
                (meta is None or not meta.get("sports")):
            self.skipped_filtered += 1
            return
        tailed = self.store.was_alerted(wallet, cond)
        if live and self.cfg.pregame_only_alerts and not (tailed and self.live_hedges_on()):
            # In-game fills are dropped. Only exception: a SELL or hedge on a position
            # we already alerted you on pre-game (checked again below for hedge-buys).
            self.skipped_filtered += 1
            return

        if not live:
            # index pre-game sports fills (any size) so agree/oppose can find this wallet
            for f in fills:
                self.store.record_trade(f)

        if notional < self.min_usd():
            return
        if side == "SELL" and not self.cfg.alert_sells:
            return
        if side == "BUY" and not (self.cfg.min_price <= vwap <= self.cfg.max_price):
            return

        book = await self._wallet_book(wallet, cond)          # this wallet's holdings in the market
        pos = book.get(f0["asset"]) if book is not None else None
        hedge_vs, flip_vs = self.classify_other_side(book, f0["asset"], pos, sum(f["usd"] for f in fills))
        if side != "BUY":
            hedge_vs, flip_vs = [], []
        is_hedge = bool(hedge_vs)
        if live and self.cfg.pregame_only_alerts and side == "BUY" and not is_hedge:
            self.skipped_filtered += 1
            return

        await self._enrich_fees(fills)
        fee = summarize(fills)
        conviction = self._is_conviction(wallet, fee) and not is_hedge
        if self.store.get("taker_only", False) and side == "BUY" and not is_hedge \
                and not (fee and fee["taker_share"] >= 0.5):
            return

        key = self._game_of(f0, meta)
        also_vs, cross_vs = (self._related(f0, await self._game_book(wallet, key, exclude_cond=cond))
                             if side == "BUY" else ([], []))
        agree, oppose = await self._crowd(f0, key) if side == "BUY" and not is_hedge else ([], [])
        usd = sum(f["usd"] for f in fills)
        score = None
        if side == "BUY" and not is_hedge:
            score = self.conviction_score(f0, usd, pos, conviction, agree, oppose, paid=is_paid(fee))
            if self.tier_rank(score["tier"]) < self.tier_rank(self.store.get("min_tier", "all")):
                self.below_tier += 1
                if self.cfg.consensus_alert_wallets <= len(agree) + 1:
                    await self._consensus(f0, agree, sport_of((meta or {}).get("league")))
                self.store.mark_alerted(wallet, cond, f0["asset"])
                return
        text = self.format_alert(f0, fills, usd, shares, vwap, pos, fee, conviction, meta,
                                 hedge_vs=hedge_vs, flip_vs=flip_vs, agree=agree, oppose=oppose,
                                 tailed=tailed, live=live, score=score, also_vs=also_vs, cross_vs=cross_vs)
        sport = sport_of((meta or {}).get("league"))
        short = (self.format_short(f0, usd, vwap, pos, fee, hedge_vs, flip_vs, live)
                 if self.cfg.compact_tabs else None)
        if not self.muted():
            await self.tg.send_alert(text, sport, short)
            self.alerts_sent += 1
        if side == "BUY" and not is_hedge:
            self.store.mark_alerted(wallet, cond, f0["asset"])
            await self._consensus(f0, agree, sport)

    async def _enrich_fees(self, fills):
        """WS fills lack usdcSize: look them up on /activity by tx hash."""
        for attempt in range(2):
            missing = [f for f in fills if f["fee_info"] is None]
            if not missing:
                return
            if attempt:
                if self.cfg.fee_retry_seconds <= 0:
                    return
                await asyncio.sleep(self.cfg.fee_retry_seconds)  # /activity can lag a few s
            f0 = missing[0]
            try:
                rows = await self.api.activity(
                    f0["wallet"], start=min(f["ts"] for f in missing) - 10, limit=200)
            except Exception as e:
                log.debug("fee enrich failed: %s", e)
                return
            by_key = {}
            for r in rows:
                k = (r.get("transactionHash"), str(r.get("asset")), (r.get("side") or "").upper())
                by_key.setdefault(k, []).append(r)
            for f in missing:
                cands = by_key.get((f["tx"], f["asset"], f["side"])) or []
                if not cands:
                    continue
                r = min(cands, key=lambda r: abs(float(r.get("size") or 0) - f["size"]))
                try:
                    usdc = float(r.get("usdcSize"))
                except (TypeError, ValueError):
                    continue
                f["usdc_raw"] = usdc
                f["usd"] = usdc
                f["fee_info"] = analyze_fill(float(r.get("size") or f["size"]),
                                             float(r.get("price") or f["price"]), usdc, f["side"])

    def _is_conviction(self, addr, fee):
        """Paid to take liquidity AND that's out of character for this wallet."""
        if not fee or fee["coverage"] < 0.5 or fee["taker_share"] < self.cfg.conviction_min_taker:
            return False
        base = (self.wallets.get(addr, {}).get("stats") or {}).get("taker_share")
        return base is not None and base <= self.cfg.conviction_max_baseline

    async def _wallet_book(self, wallet, cond):
        """{asset: {size, avg, cost, outcome}} for one wallet in one market (None on error)."""
        if not cond:
            return None
        try:
            rows = await self.api.positions(wallet, market=cond)
        except Exception:
            return None
        rows = [r for r in rows if not r.get("conditionId") or r.get("conditionId") == cond]
        if len(rows) > 3:          # a binary market has 2 outcomes; anything bigger is junk
            log.warning("position lookup for %s returned %d rows; ignoring", cond, len(rows))
            return None
        out = {}
        for r in rows:
            out[str(r.get("asset"))] = {
                "size": float(r.get("size") or 0), "avg": float(r.get("avgPrice") or 0),
                "cost": float(r.get("initialValue") or 0), "outcome": r.get("outcome") or ""}
        return out

    def classify_other_side(self, book, asset, pos, usd):
        """Split the wallet's holdings on the OTHER outcome into (hedge_vs, flip_vs).

        dust  (< HEDGE_DUST_PCT of this side, or < $50): ignored -> plain NEW/ADD
        other side >= this side:                         HEDGE (protecting it)
        other side meaningful but now smaller:           BOTH SIDES (now bigger on the new side)
        """
        other = [p for a, p in (book or {}).items() if a != asset and p["size"] >= 1]
        this_cost = (pos or {}).get("cost") or usd
        opp_cost = sum(p["cost"] for p in other)
        if not other or opp_cost < max(50.0, self.cfg.hedge_dust_pct * this_cost):
            return [], []
        if this_cost <= opp_cost:
            return other, []
        return [], other

    async def _game_book(self, wallet, key, exclude_cond=None):
        """This wallet's open positions in OTHER markets of the same game (alt lines, ML, totals)."""
        if not key:
            return []
        rows = []
        try:
            for page in range(3):              # API pages at 100 rows
                kw = {"offset": page * 100} if page else {}
                got = await self.api.positions(wallet, redeemable=False, **kw)
                rows += got or []
                if len(got or []) < 100:
                    break
        except Exception:
            return []
        out = []
        for r in rows:
            cond = r.get("conditionId") or ""
            if cond == exclude_cond or game_key(r.get("eventSlug") or r.get("slug")) != key:
                continue
            size = float(r.get("size") or 0)
            if size < 1 or r.get("redeemable"):
                continue
            out.append({"cond": cond, "asset": str(r.get("asset")), "title": r.get("title") or "",
                        "outcome": r.get("outcome") or "", "size": size,
                        "avg": float(r.get("avgPrice") or 0), "cost": float(r.get("initialValue") or 0)})
        return out

    def _related(self, t, rows):
        """Split same-game positions into same-side ('also') and crossing ('cross') lists."""
        new = lean(t["title"], t["outcome"])
        new_team = pick_label(t["title"], t["outcome"])
        also, cross = [], []
        for p in rows:
            if p["cost"] < self.cfg.crowd_min_usd:
                continue
            old = lean(p["title"], p["outcome"])
            rel = relate(new, old, _team_name(new_team), _team_name(pick_label(p["title"], p["outcome"])))
            if not rel:
                continue
            row = {**p, "rel": rel[0], "note": rel[1]}
            (also if rel[0] == "same" else cross).append(row)
        also.sort(key=lambda x: -x["cost"])
        cross.sort(key=lambda x: -x["cost"])
        return also, cross

    def _game_of(self, t, meta):
        return game_key(t.get("event_slug") or (meta or {}).get("slug") or t.get("slug"))

    async def _crowd(self, t, key=None):
        """Other tracked wallets currently holding either side of this market.

        Checks every tracked wallet's live holdings in this market (not just the ones
        the bot happened to see trade), so positions taken before a redeploy, or while
        the bot was down, still show up as 🤝 / ⚔️."""
        others = [w for w in self.wallets if w != t["wallet"]]
        seen = set(self.store.wallets_in_market(t["condition_id"], time.time() - self.cfg.crowd_days * 86400))
        others.sort(key=lambda w: w not in seen)          # recently-seen first if we must cap
        others = others[:60]
        sem = asyncio.Semaphore(8)

        async def book(w):
            async with sem:
                return await self._wallet_book(w, t["condition_id"])
        books = await asyncio.gather(*(book(w) for w in others))
        agree, oppose = [], []
        floor = self.cfg.crowd_min_usd
        for w, bk in zip(others, books):
            for asset, p in (bk or {}).items():
                if p["size"] < 1 or p["cost"] < floor:
                    continue
                row = {"wallet": w, **p}
                (agree if asset == t["asset"] else oppose).append(row)
        if key:                                  # same game, other markets (alt lines, ML, totals)
            async def gbook(w):
                async with sem:
                    return await self._game_book(w, key, exclude_cond=t["condition_id"])
            gbooks = await asyncio.gather(*(gbook(w) for w in others))
            for w, rows in zip(others, gbooks):
                also, cross = self._related(t, rows)
                agree += [{"wallet": w, **p} for p in also]
                oppose += [{"wallet": w, **p} for p in cross]
        agree.sort(key=lambda x: -x["cost"])
        oppose.sort(key=lambda x: -x["cost"])
        return agree, oppose

    @staticmethod
    def tier_rank(tier):
        return {"all": 0, "low": 0, "LOW": 0, "med": 1, "MED": 1, "high": 2, "HIGH": 2}.get(tier, 0)

    def conviction_score(self, t, usd, pos, paid_up, agree, oppose, paid=False):
        """Points for how hard this wallet is leaning in, and what the others are doing.

        size vs their normal bet  ≥3× +2 · ≥1.5× +1   ("volumed out")
        still buying              ≥2 separate buys on this side in 24h +1
        agreement                 +1 per tracked wallet on the same side (max +2)
        opposition                −2 if any tracked wallet holds the other side
        paid fees                 +1 (≥50% of the buy crossed the spread)
        out of character          +1 more if this wallet is normally a passive maker
        """
        stats = self.wallets.get(t["wallet"], {}).get("stats") or {}
        avg = stats.get("avg_bet") or 0
        exposure = pos["cost"] if pos and pos.get("cost") else usd
        mult = exposure / avg if avg else None
        bursts, total, first = self.store.buy_bursts(t["wallet"], t["asset"], time.time() - 86400)
        pts, why = 0, []
        if mult is not None:
            if mult >= 3:
                pts += 2
            elif mult >= 1.5:
                pts += 1
            why.append(f"{mult:.1f}× their usual bet")
        if bursts >= 2:
            pts += 1
            hrs = (time.time() - first) / 3600 if first else 0
            why.append(f"buy #{bursts} on this side in {hrs:.0f}h (${total:,.0f})")
        if agree:
            pts += min(len(agree), 2)
            why.append(f"{len(agree)} agree")
        if oppose:
            pts -= 2
            why.append(f"{len(oppose)} oppose")
        else:
            why.append("no opposition")
        if paid or paid_up:
            pts += 1
            why.append("paid fees")
        if paid_up:
            pts += 1
            why.append("out of character")
        tier = "HIGH" if pts >= self.cfg.tier_high else "MED" if pts >= self.cfg.tier_med else "LOW"
        return {"pts": pts, "tier": tier, "why": why, "mult": mult}

    def _name(self, addr):
        return esc(self.wallets.get(addr, {}).get("name") or addr[:8])

    def _wallet_line(self, addr):
        w = self.wallets.get(addr, {})
        s = w.get("stats") or {}
        bits = [f"👤 <a href=\"https://polymarket.com/profile/{addr}\">{self._name(addr)}</a>"]
        if "pnl_m" in s:
            def m(x):
                a = abs(x)
                v = f"${a / 1e6:,.2f}M" if a >= 1e6 else f"${a / 1e3:,.0f}K" if a >= 1e3 else f"${a:,.0f}"
                return ("+" if x >= 0 else "−") + v
            bits.append(f"sports 1M {m(s['pnl_m'])} · all {m(s.get('pnl_overall', s['pnl_all']))}")
            if s.get("avg_bet"):
                bits.append(f"avg bet ${s['avg_bet'] / 1e3:,.1f}K")
            if s.get("win_rate") is not None:
                bits.append(f"win {s['win_rate']:.0%}")
        return " · ".join(bits)

    def format_alert(self, t, fills, usd, shares, vwap, pos, fee=None, conviction=False, meta=None,
                     hedge_vs=(), agree=(), oppose=(), tailed=False, live=False, score=None,
                     flip_vs=(), also_vs=(), cross_vs=()):
        """Labelled 'TRADE ALERT!' layout: facts on top, analysis below a blank line."""
        buy = t["side"] == "BUY"
        pick = pick_label(t["title"], t["outcome"], (meta or {}).get("event_title"))
        tags = []
        if live:
            tags.append("🔴 LIVE 🔴")
        if buy:
            if is_paid(fee):
                tags.append("🚨 BUY TAKER 🚨")
            if conviction:
                tags.append("⚡ CONVICTION ⚡")
            if hedge_vs:
                tags.append("🛡️ HEDGE 🛡️")
            elif flip_vs:
                tags.append("⚖️ BOTH SIDES ⚖️")
            if any(c["rel"] == "middle" for c in cross_vs):
                tags.append("🔀 MIDDLE 🔀")
            elif cross_vs:
                tags.append("↔️ OTHER SIDE ↔️")
            if agree:
                tags.append(f"🤝 AGREES ×{len(agree)}")
            if oppose:
                tags.append(f"⚔️ OPPOSES ×{len(oppose)}")
        else:
            tags.append("🚪 EXIT" if pos is not None and pos["size"] < 1 else "📉 TRIM")

        link = f"https://polymarket.com/event/{t['event_slug'] or t['slug']}"
        league = f"[{meta['league'].upper()}] " if meta and meta.get("league") else ""
        try:
            when = datetime.fromtimestamp(t["ts"], ZoneInfo(self.cfg.tz)).strftime("%Y-%m-%d %H:%M")
        except Exception:
            when = datetime.fromtimestamp(t["ts"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines = [
            f"<b>TRADE ALERT!</b> - <a href=\"https://polymarket.com/profile/{t['wallet']}\">"
            f"{self._name(t['wallet'])}</a>",
            "",
            f"Market: {league}<a href=\"{link}\">{esc(t['title'])}</a>",
            f"Outcome: <b>{esc(pick)}</b>",
            f"Side: {t['side']}",
            f"Amount: {usd:,.2f} USDC" + "".join(f" ({x})" for x in tags),
            f"Price: {vwap:.2f}c({american(vwap)})",
            f"Size: {shares:,.2f} shares" + (f" ({len(fills)} fills)" if len(fills) > 1 else ""),
            f"Time: {when}",
        ]
        if meta and meta.get("game_start"):
            if is_live(meta, t["ts"]):
                lines.append("Starts: 🔴 in-game")
            else:
                mins = (meta["game_start"] - t["ts"]) / 60
                if mins >= 0:
                    lines.append(f"Starts: in {int(mins // 60)}h {int(mins % 60):02d}m")
        # what they already held on the other side
        for h in list(hedge_vs)[:3]:
            lines.append(f"🛡️ Already holds <b>{esc(pick_label(t['title'], h['outcome']))}</b> "
                         f"{h['size']:,.0f} sh @ {h['avg']:.3f} ({american(h['avg'])}) · ${h['cost']:,.0f}")
        if hedge_vs and pos:
            total_cost = pos["cost"] + sum(h["cost"] for h in hedge_vs)
            outs = [f"{esc(pick)} wins {pos['size'] - total_cost:+,.0f}"]
            outs += [f"{esc(pick_label(t['title'], h['outcome']))} wins {h['size'] - total_cost:+,.0f}"
                     for h in hedge_vs]
            lines.append("📐 Net after hedge: " + " · ".join(outs))
        for fv in list(flip_vs)[:3]:
            lines.append(f"⚖️ Also holds <b>{esc(pick_label(t['title'], fv['outcome']))}</b> "
                         f"{fv['size']:,.0f} sh @ {fv['avg']:.3f} ({american(fv['avg'])}) · ${fv['cost']:,.0f}")

        for c in list(cross_vs)[:3]:
            held = (f"<b>{esc(pick_label(c['title'], c['outcome']))}</b> "
                    f"{c['size']:,.0f} sh @ {c['avg']:.3f} ({american(c['avg'])}) · ${c['cost']:,.0f}")
            if c["rel"] == "middle":
                lines.append(f"🔀 Middles their {held}" + (f" → both win if {esc(c['note'])}" if c["note"] else ""))
            elif c["rel"] == "gap":
                lines.append(f"↔️ Other side of their {held}" + (f" → both lose if {esc(c['note'])}" if c["note"] else ""))
            else:
                lines.append(f"↔️ Other side of their {held}")
        for a in list(also_vs)[:3]:
            lines.append(f"➕ Also on <b>{esc(pick_label(a['title'], a['outcome']))}</b> "
                         f"{a['size']:,.0f} sh @ {a['avg']:.3f} ({american(a['avg'])}) · ${a['cost']:,.0f}")

        # analysis
        lines.append("")
        if score:
            tier = {"HIGH": "🔥 HIGH", "MED": "⭐ MED", "LOW": "▫️ LOW"}[score["tier"]]
            lines.append(f"🎯 Conviction {score['pts']:+d} ({tier}): " + " · ".join(score["why"]))
        for a in agree:
            lines.append(f"🤝 {self._name(a['wallet'])} also on "
                         f"{esc(pick_label(a.get('title') or t['title'], a['outcome'] or t['outcome']))}: "
                         f"{a['size']:,.0f} sh @ {a['avg']:.3f} ({american(a['avg'])}) · ${a['cost']:,.0f}")
        for o in oppose:
            mid = (f" (🔀 middle: both win if {esc(o['note'])})" if o.get("rel") == "middle" and o.get("note")
                   else "")
            lines.append(f"⚔️ {self._name(o['wallet'])} is on "
                         f"<b>{esc(pick_label(o.get('title') or t['title'], o['outcome']))}</b>: "
                         f"{o['size']:,.0f} sh @ {o['avg']:.3f} ({american(o['avg'])}) · ${o['cost']:,.0f}{mid}")
        if not buy and tailed:
            lines.append("↩️ Getting off a position we alerted you on")
        both = self.both_sides_line(t, pos, list(hedge_vs) + list(flip_vs)) if buy else None
        if both:
            lines.append(both)
        elif pos is not None and not buy:
            lines.append(f"📦 Still holds {pos['size']:,.0f} sh · avg {pos['avg']:.3f}"
                         if pos["size"] >= 1 else "📦 Fully out")
        elif pos and pos["size"] >= 1:
            lines.append(f"📦 Now holds {pos['size']:,.0f} sh · avg {pos['avg']:.3f} "
                         f"({american(pos['avg'])}) · cost ${pos['cost']:,.0f}")
        if fee and buy:
            base = (self.wallets.get(t["wallet"], {}).get("stats") or {}).get("taker_share")
            base_txt = f" · usually {base:.0%} taker" if base is not None else ""
            if fee["taker_share"] >= 0.01:
                lines.append(f"💸 TAKER {fee['taker_share']:.0%} · paid ${fee['fees']:,.2f} fees "
                             f"({fee['fee_pct']:.2%} of stake){base_txt}")
            else:
                lines.append(f"🧱 MAKER — resting limit, no fees{base_txt}")
        while lines and lines[-1] == "":
            lines.pop()
        return "\n".join(lines)

    @staticmethod
    def both_sides_line(t, pos, others):
        """'📦 A $x (n sh) · B $y (m sh) → bigger on A' for wallets holding both outcomes."""
        if not pos or pos.get("size", 0) < 1 or not others:
            return None
        sides = [(pick_label(t["title"], t["outcome"]), pos["cost"], pos["size"])]
        sides += [(pick_label(t["title"], o["outcome"]), o["cost"], o["size"]) for o in others]
        sides.sort(key=lambda x: -x[1])
        body = " · ".join(f"<b>{esc(n)}</b> ${c:,.0f} ({sz:,.0f} sh)" for n, c, sz in sides)
        return f"📦 {body} → bigger on <b>{esc(sides[0][0])}</b>"

    def format_short(self, t, usd, vwap, pos, fee, hedge_vs=(), flip_vs=(), live=False):
        """3-line version for the sport tabs: who/how, the bet, what they hold now."""
        bits = []
        if live:
            bits.append("🔴 LIVE")
        if t["side"] == "BUY":
            if fee:
                bits.append("💸 PAID" if is_paid(fee) else "🧱 SET")
            if hedge_vs:
                bits.append("🛡️ HEDGE")
            elif flip_vs:
                bits.append("⚖️ BOTH SIDES")
        else:
            bits.append("🚪 EXIT" if pos is not None and pos["size"] < 1 else "📉 TRIM")
        bits += [f"${usd:,.0f}",
                 f"<a href=\"https://polymarket.com/profile/{t['wallet']}\">{self._name(t['wallet'])}</a>"]
        link = f"https://polymarket.com/event/{t['event_slug'] or t['slug']}"
        line2 = (f"<a href=\"{link}\"><b>{esc(short_pick(t['title'], t['outcome']))}</b></a>"
                 f" @ {vwap:.3f} ({american(vwap)})")
        if pos is None:
            line3 = ""
        elif pos["size"] < 1:
            line3 = "📦 Fully out"
        elif t["side"] == "BUY" and (hedge_vs or flip_vs):
            line3 = self.both_sides_line(t, pos, list(hedge_vs) + list(flip_vs))
        else:
            line3 = f"📦 Holds ${pos['cost']:,.0f} ({pos['size']:,.0f} sh)"
        return "\n".join(x for x in (" · ".join(bits), line2, line3) if x)

    async def _consensus(self, t, agree, sport="other"):
        n = len(agree) + 1
        if n < self.cfg.consensus_alert_wallets:
            return
        if not self.store.mark_seen(f"consensus|{t['asset']}|{n}"):
            return
        total = sum(a["cost"] for a in agree)
        who = "\n".join(f"  • {self._name(a['wallet'])} {a['size']:,.0f} sh @ {a['avg']:.3f} "
                        f"(${a['cost']:,.0f})" for a in sorted(agree, key=lambda x: -x["cost"]))
        link = f"https://polymarket.com/event/{t['event_slug'] or t['slug']}"
        text = (f"🔥 <b>CONSENSUS · {n} of your wallets on the same side</b>\n"
                f"<a href=\"{link}\">{esc(t['title'])}</a>\n"
                f"➡️ <b>{esc(pick_label(t['title'], t['outcome']))}</b>\n"
                f"  • {self._name(t['wallet'])} (just now)\n{who}\n"
                f"Others hold ${total:,.0f} combined")
        if not self.muted():
            await self.tg.send_alert(text, sport)

    # --------------------------------------------------------------- websocket
    async def run_ws(self):
        if not self.cfg.use_websocket:
            return
        backoff = 1
        while True:
            try:
                async with websockets.connect(self.cfg.ws_url, ping_interval=None,
                                              open_timeout=15, max_size=2**22) as ws:
                    await ws.send(json.dumps({"action": "subscribe", "subscriptions": [
                        {"topic": "activity", "type": "trades"}]}))
                    self.ws_connected = True
                    self.ws_last_msg = time.time()
                    backoff = 1
                    log.info("RTDS websocket connected")
                    pinger = asyncio.create_task(self._ws_ping(ws))
                    try:
                        async for msg in ws:
                            self.ws_last_msg = time.time()
                            await self._ws_message(msg)
                    finally:
                        pinger.cancel()
            except Exception as e:
                log.warning("websocket error: %s", e)
            self.ws_connected = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def _ws_ping(self, ws):
        while True:
            await asyncio.sleep(5)
            if time.time() - self.ws_last_msg > 60:
                log.warning("websocket silent 60s, reconnecting")
                await ws.close()
                return
            try:
                await ws.send("ping")
            except Exception:
                return

    async def _ws_message(self, msg):
        if not msg or msg[0] not in "{[":
            return
        try:
            data = json.loads(msg)
        except ValueError:
            return
        for d in data if isinstance(data, list) else [data]:
            if d.get("topic") != "activity":
                continue
            payload = d.get("payload") or {}
            self.ws_msgs += 1
            for p in payload if isinstance(payload, list) else [payload]:
                if (p.get("proxyWallet") or "").lower() in self.wallets:
                    await self.ingest(normalize(p, "ws"))

    # ------------------------------------------------------------------ polling
    async def run_poller(self):
        while True:
            interval = self.cfg.poll_seconds * (4 if self.ws_healthy else 1)
            t0 = time.time()
            await asyncio.gather(*(self._poll_one(a) for a in list(self.wallets)),
                                 return_exceptions=True)
            await asyncio.sleep(max(1.0, interval - (time.time() - t0)))

    async def _poll_one(self, addr):
        start = self.last_poll_ts.get(addr, int(time.time())) - 90
        try:
            rows = await self.api.activity(addr, start=start, limit=100)
        except Exception as e:
            log.debug("poll %s failed: %s", addr, e)
            return
        newest = self.last_poll_ts.get(addr, 0)
        for r in sorted(rows, key=lambda r: r.get("timestamp") or 0):
            if r.get("type", "TRADE") != "TRADE":
                continue
            t = normalize(r, "rest")
            if not t["wallet"]:
                t["wallet"] = addr
            newest = max(newest, t["ts"])
            await self.ingest(t)
        self.last_poll_ts[addr] = newest
