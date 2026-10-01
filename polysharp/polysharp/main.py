"""Entry point: egress check -> manual watchlist feed + morning sports shortlist."""
import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import websockets

from .api import PolyAPI
from .config import Config
from .markets import SPORTS, Markets
from .selector import deep_eval, passes, suggest
from .store import Store
from .telegram import Telegram, esc
from .markets import is_live
from .watcher import Watcher, american, pick_label

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("polysharp")
ADDR = re.compile(r"^0x[a-fA-F0-9]{40}$")


async def egress_check(cfg, api):
    """Railway has blocked Polymarket before -- find out on boot, loudly."""
    out = []
    try:
        rows = await api.leaderboard("DAY", "OVERALL", 1, 0)
        out.append(("Data API", bool(rows), f"{len(rows)} row"))
    except Exception as e:
        out.append(("Data API", False, str(e)[:120]))
    if cfg.use_websocket:
        try:
            async with websockets.connect(cfg.ws_url, open_timeout=10, ping_interval=None) as ws:
                await ws.send(json.dumps({"action": "subscribe", "subscriptions": [
                    {"topic": "activity", "type": "trades"}]}))
                got = await asyncio.wait_for(ws.recv(), timeout=15)
                out.append(("Websocket", True, f"{len(got)}B first msg"))
        except Exception as e:
            out.append(("Websocket", False, str(e)[:120]))
    return out


def profile_link(addr, name):
    return f"<a href=\"https://polymarket.com/profile/{addr}\">{esc(name or addr[:10])}</a>"


def money(x):
    sign = "+" if x >= 0 else "−"
    x = abs(x)
    if x >= 1e6:
        return f"{sign}${x / 1e6:,.2f}M"
    if x >= 1e3:
        return f"{sign}${x / 1e3:,.0f}K"
    return f"{sign}${x:,.0f}"


def stat_lines(s):
    """Compact stat lines from Polymarket's own sports P&L + recent-fill style checks."""
    if not s or "pnl_m" not in s:
        return []
    out = []
    if s.get("predictions") is not None:
        line = f"{s['predictions']:,} predictions · avg bet ${s.get('avg_bet', 0) / 1e3:,.1f}K"
        if s.get("pnl_overall") is not None:
            line += f" · overall P&L {money(s['pnl_overall'])}"
        out.append(line)
    out.append(f"Sports P&L: 1W {money(s['pnl_w'])} · 1M {money(s['pnl_m'])} · All {money(s['pnl_all'])}")
    l3 = f"1M volume ${s['vol_m'] / 1e6:,.1f}M · margin {s['margin_all']:.2%}"
    if s.get("win_rate") is not None:
        l3 += f" · win {s['win_rate']:.0%} (last {s['win_n']:,} settled, {s['win_days']:g}d)"
    out.append(l3)
    bits = []
    if s.get("days_since_trade") is not None:
        bits.append(f"last bet {s['days_since_trade']:.1f}d ago")
    if s.get("sports_share") is not None:
        bits.append(f"pre-game {1 - s.get('live_share', 0):.0%}")
        bits.append(f"sports {s['sports_share']:.0%}")
    if s.get("taker_share") is not None:
        bits.append(f"{s['taker_share']:.0%} taker")
    if s.get("leagues"):
        bits.append("/".join(s["leagues"]))
    if bits:
        out.append(" · ".join(bits))
    return out


def fmt_wallet(addr, w):
    lines = [f"• {profile_link(addr, w.get('name'))}"]
    lines += [f"   {x}" for x in stat_lines(w.get("stats"))]
    return "\n".join(lines)


