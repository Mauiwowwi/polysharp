"""Live web dashboard: top open positions across every tracked account.

Served by the bot itself (no extra service). A background loop rebuilds one JSON
snapshot every DASHBOARD_REFRESH seconds; the page polls it.

    GET /?key=<DASHBOARD_KEY>          the page
    GET /api/data?key=<DASHBOARD_KEY>  the snapshot (JSON)
    GET /health                        "ok" (for Railway)
"""
import asyncio
import hmac
import json
import logging
import os
import time
from urllib.parse import parse_qs, urlsplit

from .fees import fill_mix
from .markets import SPORTS, is_live, sport_of
from .watcher import american, game_key, lean, pick_label, relate

log = logging.getLogger(__name__)
PAGE = os.path.join(os.path.dirname(__file__), "dashboard.html")


def build_snapshot(results, names, crowd_min=250.0, now=None):
    """results: {address: (positions, meta_by_condition)} -> JSON-ready snapshot.

    Every open sports position becomes a row. Agreement / opposition is worked out
    across ALL accounts and ALL markets of the same game (alt lines, ML, totals),
    added up per account; accounts under crowd_min $ on a side are ignored."""
    now = now or time.time()
    rows = []
    for addr, (pool, meta) in results.items():
        for r in pool:
            cond = r.get("conditionId") or ""
            m = meta.get(cond) or {}
            title, outcome = r.get("title") or "", r.get("outcome") or ""
            gs = m.get("game_start")
            status = "live" if gs and is_live(m, now) else "upcoming" if gs and gs > now else \
                "started" if gs else "unknown"
            lg = (m.get("league") or "").lower()
            sp = sport_of(lg)
            avg, cur = float(r.get("avgPrice") or 0), float(r.get("curPrice") or 0)
            rows.append({
                "id": f"{addr[:8]}-{cond[:10]}-{str(r.get('asset'))[-6:]}",
                "account": names.get(addr) or addr[:10], "address": addr,
                "account_url": f"https://polymarket.com/profile/{addr}",
                "sport": sp, "sport_label": SPORTS.get(sp, SPORTS["other"])[0],
                "league": lg.upper(), "title": title,
                "event_url": f"https://polymarket.com/event/{r.get('eventSlug') or r.get('slug') or ''}",
                "pick": pick_label(title, outcome, m.get("event_title")),
                "cost": round(float(r.get("initialValue") or 0), 2),
                "shares": round(float(r.get("size") or 0), 2),
                "avg": avg, "cur": cur, "avg_odds": american(avg), "cur_odds": american(cur),
                "pnl": round(float(r.get("cashPnl") or 0), 2),
                "start": gs, "status": status, "cond": cond, "asset": str(r.get("asset")),
                "fill": None,
                "_cond": cond, "_asset": str(r.get("asset")),
                "_game": game_key(r.get("eventSlug") or m.get("slug") or r.get("slug")),
                "_lean": lean(title, outcome),
            })

    by_game = {}
    for row in rows:
        by_game.setdefault(row["_game"], []).append(row)

    for row in rows:
        agree, oppose = {}, {}
        row["own_other"] = []          # this account's OTHER side of the same game (both sides)
        for o in by_game.get(row["_game"], []):
            if o is row:
                continue
            note = None
            if o["_cond"] == row["_cond"]:
                kind = "same" if o["_asset"] == row["_asset"] else "opposite"
            else:
                rel = relate(row["_lean"], o["_lean"])
                if not rel:
                    continue
                kind, note = rel
            if o["address"] == row["address"]:
                if kind != "same":
                    row["own_other"].append({"pick": o["pick"], "amount": round(o["cost"], 2),
                                             "kind": kind, "note": note})
                continue
            side = agree if kind == "same" else oppose
            a = side.setdefault(o["address"], {"account": o["account"], "amount": 0.0, "picks": []})
            a["amount"] += o["cost"]
            if o["pick"] not in a["picks"]:
                a["picks"].append(o["pick"])
        row["agree"] = sorted((dict(v, amount=round(v["amount"], 2)) for v in agree.values()
                               if v["amount"] >= crowd_min), key=lambda v: -v["amount"])
        row["oppose"] = sorted((dict(v, amount=round(v["amount"], 2)) for v in oppose.values()
                                if v["amount"] >= crowd_min), key=lambda v: -v["amount"])
        row["agree_total"] = round(sum(v["amount"] for v in row["agree"]), 2)
        row["oppose_total"] = round(sum(v["amount"] for v in row["oppose"]), 2)
        row["own_other"].sort(key=lambda v: -v["amount"])
        row["own_other_total"] = round(sum(v["amount"] for v in row["own_other"]), 2)

    for row in rows:
        for k in [k for k in row if k.startswith("_")]:
            del row[k]
    rows.sort(key=lambda x: -x["cost"])
    sports = [{"key": k, "label": SPORTS[k][0]} for k in SPORTS
              if any(r["sport"] == k for r in rows)]
    return {"updated": now, "accounts": len(names), "active_accounts": len({r["address"] for r in rows}),
            "crowd_min": crowd_min, "sports": sports, "positions": rows}


