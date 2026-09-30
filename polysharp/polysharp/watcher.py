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
import logging
import time

import websockets

from .fees import analyze_fill, summarize
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


class Watcher:
    def __init__(self, cfg, api, store, tg):
        self.cfg, self.api, self.store, self.tg = cfg, api, store, tg
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
        self.store.record_trade(t)

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

    async def on_bundle(self, fills):
        f0 = fills[0]
        shares = sum(f["size"] for f in fills)
        notional = sum(f["size"] * f["price"] for f in fills)
        vwap = notional / shares if shares else f0["price"]
        side = f0["side"]
        if notional < self.min_usd():
            return
        if side == "SELL" and not self.cfg.alert_sells:
            return
        if side == "BUY" and not (self.cfg.min_price <= vwap <= self.cfg.max_price):
            return

        await self._enrich_fees(fills)
        fee = summarize(fills)
        conviction = self._is_conviction(f0["wallet"], fee)
        if self.store.get("taker_only", False) and not (fee and fee["taker_share"] >= 0.5):
            return

        pos = await self._position_after(f0)
        usd = sum(f["usd"] for f in fills)
        text = self.format_alert(f0, fills, usd, shares, vwap, pos, fee, conviction)
        text += self._opposition_line(f0)
        if not self.muted():
            await self.tg.send(text)
            self.alerts_sent += 1
        if side == "BUY":
            await self._check_consensus(f0)

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

    async def _position_after(self, t):
        try:
            rows = await self.api.positions(t["wallet"], market=t["condition_id"])
        except Exception:
            return None
        for r in rows:
            if str(r.get("asset")) == t["asset"]:
                return {"size": float(r.get("size") or 0),
                        "value": float(r.get("currentValue") or 0),
                        "avg": float(r.get("avgPrice") or 0),
                        "cost": float(r.get("initialValue") or 0)}
        return {"size": 0.0, "value": 0.0, "avg": 0.0, "cost": 0.0}

    def _wallet_line(self, addr):
        w = self.wallets.get(addr, {})
        s = w.get("stats") or {}
        name = esc(w.get("name") or addr[:10])
        bits = [f"👤 <a href=\"https://polymarket.com/profile/{addr}\">{name}</a>"]
        if s.get("n"):
            bits.append(f"win {s['win_rate']:.0%} · ROI {s['roi']:+.1%} · n={s['n']}")
        ranks = s.get("ranks") or {}
        if ranks:
            best = min(ranks.items(), key=lambda kv: kv[1])
            bits.append(f"#{best[1]} {best[0].replace(':', ' ').title()}")
        if w.get("source") == "manual":
            bits.append("manual")
        return " · ".join(bits)

    def format_alert(self, t, fills, usd, shares, vwap, pos, fee=None, conviction=False):
        if t["side"] == "BUY":
            tag = "🟢 NEW" if pos and pos["size"] <= shares * 1.05 else "🟢 ADD"
        else:
            tag = "🔴 EXIT" if pos is not None and pos["size"] < 1 else "🟠 TRIM"
        link = f"https://polymarket.com/event/{t['event_slug'] or t['slug']}"
        if conviction:
            tag = "⚡ CONVICTION " + tag
        lines = [
            f"<b>{tag} {t['side']} · ${usd:,.0f}</b>",
            f"<a href=\"{link}\">{esc(t['title'])}</a>",
            f"➡️ <b>{esc(t['outcome'])}</b> @ {vwap:.3f}  ({shares:,.0f} sh"
            + (f", {len(fills)} fills)" if len(fills) > 1 else ")"),
            self._wallet_line(t["wallet"]),
        ]
        if pos and pos["size"] >= 1:
            lines.append(f"📦 Now holds {pos['size']:,.0f} sh · avg {pos['avg']:.3f} "
                         f"· cost ${pos['cost']:,.0f}")
        if fee:
            base = (self.wallets.get(t["wallet"], {}).get("stats") or {}).get("taker_share")
            base_txt = f" · usually {base:.0%} taker" if base is not None else ""
            if fee["taker_share"] >= 0.01:
                lines.append(f"💸 TAKER {fee['taker_share']:.0%} · paid ${fee['fees']:,.2f} fees "
                             f"({fee['fee_pct']:.2%} of stake, rate {fee['rate']:.3f}){base_txt}")
            else:
                lines.append(f"🧱 MAKER — resting limit, no fees{base_txt}")
        lag = time.time() - t["ts"]
        lines.append(f"⏱ {lag:.0f}s after fill · via {t['source']}")
        return "\n".join(lines)

    def _opposition_line(self, t):
        since = time.time() - self.cfg.consensus_hours * 3600
        rows = self.store.db.execute(
            """SELECT wallet, outcome, SUM(usd) usd FROM trades
               WHERE condition_id=? AND asset!=? AND side='BUY' AND ts>=? AND wallet!=?
               GROUP BY wallet, outcome HAVING SUM(usd) >= ?""",
            (t["condition_id"], t["asset"], since, t["wallet"], self.min_usd() * 0.5)).fetchall()
        if not rows:
            return ""
        names = ", ".join(
            f"{esc(self.wallets.get(r['wallet'], {}).get('name') or r['wallet'][:8])} "
            f"({esc(r['outcome'])} ${r['usd']:,.0f})" for r in rows)
        return f"\n⚔️ Opposed by: {names}"

    async def _check_consensus(self, t):
        since = time.time() - self.cfg.consensus_hours * 3600
        buyers = self.store.buyers_of(t["asset"], since)
        buyers = [b for b in buyers if b["usd"] >= self.min_usd() * 0.5]
        n = len(buyers)
        if n < self.cfg.consensus_wallets:
            return
        if not self.store.mark_seen(f"consensus|{t['asset']}|{n}|{int(since // 3600)}"):
            return
        total = sum(b["usd"] for b in buyers)
        who = "\n".join(
            f"  • {esc(self.wallets.get(b['wallet'], {}).get('name') or b['wallet'][:8])}"
            f" ${b['usd']:,.0f} @ {b['px']:.3f}" for b in sorted(buyers, key=lambda x: -x["usd"]))
        link = f"https://polymarket.com/event/{t['event_slug'] or t['slug']}"
        text = (f"🔥 <b>CONSENSUS · {n} sharps · ${total:,.0f}</b>\n"
                f"<a href=\"{link}\">{esc(t['title'])}</a>\n"
                f"➡️ <b>{esc(t['outcome'])}</b> (last {self.cfg.consensus_hours:g}h)\n{who}")
        if not self.muted():
            await self.tg.send(text)

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