def fmt_feed_wallet(addr, w):
    """Compact 3-line card for /wallets."""
    s = w.get("stats") or {}
    head = f"• <b>{profile_link(addr, w.get('name'))}</b>"
    if "pnl_m" not in s:
        return head + "\n   (no stats yet)"
    flags = []
    if (s.get("days_since_trade") or 0) > 7:
        flags.append(f"⚠️ cold {s['days_since_trade']:.0f}d")
    if s.get("live_share", 0) > 0.5:
        flags.append(f"⚠️ {s['live_share']:.0%} live")
    l1 = (f"   P&L 1W {money(s['pnl_w'])} · 1M {money(s['pnl_m'])} · "
          f"All {money(s.get('pnl_overall', s['pnl_all']))}")
    bits = [f"{s.get('predictions', 0):,} preds", f"avg ${s.get('avg_bet', 0) / 1e3:,.1f}K"]
    if s.get("win_rate") is not None:
        bits.append(f"win {s['win_rate']:.0%}")
    bits.append(f"margin {s['margin_all']:.2%}")
    l2 = "   " + " · ".join(bits)
    bits = []
    if s.get("taker_share") is not None:
        bits.append(f"taker {s['taker_share']:.0%}")
    bits.append(f"pre-game {1 - s.get('live_share', 0):.0%}")
    if s.get("leagues"):
        bits.append("/".join(s["leagues"]))
    if s.get("days_since_trade") is not None:
        bits.append(f"last bet {s['days_since_trade']:.1f}d")
    l3 = "   " + " · ".join(bits) + (("  " + " ".join(flags)) if flags else "")
    return "\n".join([head, l1, l2, l3])


def cmd_name(name):
    return re.sub(r"[^A-Za-z0-9_.-]", "", (name or "").replace(" ", "_"))[:24]