class Dashboard:
    def __init__(self, app, key, port=8080, refresh=30):
        self.app, self.key, self.port, self.refresh = app, key or "", int(port), max(10, int(refresh))
        self.snapshot = None
        self.error = None
        self._mix = {}           # (address, asset) -> (shares when checked, fill_mix result)

    def enabled(self):
        return bool(self.key)

    async def rebuild(self):
        w = self.app.watcher
        addrs = list(w.wallets)
        res = await asyncio.gather(*(self.app.top_positions(a, None) for a in addrs), return_exceptions=True)
        results = {}
        for a, r in zip(addrs, res):
            if isinstance(r, Exception):
                log.warning("dashboard: %s failed: %s", a, r)
                continue
            pool, meta, _, _ = r
            results[a] = (pool, meta)
        names = {a: (v.get("name") or a[:10]) for a, v in w.wallets.items()}
        snap = build_snapshot(results, names, w.crowd_min())
        await self.add_fill_mix(snap)
        self.snapshot = snap

    @staticmethod
    def shown(snap, now=None):
        """Rows any view can show: top 10 per tab (all + each sport) and each tab's 2-hour window."""
        now = now or time.time()
        rows = snap["positions"]
        out = {}
        for key in ["all"] + [s["key"] for s in snap["sports"]]:
            tab = rows if key == "all" else [r for r in rows if r["sport"] == key]
            soon = [r for r in tab if r["status"] == "upcoming" and r["start"] and 0 < r["start"] - now <= 7200]
            for r in tab[:10] + soon[:10]:
                out[r["id"]] = r
        return list(out.values())

    async def add_fill_mix(self, snap):
        """Taken (fees paid) vs laid (limit orders) for every row on screen. Cached until
        the position size changes, so steady positions cost no extra API calls."""
        sem = asyncio.Semaphore(6)

        async def one(r):
            k = (r["address"], r["asset"])
            hit = self._mix.get(k)
            if hit and abs(hit[0] - r["shares"]) < 1:
                r["fill"] = hit[1]
                return
            async with sem:
                try:
                    acts = await self.app.api.activity(r["address"], limit=500, market=r["cond"])
                except Exception:
                    return
            r["fill"] = fill_mix(acts, r["asset"])
            self._mix[k] = (r["shares"], r["fill"])
        await asyncio.gather(*(one(r) for r in self.shown(snap)))

    async def loop(self):
        while True:
            try:
                await self.rebuild()
                self.error = None
            except Exception as e:
                log.exception("dashboard rebuild failed")
                self.error = str(e)
            await asyncio.sleep(self.refresh)

    def _ok(self, qs):
        got = (qs.get("key") or [""])[0]
        return bool(self.key) and hmac.compare_digest(got.encode(), self.key.encode())

    async def handle(self, reader, writer):
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            line = head.split(b"\r\n", 1)[0].decode("latin-1")
            method, target = (line.split(" ") + ["", ""])[:2]
            url = urlsplit(target)
            qs = parse_qs(url.query)
            if method not in ("GET", "HEAD"):
                status, ctype, body = 405, "text/plain", b"method not allowed"
            elif url.path == "/health":
                status, ctype, body = 200, "text/plain", b"ok"
            elif not self._ok(qs):
                status, ctype, body = 403, "text/plain", b"Forbidden - add ?key=YOUR_DASHBOARD_KEY to the link."
            elif url.path in ("/", "/index.html"):
                with open(PAGE, "rb") as f:
                    body = f.read()
                status, ctype = 200, "text/html; charset=utf-8"
            elif url.path == "/api/data":
                snap = self.snapshot or {"updated": None, "positions": [], "sports": [], "loading": True}
                snap = dict(snap, refresh=self.refresh, error=self.error)
                status, ctype, body = 200, "application/json", json.dumps(snap).encode()
            else:
                status, ctype, body = 404, "text/plain", b"not found"
            reason = {200: "OK", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed"}[status]
            hdr = (f"HTTP/1.1 {status} {reason}\r\nContent-Type: {ctype}\r\n"
                   f"Content-Length: {len(body)}\r\nCache-Control: no-store\r\n"
                   f"X-Robots-Tag: noindex\r\nConnection: close\r\n\r\n").encode()
            writer.write(hdr + (b"" if method == "HEAD" else body))
            await writer.drain()
        except Exception as e:
            log.debug("dashboard request error: %s", e)
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def run(self):
        if not self.enabled():
            return
        server = await asyncio.start_server(self.handle, "0.0.0.0", self.port)
        log.info("dashboard on :%s", self.port)
        async with server:
            await asyncio.gather(server.serve_forever(), self.loop())
