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
from .markets import Markets
from .selector import deep_eval, passes, suggest
from .store import Store
from .telegram import Telegram, esc
from .watcher import Watcher

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
    out = [f"Sports P&L: 1W {money(s['pnl_w'])} · 1M {money(s['pnl_m'])} · All {money(s['pnl_all'])}",
           f"1M volume ${s['vol_m'] / 1e6:,.1f}M · margin {s['margin_m']:.2%} "
           f"(all-time {s['margin_all']:.2%})"]
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
        self.tg = Telegram(self.cfg.tg_token, self.cfg.tg_chat_id)
        self.watcher = Watcher(self.cfg, self.api, self.store, self.tg, self.markets)
        self.busy = False
        self._register()

    # ------------------------------------------------------------ evaluation
    async def evaluate(self, addr):
        acts = await self.api.activity(addr, limit=500)
        stats = await deep_eval(self.api, self.markets, self.cfg, addr, acts)
        name = next((a.get("name") or a.get("pseudonym") for a in acts
                     if a.get("name") or a.get("pseudonym")), None)
        return stats, name

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
            await self.tg.send(self.format_digest(picks, summary, health))
            self.store.mark_suggested(p["address"] for p in picks)
            self.store.set("last_suggest", time.time())
            self.store.prune()
        except Exception as e:
            log.exception("morning digest failed")
            await self.tg.send(f"⚠️ Shortlist failed: {esc(e)}")
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
        out.append(f"<i>Bar: sports P&L positive this month and ≥{money(cfg.min_realized_pnl)} all-time, "
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

        @tg.command("help")
        async def _help(args):
            return ("<b>PolySharp commands</b>\n"
                    "<b>Your feed</b>\n"
                    "/add 0x… [name] — start alerting on a wallet\n"
                    "/remove 0x… — stop alerting\n"
                    "/wallets — your list with sports stats\n"
                    "/stats 0x… — check any wallet (sports ROI, live %, activity)\n"
                    "<b>Shortlist</b>\n"
                    f"/suggest — run the shortlist now (auto daily at {cfg.suggest_time})\n"
                    "/skip 0x… [days] — hide from shortlists (default 30d)\n"
                    "<b>Alerts</b>\n"
                    "/min 5000 — minimum $ per alert\n"
                    "/takeronly on|off — only alert when they paid fees to cross\n"
                    "/livehedges on|off — in-game exits/hedges on positions you were alerted on\n"
                    "/mute 2h · /unmute\n"
                    "/status — feed health")

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
            ws_ = sorted(w.wallets.items(), key=lambda kv: (kv[1].get("name") or kv[0]).lower())
            if not ws_:
                return "Your feed is empty. Add one with /add 0x… name"
            return (f"<b>Your feed: {len(ws_)} wallets</b>\n"
                    + "\n".join(fmt_wallet(a, x) for a, x in ws_))

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
            return "🔎 Building the shortlist — takes a few minutes…"

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
        await self.tg.send("🚀 <b>PolySharp online</b>\n" + "\n".join(lines))

        await asyncio.gather(
            self.watcher.run_ws(), self.watcher.run_poller(),
            self.tg.poll_commands(), self.morning_loop())


def main():
    asyncio.run(App().run())


if __name__ == "__main__":
    main()
