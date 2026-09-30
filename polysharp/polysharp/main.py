"""Entry point: egress check -> wallet selection -> websocket + poller + commands."""
import asyncio
import json
import logging
import re
import time

import websockets

from .api import PolyAPI
from .config import Config
from .selector import evaluate, passes, select_wallets
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


def fmt_wallet(addr, w):
    s = w.get("stats") or {}
    name = esc(w.get("name") or addr[:10])
    if not s.get("n"):
        return f"• <a href=\"https://polymarket.com/profile/{addr}\">{name}</a> ({w.get('source')})"
    return (f"• <a href=\"https://polymarket.com/profile/{addr}\">{name}</a> — "
            f"ROI {s['roi']:+.1%} · ${s['cost'] / 1e6:,.2f}M staked · n={s['n']} · "
            f"win {s['win_rate']:.0%} · P&L ${s['pnl']:,.0f}"
            + (f" · {s['taker_share']:.0%} taker" if s.get("taker_share") is not None else "")
            + (" · manual" if w.get("source") == "manual" else ""))


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
        self.tg = Telegram(self.cfg.tg_token, self.cfg.tg_chat_id)
        self.watcher = Watcher(self.cfg, self.api, self.store, self.tg)
        self.refreshing = False
        self._register()

    async def refresh(self, announce=True):
        if self.refreshing:
            return "Already refreshing."
        self.refreshing = True
        try:
            picks, summary = await select_wallets(self.api, self.cfg, self.store.blocked())
            if not picks and summary["evaluated"] == 0:
                msg = "⚠️ Refresh got 0 leaderboard candidates — API unreachable? Keeping current list."
            else:
                self.store.replace_auto_wallets(picks)
                self.watcher.reload_wallets()
                msg = (f"🔄 Wallet refresh: {summary['candidates']} candidates → "
                       f"{summary['evaluated']} with volume → {summary['passed']} passed filters → tracking {summary['picked']} "
                       f"(+ manual). Total live: {len(self.watcher.wallets)}")
            self.store.set("last_refresh", time.time())
            if announce:
                await self.tg.send(msg)
            return msg
        finally:
            self.refreshing = False

    async def refresh_loop(self):
        while True:
            last = float(self.store.get("last_refresh", 0))
            due = last + self.cfg.refresh_hours * 3600
            await asyncio.sleep(max(60, due - time.time()))
            try:
                await self.refresh()
                self.store.prune()
            except Exception:
                log.exception("refresh failed")

    # ---------------------------------------------------------------- commands
    def _register(self):
        tg, w, st, cfg = self.tg, self.watcher, self.store, self.cfg

        @tg.command("help")
        async def _help(args):
            return ("<b>PolySharp commands</b>\n"
                    "/status — feed health & settings\n"
                    "/wallets — who's being tracked\n"
                    "/stats 0x… — score a wallet without adding\n"
                    "/add 0x… [name] — track a wallet manually\n"
                    "/remove 0x… — stop tracking (and never auto-pick again)\n"
                    "/min 5000 — minimum $ per alert\n"
                    "/takeronly on|off — only alert when they paid fees to cross\n"
                    "/mute 2h · /unmute\n"
                    "/refresh — rerun leaderboard selection now")

        @tg.command("start")
        async def _start(args):
            return await _help(args)

        @tg.command("status")
        async def _status(args):
            up = (time.time() - w.started) / 3600
            last = float(st.get("last_refresh", 0))
            muted = float(st.get("muted_until", 0))
            ws = ("✅ live" if w.ws_healthy else ("🟡 connected, quiet" if w.ws_connected else "❌ down"))
            return (f"<b>Status</b> (up {up:.1f}h)\n"
                    f"Websocket: {ws} · {w.ws_msgs:,} firehose msgs\n"
                    f"Polling: every {cfg.poll_seconds * (4 if w.ws_healthy else 1):.0f}s\n"
                    f"Wallets: {len(w.wallets)} · alerts sent: {w.alerts_sent}\n"
                    f"Min alert: ${w.min_usd():,.0f} · sells: {'on' if cfg.alert_sells else 'off'}"
                    f" · taker-only: {'on' if st.get('taker_only', False) else 'off'}\n"
                    f"Last refresh: {(time.time() - last) / 3600:.1f}h ago\n"
                    + (f"🔇 Muted for {(muted - time.time()) / 60:.0f} more min" if muted > time.time() else ""))

        @tg.command("wallets")
        async def _wallets(args):
            ws_ = sorted(w.wallets.items(),
                         key=lambda kv: -(kv[1].get("stats") or {}).get("score", 0))
            if not ws_:
                return "No wallets tracked yet. Try /refresh."
            return f"<b>Tracking {len(ws_)} wallets</b>\n" + "\n".join(fmt_wallet(a, x) for a, x in ws_)

        @tg.command("stats")
        async def _stats(args):
            if not args or not ADDR.match(args[0]):
                return "Usage: /stats 0x…"
            s = await evaluate(self.api, cfg, args[0].lower())
            ok, why = passes(s, cfg)
            return (fmt_wallet(args[0].lower(), {"stats": s, "source": "check"}) +
                    f"\nLast settled: {s['days_inactive']}d ago\n" +
                    ("✅ passes filters" if ok else "❌ fails: " + ", ".join(why)))

        @tg.command("add")
        async def _add(args):
            if not args or not ADDR.match(args[0]):
                return "Usage: /add 0x… [name]"
            addr = args[0].lower()
            s = await evaluate(self.api, cfg, addr)
            st.add_manual(addr, " ".join(args[1:]) or None, s)
            w.reload_wallets()
            return "➕ Added\n" + fmt_wallet(addr, w.wallets[addr])

        @tg.command("remove")
        async def _remove(args):
            if not args or not ADDR.match(args[0]):
                return "Usage: /remove 0x…"
            st.remove(args[0])
            w.reload_wallets()
            return f"➖ Removed {esc(args[0][:10])}… and blocked from auto-selection."

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

        @tg.command("takeronly")
        async def _takeronly(args):
            if args and args[0].lower() in ("on", "off"):
                st.set("taker_only", args[0].lower() == "on")
            on = st.get("taker_only", False)
            return (f"Taker-only mode: <b>{'ON' if on else 'OFF'}</b>\n"
                    + ("Only alerting on bundles where ≥50% was taken (fees paid)."
                       if on else "Alerting on all trades; conviction trades get ⚡."))

        @tg.command("unmute")
        async def _unmute(args):
            st.set("muted_until", 0)
            return "🔔 Unmuted."

        @tg.command("refresh")
        async def _refresh(args):
            asyncio.create_task(self.refresh())
            return "Refreshing wallet list — takes a minute or two…"

    # -------------------------------------------------------------------- run
    async def run(self):
        checks = await egress_check(self.cfg, self.api)
        lines = [f"{'✅' if ok else '❌'} {name}: {esc(info)}" for name, ok, info in checks]
        rest_ok = checks[0][1]
        if not rest_ok:
            lines.append("\n<b>Polymarket API unreachable from this host.</b> "
                         "Set HTTPS_PROXY on Railway (see README) and redeploy.")
        await self.tg.send("🚀 <b>PolySharp online</b>\n" + "\n".join(lines))

        self.watcher.reload_wallets()
        stale = time.time() - float(self.store.get("last_refresh", 0)) > self.cfg.refresh_hours * 3600
        if rest_ok and (stale or not self.watcher.wallets):
            await self.refresh()

        await asyncio.gather(
            self.watcher.run_ws(), self.watcher.run_poller(),
            self.tg.poll_commands(), self.refresh_loop())


def main():
    asyncio.run(App().run())


if __name__ == "__main__":
    main()
