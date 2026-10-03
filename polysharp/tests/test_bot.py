import asyncio
import json
import time

import pytest
import websockets

from polysharp.config import Config
from polysharp.store import Store
from polysharp.watcher import Watcher, normalize

A = "0x" + "a" * 40
B = "0x" + "b" * 40
C = "0x" + "c" * 40
D = "0x" + "d" * 40
E = "0x" + "e" * 40


class FakeTG:
    def __init__(self):
        self.sent = []

    async def send(self, text, chat_id=None):
        self.sent.append(text)

    async def send_alert(self, text, sport="other", short=None, buttons=None):
        self.sent.append(text)


class FakeAPI:
    def __init__(self):
        self.pos = {}
        self.act = {}

    async def positions(self, user, market=None, redeemable=None):
        if redeemable:
            return [{"curPrice": 0, "initialValue": 1000}] if user == B else []
        return self.pos.get(user, [])

    async def activity(self, user, start=None, limit=100):
        return self.act.get(user, [])

    async def leaderboard(self, period, category, limit, offset, order="PNL"):
        if offset:
            return []
        return [{"proxyWallet": A, "userName": "alpha", "rank": "3", "vol": 5e5},
                {"proxyWallet": B, "userName": "beta", "rank": "5", "vol": 5e5},
                {"proxyWallet": C, "userName": "mm", "rank": "1", "vol": 9e6},
                {"proxyWallet": D, "userName": "btystu", "rank": "2", "vol": 3.6e5},
                {"proxyWallet": E, "userName": "tiny", "rank": "9", "vol": 5e4}]

    async def closed_positions(self, user, max_rows=500):
        now = int(time.time())
        if user == A:   # 60 bets, 40 winners, strong margin
            return ([{"totalBought": 2000, "avgPrice": 0.5, "realizedPnl": 600, "timestamp": now}] * 40 +
                    [{"totalBought": 2000, "avgPrice": 0.5, "realizedPnl": -700, "timestamp": now}] * 20)
        if user == D:   # btystu-style: huge ROI, tiny sample, low volume
            return [{"totalBought": 10000, "avgPrice": 0.5, "realizedPnl": 3500, "timestamp": now}] * 46
        if user == E:
            raise AssertionError("pre-cut wallet should never be fetched")
        if user == B:   # looks good but hides unredeemed losers -> still ok-ish
            return [{"totalBought": 1000, "avgPrice": 0.5, "realizedPnl": 400, "timestamp": now}] * 50
        # market maker: huge volume, thin margin
        return ([{"totalBought": 100000, "avgPrice": 0.5, "realizedPnl": 300, "timestamp": now}] * 60 +
                [{"totalBought": 100000, "avgPrice": 0.5, "realizedPnl": -250, "timestamp": now}] * 40)


def cfg(tmp_path, **kw):
    c = Config()
    c.db_path = str(tmp_path / "t.db")
    c.tg_token = c.tg_chat_id = "x"
    c.bundle_seconds = 0.2
    c.fee_retry_seconds = 0
    c.min_alert_usd = 1000
    c.min_realized_pnl = 1000
    c.lb_slices = ["MONTH:OVERALL"]
    c.min_closed = 40
    c.min_staked = 20000
    c.min_lb_vol = 100000
    c.min_win_rate = 0.55
    c.min_roi = 0.04
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def fill(wallet, size=5000, price=0.4, tx="0xt1", side="BUY", asset="111", outcome="Yes", cond="0xc1"):
    return {"proxyWallet": wallet, "size": size, "price": price, "side": side, "asset": asset,
            "conditionId": cond, "outcome": outcome, "title": "Will X happen?", "slug": "x",
            "eventSlug": "x-event", "transactionHash": tx, "timestamp": int(time.time())}


@pytest.mark.asyncio
async def test_bundling_dedupe_and_alert(tmp_path):
    c = cfg(tmp_path)
    st, tg, api = Store(c.db_path), FakeTG(), FakeAPI()
    st.add_manual(A, "alpha", {"n": 60, "win_rate": .66, "roi": .1, "pnl": 5000, "score": 1})
    w = Watcher(c, api, st, tg)
    w.reload_wallets()
    api.pos[A] = [{"asset": "111", "size": 7500, "currentValue": 3000, "avgPrice": 0.4, "initialValue": 3000}]
    # two partial fills in one order via WS, then REST replays the same tx
    await w.ingest(normalize(fill(A, 5000, 0.4, "0xt1"), "ws"))
    await w.ingest(normalize(fill(A, 2500, 0.4, "0xt2"), "ws"))
    await w.ingest(normalize(fill(A, 5000, 0.4, "0xt1"), "rest"))
    await w.ingest(normalize(fill(B, 5000, 0.4, "0xt3"), "ws"))  # untracked wallet
    await asyncio.sleep(0.4)
    assert len(tg.sent) == 1, tg.sent
    msg = tg.sent[0]
    assert "3,000.00 USDC" in msg and "2 fills" in msg and "Side: BUY" in msg and "alpha" in msg


@pytest.mark.asyncio
async def test_min_filter(tmp_path):
    c = cfg(tmp_path)
    st, tg, api = Store(c.db_path), FakeTG(), FakeAPI()
    st.add_manual(A, "alpha")
    w = Watcher(c, api, st, tg)
    w.reload_wallets()
    await w.ingest(normalize(fill(A, 100, 0.4, "0x1"), "ws"))   # $40 -> below min
    await asyncio.sleep(0.4)
    assert tg.sent == []


