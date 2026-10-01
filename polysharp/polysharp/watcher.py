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


def pick_label(title, outcome):
    """The side the bettor actually holds, with its line.

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
        return yes_no_label(title, outcome.lower() == "yes")
    return outcome


_WIN = re.compile(r"^Will (.+?) win(?: on \d{4}-\d{2}-\d{2})?\s*\??$", re.I)
_DRAW = re.compile(r"^Will (.+?) vs\.? (.+?) end in a draw\s*\??$", re.I)
_EXACT = re.compile(r"^Exact Score:\s*(.+?)\s*\??$", re.I)
_BTTS = re.compile(r"both teams to score", re.I)
_WILL = re.compile(r"^Will (.+?)\s*\??$", re.I)


def yes_no_label(title, yes):
    """Turn a Yes/No soccer-style market into the actual bet.

    Will Athletic Club win?        Yes -> "Athletic Club to win"   No -> "Athletic Club NOT to win (draw or loss)"
    Will A vs. B end in a draw?    Yes -> "Draw (A vs B)"          No -> "No draw (A or B wins)"
    Exact Score: A 1 - 1 B?        Yes -> "Exact score A 1-1 B"   No -> "NOT exact score A 1-1 B"
    anything else                  "YES: <question>" / "NO: <question>"
    """
    t = (title or "").strip()
    m = _DRAW.match(t)
    if m:
        a, b = m.group(1).strip(), m.group(2).strip()
        return f"Draw ({a} vs {b})" if yes else f"No draw ({a} or {b} wins)"
    m = _WIN.match(t)
    if m:
        team = m.group(1).strip()
        return f"{team} to win" if yes else f"{team} NOT to win (draw or loss)"
    m = _EXACT.match(t)
    if m:
        score = re.sub(r"\s*-\s*", "-", m.group(1).strip())
        return f"Exact score {score}" if yes else f"NOT exact score {score}"
    if _BTTS.search(t):
        return "Both teams score" if yes else "Both teams NOT to score"
    m = _WILL.match(t)
    q = m.group(1) if m else t.rstrip("?")
    return f"{'YES' if yes else 'NO'}: {q}"


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

        meta = await self._meta(f0)
        live = bool(meta) and is_live(meta, f0["ts"])
        if meta is not None and self.cfg.sports_only_alerts and not meta.get("sports"):
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

        agree, oppose = await self._crowd(f0) if side == "BUY" and not is_hedge else ([], [])
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
                                 tailed=tailed, live=live, score=score)
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
        try:
            rows = await self.api.positions(wallet, market=cond)
        except Exception:
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
        other side meaningful but now smaller:           FLIP (moved weight across)
        """
        other = [p for a, p in (book or {}).items() if a != asset and p["size"] >= 1]
        this_cost = (pos or {}).get("cost") or usd
        opp_cost = sum(p["cost"] for p in other)
        if not other or opp_cost < max(50.0, self.cfg.hedge_dust_pct * this_cost):
            return [], []
        if this_cost <= opp_cost:
            return other, []
        return [], other

    async def _crowd(self, t):
        """Other tracked wallets currently holding either side of this market."""
        since = time.time() - self.cfg.crowd_days * 86400
        others = [w for w in self.store.wallets_in_market(t["condition_id"], since)
                  if w != t["wallet"] and w in self.wallets]
        agree, oppose = [], []
        books = await asyncio.gather(*(self._wallet_book(w, t["condition_id"]) for w in others))
        floor = self.min_usd() * 0.5
        for w, book in zip(others, books):
            for asset, p in (book or {}).items():
                if p["size"] < 1 or p["cost"] < floor:
                    continue
                row = {"wallet": w, **p}
                (agree if asset == t["asset"] else oppose).append(row)
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
                     flip_vs=()):
        if t["side"] == "BUY" and hedge_vs:
            tag = "🛡️ HEDGE"
        elif t["side"] == "BUY" and flip_vs:
            tag = "🔄 FLIP"
        elif t["side"] == "BUY":
            tag = "🟢 NEW" if pos and pos["size"] <= shares * 1.05 else "🟢 ADD"
        else:
            tag = "🚪 EXIT" if pos is not None and pos["size"] < 1 else "📉 TRIM"
        if conviction:
            tag = "⚡ CONVICTION " + tag
        if live:
            tag = "🔴 LIVE " + tag
        if score:
            tag = {"HIGH": "🔥 HIGH", "MED": "⭐ MED", "LOW": "▫️ LOW"}[score["tier"]] + " · " + tag
        verb = "BUY" if t["side"] == "BUY" else "SELL"
        head = f"{tag} {verb} · ${usd:,.0f}"
        if t["side"] == "BUY" and is_paid(fee):
            head = f"💸 PAID ${fee['fees']:,.0f} · " + head
        if t["side"] == "BUY" and agree:
            head += f" · 🤝 AGREES ×{len(agree)}"
        if t["side"] == "BUY" and oppose:
            head += f" · ⚔️ OPPOSES ×{len(oppose)}"
        link = f"https://polymarket.com/event/{t['event_slug'] or t['slug']}"
        when = ""
        if meta and meta.get("game_start"):
            mins = (meta["game_start"] - t["ts"]) / 60
            if is_live(meta, t["ts"]):
                when = " · 🔴 in-game"
            elif mins >= 0:
                when = f" · ⏳ starts in {int(mins // 60)}h {int(mins % 60):02d}m"
        league = f"[{meta['league'].upper()}] " if meta and meta.get("league") else ""
        pick = pick_label(t["title"], t["outcome"])

        # 1. who
        who = f"👤 <a href=\"https://polymarket.com/profile/{t['wallet']}\">{self._name(t['wallet'])}</a>"
        lines = [f"<b>{head}</b> {who}", ""]
        # 2. what
        lines.append(f"{league}<a href=\"{link}\">{esc(t['title'])}</a>{when}")
        lines.append(f"➡️ <b>{esc(pick)} @ {vwap:.3f} ({american(vwap)})</b> ({shares:,.0f} sh"
                     + (f", {len(fills)} fills)" if len(fills) > 1 else ")"))
        lines.append("")
        # 3. why
        if fee and t["side"] == "BUY":
            base = (self.wallets.get(t["wallet"], {}).get("stats") or {}).get("taker_share")
            base_txt = f" · usually {base:.0%} taker" if base is not None else ""
            if fee["taker_share"] >= 0.01:
                lines.append(f"💸 <b>TAKER {fee['taker_share']:.0%}</b> · paid ${fee['fees']:,.2f} fees "
                             f"({fee['fee_pct']:.2%} of stake){base_txt}")
            else:
                lines.append(f"🧱 MAKER — resting limit, no fees{base_txt}")
        if score:
            lines.append(f"🎯 Conviction {score['pts']:+d}: " + " · ".join(score["why"]))
        for a in agree:
            lines.append(f"🤝 {self._name(a['wallet'])} also on "
                         f"{esc(pick_label(t['title'], a['outcome'] or t['outcome']))}: "
                         f"{a['size']:,.0f} sh @ {a['avg']:.3f} ({american(a['avg'])}) · ${a['cost']:,.0f}")
        for o in oppose:
            lines.append(f"⚔️ {self._name(o['wallet'])} is on "
                         f"<b>{esc(pick_label(t['title'], o['outcome']))}</b>: "
                         f"{o['size']:,.0f} sh @ {o['avg']:.3f} ({american(o['avg'])}) · ${o['cost']:,.0f}")
        if hedge_vs:
            for h in hedge_vs:
                lines.append(f"🛡️ Already holds <b>{esc(pick_label(t['title'], h['outcome']))}</b> "
                             f"{h['size']:,.0f} sh @ {h['avg']:.3f} ({american(h['avg'])}) · ${h['cost']:,.0f}")
            if pos:
                total_cost = pos["cost"] + sum(h["cost"] for h in hedge_vs)
                outs = [f"{esc(pick)} wins {pos['size'] - total_cost:+,.0f}"]
                outs += [f"{esc(pick_label(t['title'], h['outcome']))} wins "
                         f"{h['size'] - total_cost:+,.0f}" for h in hedge_vs]
                lines.append("📐 Net after hedge: " + " · ".join(outs))
        for fv in flip_vs:
            lines.append(f"🔄 Was on <b>{esc(pick_label(t['title'], fv['outcome']))}</b> "
                         f"{fv['size']:,.0f} sh @ {fv['avg']:.3f} ({american(fv['avg'])}) · "
                         f"${fv['cost']:,.0f} — now bigger on {esc(pick)}")
        if t["side"] == "SELL" and tailed:
            lines.append("↩️ Getting off a position we alerted you on")
        # 4. where they stand now (bottom)
        if pos is not None and t["side"] == "SELL":
            lines.append(f"📦 Still holds {pos['size']:,.0f} sh · avg {pos['avg']:.3f}"
                         if pos["size"] >= 1 else "📦 Fully out")
        elif pos and pos["size"] >= 1:
            lines.append(f"📦 Now holds {pos['size']:,.0f} sh · avg {pos['avg']:.3f} "
                         f"({american(pos['avg'])}) · cost ${pos['cost']:,.0f}")
        while lines and lines[-1] == "":
            lines.pop()
        return "\n".join(lines)

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
                bits.append("🔄 FLIP")
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