def next_run(now_utc, hhmm, tz):
    """Next UTC datetime at local time hhmm in tz, strictly after now_utc."""
    h, m = (int(x) for x in hhmm.split(":"))
    local = now_utc.astimezone(ZoneInfo(tz))
    target = local.replace(hour=h, minute=m, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return target.astimezone(ZoneInfo("UTC"))


def parse_duration(s):
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([mhd]?)", s.lower())
    if not m:
        return None
    n, unit = float(m.group(1)), m.group(2) or "h"
    return n * {"m": 60, "h": 3600, "d": 86400}[unit]


class App:
    def __init__(self):
        self.cfg = Config()
        self.cfg.validate()
        self.api = PolyAPI()
        self.store = Store(self.cfg.db_path)
        self.markets = Markets(self.api, self.store)
        self.tg = Telegram(self.cfg.tg_token, self.cfg.tg_chat_id, self.cfg.admin_ids)
        self.watcher = Watcher(self.cfg, self.api, self.store, self.tg, self.markets)
        self.busy = False
        self._register()

    # ------------------------------------------------------------ evaluation
    async def evaluate(self, addr):
        acts = await self.api.activity(addr, limit=500)
        stats = await deep_eval(self.api, self.markets, self.cfg, addr, acts)
        stats["as_of"] = time.time()
        name = next((a.get("name") or a.get("pseudonym") for a in acts
                     if a.get("name") or a.get("pseudonym")), None)
        return stats, name

    async def refresh_stale(self, max_age_h=6, force=False):
        """Re-score wallets whose saved stats are missing, old-format or stale."""
        now = time.time()
        stale = [a for a, w in self.watcher.wallets.items()
                 if force or "pnl_m" not in (w.get("stats") or {})
                 or now - (w.get("stats") or {}).get("as_of", 0) > max_age_h * 3600]

        async def one(addr):
            try:
                stats, _ = await self.evaluate(addr)
                self.store.update_stats(addr, stats)
            except Exception as e:
                log.warning("refresh %s failed: %s", addr, e)
        await asyncio.gather(*(one(a) for a in stale))
        if stale:
            self.watcher.reload_wallets()
        return len(stale)

    def find_wallet(self, query):
        """Match /top10 <name|address|partial name> against the feed."""
        q = query.lower().lstrip("@")
        ws_ = self.watcher.wallets
        if q in ws_:
            return q
        exact = [a for a, w in ws_.items() if (w.get("name") or "").lower() == q]
        if exact:
            return exact[0]
        part = [a for a, w in ws_.items() if q in (w.get("name") or "").lower() or a.startswith(q)]
        return part[0] if len(part) == 1 else (part or None)

    async def top_positions(self, addr, n=10):
        """Biggest OPEN sports positions by $ in (resolved ones are excluded)."""
        rows = await self.api.positions(addr, sort="INITIAL")
        rows = [r for r in rows if not r.get("redeemable")
                and 0 < float(r.get("curPrice") or 0) < 1 and float(r.get("size") or 0) >= 1]
        meta = await self.markets.get({r["conditionId"]: r.get("eventSlug") or r.get("slug") or ""
                                       for r in rows if r.get("conditionId")})
        sports = [r for r in rows if (meta.get(r.get("conditionId")) or {}).get("sports")]
        other = len(rows) - len(sports)
        pool = sports if self.cfg.sports_only_alerts else rows
        pool.sort(key=lambda r: -float(r.get("initialValue") or 0))
        return pool[:n], meta, len(pool), other

    async def trade_count(self, addr, cond, asset):
        try:
            rows = await self.api.activity(addr, limit=500, market=cond)
        except Exception:
            return None
        return sum(1 for r in rows if str(r.get("asset")) == str(asset)
                   and (r.get("side") or "").upper() == "BUY")

    async def format_top10(self, addr):
        name = (self.watcher.wallets.get(addr) or {}).get("name") or addr[:10]
        top, meta, total, other = await self.top_positions(addr)
        if not top:
            return f"{profile_link(addr, name)} has no open sports positions right now."
        counts = await asyncio.gather(*(self.trade_count(addr, r.get("conditionId"), r.get("asset"))
                                        for r in top))
        now = time.time()
        stake = sum(float(r.get("initialValue") or 0) for r in top)
        pnl = sum(float(r.get("cashPnl") or 0) for r in top)
        out = [f"🏆 <b>{profile_link(addr, name)} — top {len(top)} open positions</b>",
               f"${stake:,.0f} in · now {money(pnl)} · {total} open sports positions"
               + (f" (+{other} non-sports hidden)" if other else "")]
        for i, (r, n) in enumerate(zip(top, counts), 1):
            m = meta.get(r.get("conditionId")) or {}
            cost = float(r.get("initialValue") or 0)
            shares = float(r.get("size") or 0)
            avg, cur = float(r.get("avgPrice") or 0), float(r.get("curPrice") or 0)
            dot = "🟢" if float(r.get("cashPnl") or 0) >= 0 else "🔴"
            when = ""
            if m.get("game_start"):
                if is_live(m, now):
                    when = " · 🔴 in-game"
                elif m["game_start"] > now:
                    mins = (m["game_start"] - now) / 60
                    when = (f" · ⏳ {mins / 1440:.0f}d" if mins > 48 * 60
                            else f" · ⏳ {int(mins // 60)}h {int(mins % 60):02d}m")
            league = f"[{m['league'].upper()}] " if m.get("league") else ""
            link = f"https://polymarket.com/event/{r.get('eventSlug') or r.get('slug')}"
            out += ["",
                    f"<b>{i}. {dot} ${cost:,.0f}</b>{when}",
                    f"{league}<a href=\"{link}\">{esc(r.get('title'))}</a>",
                    f"Outcome: <b>{esc(pick_label(r.get('title'), r.get('outcome')))}</b>",
                    f"Trades: {n if n is not None else '—'} | Shares: {shares:,.0f}",
                    f"Cost: ${cost:,.0f} | Payout: ${shares:,.0f}",
                    f"💰 Profit if it wins: {'+' if shares >= cost else '−'}${abs(shares - cost):,.0f}",
                    f"Avg: {avg * 100:.1f}¢ ({american(avg)}) | Last: {cur * 100:.1f}¢ ({american(cur)})"]
        return "\n".join(out)

    # ------------------------------------------------------------ sport topics
    async def handle_migration(self, old, new):
        m = self.store.get("chat_migrations", {})
        m[old] = new
        self.store.set("chat_migrations", m)
        self.store.set("topics", self.tg.topics)
        await self.tg.send_admins(
            f"ℹ️ Telegram upgraded your group to a supergroup (that happens when Topics is turned on). "
            f"New chat id: <code>{new}</code>\nThe bot has switched over automatically. Please also set "
            f"<code>TELEGRAM_CHAT_ID</code> in Railway to <code>{new}</code> so it sticks.")

    async def ensure_topics(self):
        """Create any missing sport tabs in topic-enabled groups. Returns a status line."""
        self.tg.all_feed = self.cfg.all_feed
        self.tg.silent_general = self.cfg.silent_general
        saved = self.store.get("topics", {})
        self.tg.topics = {c: dict(v) for c, v in saved.items()}
        self.tg.on_migrate = self.handle_migration
        self.tg.apply_migrations(self.store.get("chat_migrations", {}))
        if not self.cfg.sport_topics:
            self.tg.topics = {}
            return "Sport topics: off"
        notes = []
        for chat in [c for c in self.tg.chat_ids if c.startswith("-")]:
            have = self.tg.topics.setdefault(chat, {})
            for key, (title, _) in SPORTS.items():
                if key in have:
                    continue
                tid, err = await self.tg.create_topic(chat, title)
                if self.tg.resolve(chat) != chat:          # group id changed mid-setup
                    have = self.tg.topics.setdefault(self.tg.resolve(chat), have)
                    self.tg.topics.pop(chat, None)
                    chat = self.tg.resolve(chat)
                if tid:
                    have[key] = tid
                    continue
                if err and "not a forum" in err.lower():
                    notes.append("group doesn't have Topics turned on")
                elif err and ("rights" in err.lower() or "admin" in err.lower()):
                    notes.append("make Whaletail a group admin with 'Manage Topics'")
                else:
                    notes.append(err or "couldn't create topics")
                break
            if not have:
                self.tg.topics.pop(chat, None)
        self.store.set("topics", self.tg.topics)
        ready = sum(len(v) for v in self.tg.topics.values())
        if ready:
            return f"Sport topics: {ready} tabs ready" + (f" (⚠️ {notes[0]})" if notes else "")
        return "Sport topics: not set up" + (f" — {notes[0]}" if notes else "")

    def topics_text(self):
        if not self.tg.topics:
            return ("No sport tabs yet. Turn on Topics in the group, make Whaletail an admin with "
                    "'Manage Topics', then /topics setup. Or create tabs yourself and run "
                    "/bindtopic &lt;sport&gt; inside each one.\nSports: " + ", ".join(SPORTS))
        out = ["<b>Sport tabs</b>"]
        for chat, m in self.tg.topics.items():
            for key, (title, _) in SPORTS.items():
                out.append(f"{title}: {'✅' if key in m else '— (goes to General)'}")
        out.append(f"All feed in General: {'on' if self.tg.all_feed else 'off'}")
        return "\n".join(out)

    # --------------------------------------------------------- morning digest
    async def morning(self, manual=False):
        if self.busy:
            return "Already running — results coming shortly."
        self.busy = True
        try:
            tracked = set(self.watcher.wallets)
            exclude = tracked | self.store.skipped()
            if not manual:
                exclude |= self.store.recently_suggested(self.cfg.suggest_cooldown_days)
            picks, summary = await suggest(self.api, self.markets, self.cfg, exclude)
            health = await self.watchlist_health()
            await self.tg.send_admins(self.format_digest(picks, summary, health))
            self.store.mark_suggested(p["address"] for p in picks)
            self.store.set("last_suggest", time.time())
            self.store.prune()
        except Exception as e:
            log.exception("morning digest failed")
            await self.tg.send_admins(f"⚠️ Shortlist failed: {esc(e)}")
        finally:
            self.busy = False

    async def watchlist_health(self):
        out = []
        for addr, w in list(self.watcher.wallets.items()):
            try:
                stats, _ = await self.evaluate(addr)
                self.store.update_stats(addr, stats)
            except Exception as e:
                log.warning("health %s failed: %s", addr, e)
                stats = w.get("stats") or {}
            out.append((addr, w.get("name"), stats))
        self.watcher.reload_wallets()
        return out

    def format_digest(self, picks, summary, health):
        cfg = self.cfg
        out = [f"☀️ <b>Sports sharps shortlist</b> — {len(picks)} to look at "
               f"(from {summary['screened']} checked)"]
        if summary.get("errors") and not summary["screened"]:
            out.append(f"⚠️ Leaderboard error: {esc(summary['errors'][0])}")
        for i, c in enumerate(picks, 1):
            s = c["stats"]
            out.append(f"\n<b>{i}.</b> {profile_link(c['address'], c['name'])}")
            out += [f"   {x}" for x in stat_lines(s)]
            out.append(f"   <code>/add {c['address']} {cmd_name(c['name'])}</code>")
        if not picks:
            out.append("\nNobody new cleared the bar today.")
        f = summary.get("fails") or {}
        if f:
            out.append("\n<i>Filtered out: " + ", ".join(
                f"{v} {k}" for k, v in sorted(f.items(), key=lambda kv: -kv[1]) if v) + "</i>")
        out.append(f"<i>Bar: ≥{cfg.min_predictions} predictions, avg bet ≥${cfg.min_avg_bet:,.0f}, "
                   f"sports P&L positive this month and ≥{money(cfg.min_realized_pnl)} all-time, "
                   f"≥${cfg.min_month_vol / 1e3:,.0f}K monthly volume, margin ≥{cfg.min_margin:.1%}, "
                   f"≥{cfg.min_sports_share:.0%} sports, "
                   f"≤{cfg.max_live_share:.0%} live, bet in last {cfg.max_days_inactive:g}d</i>")
        out.append("Not interested? <code>/skip 0x…</code> hides one for 30 days.")
        if health:
            out.append("\n<b>Your list</b>")
            for addr, name, s in health:
                _, why = passes(s, cfg)
                flags = [r for r in why if r.startswith(("cold", "inactive", "live", "sports", "1M P&L"))]
                icon = "⚠️" if flags else "✅"
                bits = [f"1M {money(s['pnl_m'])}"] if "pnl_m" in s else []
                if s.get("days_since_trade") is not None:
                    bits.append(f"last bet {s['days_since_trade']:.1f}d")
                if s.get("live_share") is not None:
                    bits.append(f"pre-game {1 - s['live_share']:.0%}")
                out.append(f"{icon} {profile_link(addr, name)} — " + " · ".join(bits)
                           + (f" · <b>{', '.join(flags)}</b>" if flags else ""))
        return "\n".join(out)

    async def morning_loop(self):
        while True:
            nxt = next_run(datetime.now(ZoneInfo("UTC")), self.cfg.suggest_time, self.cfg.tz)
            log.info("Next shortlist at %s UTC", nxt.isoformat())
            await asyncio.sleep(max(30, (nxt - datetime.now(ZoneInfo("UTC"))).total_seconds()))
            await self.morning()

    # ---------------------------------------------------------------- commands
    def _register(self):
        tg, w, st, cfg = self.tg, self.watcher, self.store, self.cfg
        tg.admin_only = set(self.ADMIN_ONLY)

        @tg.command("help")
        async def _help(args):
            out = ("<b>PolySharp</b>\n"
                   "/top10 [name] — biggest open positions (no name = pick from buttons)\n"
                   "/wallets — accounts we follow, with stats\n"
                   "/stats 0x… — check any wallet\n"
                   "/status — feed health")
            if tg.is_admin:
                out += ("\n\n<b>Admin</b>\n"
                        "/add 0x… [name] · /remove 0x… — manage the feed\n"
                        f"/suggest — shortlist now (auto daily at {cfg.suggest_time}, sent to you privately)\n"
                        "/skip 0x… [days] — hide from shortlists\n"
                        "/min 5000 · /tier all|med|high · /takeronly on|off · /livehedges on|off\n"
                        "/mute 2h · /unmute\n"
                        "/topics [setup] · /bindtopic &lt;sport&gt; — sport tabs in the group")
            return out

        @tg.command("start")
        async def _start(args):
            return await _help(args)

        @tg.command("status")
        async def _status(args):
            up = (time.time() - w.started) / 3600
            last = float(st.get("last_suggest", 0))
            muted = float(st.get("muted_until", 0))
            ws = ("✅ live" if w.ws_healthy else ("🟡 connected, quiet" if w.ws_connected else "❌ down"))
            nxt = next_run(datetime.now(ZoneInfo("UTC")), cfg.suggest_time, cfg.tz)
            return (f"<b>Status</b> (up {up:.1f}h)\n"
                    f"Websocket: {ws} · {w.ws_msgs:,} firehose msgs\n"
                    f"Wallets: {len(w.wallets)} · alerts sent: {w.alerts_sent} · "
                    f"below tier: {w.below_tier} · tier: {st.get('min_tier', 'all')} · "
                    f"filtered (non-sports/live): {w.skipped_filtered}\n"
                    f"Min alert: ${w.min_usd():,.0f} · sells: {'on' if cfg.alert_sells else 'off'}"
                    f" · taker-only: {'on' if st.get('taker_only', False) else 'off'}\n"
                    f"Sports-only: {'on' if cfg.sports_only_alerts else 'off'} · "
                    f"pre-game only: {'on' if cfg.pregame_only_alerts else 'off'} · "
                    f"live hedges/exits: {'on' if w.live_hedges_on() else 'off'}\n"
                    f"Last shortlist: {'never' if not last else f'{(time.time() - last) / 3600:.1f}h ago'}"
                    f" · next in {(nxt - datetime.now(ZoneInfo('UTC'))).total_seconds() / 3600:.1f}h\n"
                    + (f"🔇 Muted for {(muted - time.time()) / 60:.0f} more min" if muted > time.time() else ""))

        @tg.command("wallets")
        async def _wallets(args):
            if not w.wallets:
                return "Your feed is empty. Add one with /add 0x… name"
            await self.refresh_stale(force=bool(args and args[0].lower() == "refresh"))
            ws_ = sorted(w.wallets.items(),
                         key=lambda kv: -((kv[1].get("stats") or {}).get("pnl_m") or 0))
            return (f"<b>Your feed: {len(ws_)} wallets</b> (sorted by 1M sports P&L)\n\n"
                    + "\n\n".join(fmt_feed_wallet(a, x) for a, x in ws_)
                    + "\n\n<i>Stats refresh every 6h · /wallets refresh to force</i>")

        @tg.command("stats")
        async def _stats(args):
            if not args or not ADDR.match(args[0]):
                return "Usage: /stats 0x…"
            addr = args[0].lower()
            stats, name = await self.evaluate(addr)
            ok, why = passes(stats, cfg)
            return (fmt_wallet(addr, {"name": name, "stats": stats}) + "\n"
                    + ("✅ would make the shortlist" if ok else "❌ shortlist bar: " + ", ".join(why))
                    + ("\n(already in your feed)" if addr in w.wallets else
                       f"\n<code>/add {addr} {cmd_name(name)}</code>"))

        @tg.command("add")
        async def _add(args):
            if not args or not ADDR.match(args[0]):
                return "Usage: /add 0x… [name] — the address is on their Polymarket profile"
            addr = args[0].lower()
            stats, name = await self.evaluate(addr)
            st.add_manual(addr, " ".join(args[1:]) or name, stats)
            w.reload_wallets()
            return "➕ Added to your feed\n" + fmt_wallet(addr, w.wallets[addr])

        @tg.command("remove")
        async def _remove(args):
            if not args or not ADDR.match(args[0]):
                return "Usage: /remove 0x…"
            name = (w.wallets.get(args[0].lower()) or {}).get("name") or args[0][:10]
            if not st.remove(args[0]):
                return "That wallet isn't in your feed."
            st.skip(args[0], 30)
            w.reload_wallets()
            return f"➖ Removed {esc(name)} (also hidden from shortlists for 30 days)."

        @tg.command("skip")
        async def _skip(args):
            if not args or not ADDR.match(args[0]):
                return "Usage: /skip 0x… [days]"
            try:
                days = float(args[1]) if len(args) > 1 else 30
            except ValueError:
                return "Usage: /skip 0x… [days]"
            st.skip(args[0], days)
            return f"🙈 Hidden from shortlists for {days:g} days."

        @tg.command("suggest")
        async def _suggest(args):
            if self.busy:
                return "Already running — results coming shortly."
            asyncio.create_task(self.morning(manual=True))
            return "🔎 Building the shortlist — takes a few minutes. It'll arrive in your private chat with the bot."

        @tg.command("refresh")
        async def _refresh(args):
            return await _suggest(args)

        @tg.command("min")
        async def _min(args):
            try:
                v = float(args[0].replace("$", "").replace(",", "").lower().replace("k", "e3"))
            except (IndexError, ValueError):
                return f"Current min: ${w.min_usd():,.0f}. Usage: /min 5000"
            st.set("min_alert_usd", v)
            return f"Min alert set to ${v:,.0f}"

        @tg.command("mute")
        async def _mute(args):
            secs = parse_duration(args[0]) if args else 3600
            if not secs:
                return "Usage: /mute 30m | 2h | 1d"
            st.set("muted_until", time.time() + secs)
            return f"🔇 Muted for {secs / 3600:.1f}h (still recording trades for consensus)."

        @tg.command("unmute")
        async def _unmute(args):
            st.set("muted_until", 0)
            return "🔔 Unmuted."

        @tg.command("top10")
        async def _top10(args):
            if not w.wallets:
                return "Your feed is empty. Add one with /add 0x… name"
            if args:
                hit = self.find_wallet(" ".join(args))
                if isinstance(hit, str):
                    return await self.format_top10(hit)
                if hit:
                    return ("Which one?", [[(w.wallets[a].get("name") or a[:10], f"top:{a}")] for a in hit])
                return (f"No wallet in your feed matches '{esc(' '.join(args))}'. Pick one:",
                        self._wallet_buttons())
            return ("🏆 <b>Top 10 open positions</b> — tap an account:", self._wallet_buttons())

        @tg.callback("top")
        async def _top_cb(addr):
            if addr not in w.wallets:
                return "That wallet isn't in your feed any more."
            return await self.format_top10(addr)

        @tg.command("topics")
        async def _topics(args):
            if args and args[0].lower() == "setup":
                return (await self.ensure_topics()) + "\n\n" + self.topics_text()
            return self.topics_text()

        @tg.command("bindtopic")
        async def _bindtopic(args):
            key = (args[0].lower() if args else "")
            if key not in SPORTS:
                return "Usage (inside a topic tab): /bindtopic " + "|".join(SPORTS)
            if not tg.current_thread:
                return "Run this inside the topic tab you want that sport's alerts to go to."
            tg.topics.setdefault(tg.current_chat, {})[key] = tg.current_thread
            st.set("topics", tg.topics)
            return f"✅ {SPORTS[key][0]} alerts will post in this tab."

        @tg.command("tier")
        async def _tier(args):
            if args and args[0].lower() in ("all", "med", "medium", "high"):
                st.set("min_tier", {"medium": "med"}.get(args[0].lower(), args[0].lower()))
            cur = st.get("min_tier", "all")
            return (f"Buy alerts shown: <b>{ {'all': 'ALL', 'med': 'MED + HIGH', 'high': 'HIGH only'}[cur] }</b>\n"
                    "Score: size vs usual bet (≥1.5× +1, ≥3× +2) · still buying +1 · "
                    "each agreeing wallet +1 (max 2) · any opposition −2 · paid to cross +1\n"
                    f"🔥 HIGH ≥{cfg.tier_high} · ⭐ MED ≥{cfg.tier_med} · ▫️ LOW below\n"
                    "Sells, exits, hedges and 🔥 CONSENSUS always come through.\n"
                    "Usage: /tier all | med | high")

        @tg.command("livehedges")
        async def _livehedges(args):
            if args and args[0].lower() in ("on", "off"):
                st.set("live_hedges", args[0].lower() == "on")
            on = w.live_hedges_on()
            return (f"Live hedges/exits: <b>{'ON' if on else 'OFF'}</b>\n"
                    + ("In-game BUYs are still never sent. You WILL get in-game sells/hedges, but only "
                       "on positions you were alerted on pre-game."
                       if on else "Nothing in-game is sent at all."))

        @tg.command("takeronly")
        async def _takeronly(args):
            if args and args[0].lower() in ("on", "off"):
                st.set("taker_only", args[0].lower() == "on")
            on = st.get("taker_only", False)
            return (f"Taker-only mode: <b>{'ON' if on else 'OFF'}</b>\n"
                    + ("Only alerting on bundles where ≥50% was taken (fees paid)."
                       if on else "Alerting on all trades; conviction trades get ⚡."))

    ADMIN_ONLY = {"add", "remove", "skip", "suggest", "refresh", "min", "tier", "topics", "bindtopic",
                  "takeronly", "livehedges", "mute", "unmute"}

    def _wallet_buttons(self, prefix="top"):
        names = sorted(self.watcher.wallets.items(), key=lambda kv: (kv[1].get("name") or kv[0]).lower())
        btns = [(w.get("name") or a[:10], f"{prefix}:{a}") for a, w in names]
        return [btns[i:i + 2] for i in range(0, len(btns), 2)]

    # -------------------------------------------------------------------- run
    async def run(self):
        checks = await egress_check(self.cfg, self.api)
        lines = [f"{'✅' if ok else '❌'} {name}: {esc(info)}" for name, ok, info in checks]
        if not checks[0][1]:
            lines.append("\n<b>Polymarket API unreachable from this host.</b> "
                         "Set HTTPS_PROXY on Railway (see README) and redeploy.")
        had_auto = any(v["source"] == "auto" for v in self.store.active_wallets().values())
        self.store.drop_auto_wallets()
        self.watcher.reload_wallets()
        lines.append(f"Feed: {len(self.watcher.wallets)} wallets you added"
                     + (" (auto-picked wallets cleared — feed is manual now)" if had_auto else ""))
        lines.append(f"Shortlist: daily at {self.cfg.suggest_time} ({self.cfg.tz}) · /suggest to run now")
        lines.append(await self.ensure_topics())
        lines.append(f"Chats: {len(self.tg.chat_ids)} · admins: {len(self.tg.admins)}"
                     + ("" if self.tg.admins else " ⚠️ set ADMIN_USER_IDS"))
        await self.tg.send_admins("🚀 <b>PolySharp online</b>\n" + "\n".join(lines))

        await asyncio.gather(
            self.watcher.run_ws(), self.watcher.run_poller(),
            self.tg.poll_commands(), self.morning_loop())


def main():
    asyncio.run(App().run())


if __name__ == "__main__":
    main()