@pytest.mark.asyncio
async def test_websocket_feed(tmp_path):
    received = []

    async def server(ws):
        received.append(json.loads(await ws.recv()))
        await ws.send(json.dumps({"topic": "activity", "type": "trades",
                                  "payload": fill(A, 10000, 0.5, "0xws")}))
        await ws.send(json.dumps({"topic": "activity", "type": "trades",
                                  "payload": fill(B, 10000, 0.5, "0xws2")}))
        await asyncio.sleep(2)

    async with websockets.serve(server, "127.0.0.1", 8765):
        c = cfg(tmp_path, ws_url="ws://127.0.0.1:8765")
        st, tg = Store(c.db_path), FakeTG()
        st.add_manual(A, "alpha")
        w = Watcher(c, FakeAPI(), st, tg)
        w.reload_wallets()
        task = asyncio.create_task(w.run_ws())
        await asyncio.sleep(0.8)
        task.cancel()
    assert received[0]["subscriptions"][0] == {"topic": "activity", "type": "trades"}
    assert len(tg.sent) == 1 and "5,000.00 USDC" in tg.sent[0] and "via ws" not in tg.sent[0]


# --- fee / conviction -------------------------------------------------------
from polysharp.fees import analyze_fill, taker_baseline


def test_fee_math_on_live_rows():
    # real /activity rows pulled 2026-09-30
    t = analyze_fill(5, 0.73, 3.67956, "BUY")
    assert t["taker"] and abs(t["rate"] - 0.03) < 1e-4 and abs(t["fee"] - 0.02956) < 1e-5
    assert analyze_fill(5, 0.78, 3.92574, "BUY")["taker"]
    m = analyze_fill(13872.07, 0.68, 9433.0076, "BUY")
    assert not m["taker"] and m["fee"] == 0
    s = analyze_fill(1000, 0.5, 500 - 1000 * .05 * .25, "SELL")  # sell: fee comes out of proceeds
    assert s["taker"] and abs(s["rate"] - 0.05) < 1e-6
    assert analyze_fill(5, 0.5, None, "BUY") is None


def test_taker_baseline():
    rows = [{"type": "TRADE", "side": "BUY", "size": 1000, "price": .5, "usdcSize": 500}] * 9 + \
           [{"type": "TRADE", "side": "BUY", "size": 1000, "price": .5, "usdcSize": 507.5}]
    assert taker_baseline(rows) == {"taker_share": 0.1, "sample": 10}


@pytest.mark.asyncio
async def test_conviction_alert_enriches_ws_fill(tmp_path):
    c = cfg(tmp_path)
    st, tg, api = Store(c.db_path), FakeTG(), FakeAPI()
    st.add_manual(A, "maker_guy", {"n": 100, "win_rate": .6, "roi": .08, "pnl": 9e4, "score": 1,
                                   "taker_share": 0.1})
    w = Watcher(c, api, st, tg)
    w.reload_wallets()
    f = fill(A, 10000, 0.6, "0xconv")
    # WS payload has no usdcSize; REST has it with a 3% taker fee baked in
    api.act[A] = [dict(f, type="TRADE", usdcSize=10000 * .6 + 10000 * .03 * .6 * .4)]
    await w.ingest(normalize(f, "ws"))
    await asyncio.sleep(0.4)
    msg = tg.sent[0]
    assert "⚡ CONVICTION" in msg and "TAKER 100%" in msg and "$72.00 fees" in msg
    assert "usually 10% taker" in msg


@pytest.mark.asyncio
async def test_maker_fill_and_taker_only_mode(tmp_path):
    c = cfg(tmp_path)
    st, tg, api = Store(c.db_path), FakeTG(), FakeAPI()
    st.add_manual(A, "a", {"taker_share": 0.1})
    w = Watcher(c, api, st, tg)
    w.reload_wallets()
    f = fill(A, 10000, 0.6, "0xmk")
    api.act[A] = [dict(f, type="TRADE", usdcSize=6000)]
    await w.ingest(normalize(f, "ws"))
    await asyncio.sleep(0.4)
    assert "MAKER" in tg.sent[0] and "CONVICTION" not in tg.sent[0]
    st.set("taker_only", True)
    f2 = fill(A, 10000, 0.6, "0xmk2")
    api.act[A] = [dict(f2, type="TRADE", usdcSize=6000)]
    await w.ingest(normalize(f2, "ws"))
    await asyncio.sleep(0.4)
    assert len(tg.sent) == 1


@pytest.mark.asyncio
async def test_slice_parsing_old_and_new_formats(tmp_path):
    calls = []

    class Rec(FakeAPI):
        async def leaderboard(self, period, category, limit, offset, order="PNL"):
            calls.append((period, category, order))
            return []

    c = cfg(tmp_path, lb_slices=["MONTH:OVERALL", "ALL:SPORTS:VOL", "WEEK", "ALL:SPORTS:junk"])
    from polysharp.selector import gather_candidates
    await gather_candidates(Rec(), c)
    assert calls == [("MONTH", "OVERALL", "PNL"), ("ALL", "SPORTS", "VOL"),
                     ("WEEK", "OVERALL", "PNL"), ("ALL", "SPORTS", "PNL")]
