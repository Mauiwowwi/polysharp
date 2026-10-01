import asyncio
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from polysharp.config import Config
from polysharp.markets import Markets, classify_gamma, is_live, parse_ts
from polysharp.selector import suggest
from polysharp.store import Store
from polysharp.watcher import Watcher, normalize

NOW = int(time.time())
SHARP, LIVE, WAR, COLD, SMALL, TRACKED = ("0x" + c * 40 for c in "123456")


def w(tag):  # condition id helper
    return "0x" + tag.ljust(64, "0")


# Market universe: sports games (with start times), a sports future, war markets
GAMES = {w(f"g{i}"): NOW - 86400 * (i % 5 + 1) for i in range(300)}   # games in the past
UPCOMING = w("up1")
FUTURE = w("fut")
WARS = [w(f"war{i}") for i in range(300)]
SLUG_ONLY = w("slugonly")   # gamma doesn't know it; slug fallback says NFL


def gamma_row(cid):
    if cid in GAMES:
        return {"conditionId": cid, "feeType": "sports_fees_v3", "sportsMarketType": "moneyline",
                "gameStartTime": datetime.fromtimestamp(GAMES[cid], ZoneInfo("UTC"))
                .strftime("%Y-%m-%dT%H:%M:%S+00"), "events": [{"slug": f"mlb-nyy-bos-{cid[-6:]}"}]}
    if cid == UPCOMING:
        return {"conditionId": cid, "feeType": "sports_fees_v3",
                "gameStartTime": datetime.fromtimestamp(NOW + 3 * 3600 + 600, ZoneInfo("UTC"))
                .isoformat(), "events": [{"slug": "nfl-kc-buf-2026-10-04"}]}
    if cid == FUTURE:
        return {"conditionId": cid, "feeType": "sports_fees_v2", "events": [{"slug": "world-series-winner"}]}
    if cid in WARS:
        return {"conditionId": cid, "feeType": None, "events": [{"slug": "israel-strike-iran"}]}
    return None


def closed_rows(conds, n, pnl_each, cost_each=5000, ts=NOW - 3600):
    return [{"conditionId": conds[i % len(conds)], "totalBought": cost_each / 0.5, "avgPrice": 0.5,
             "realizedPnl": pnl_each if i % 5 else -pnl_each / 2, "timestamp": ts,
             "eventSlug": "x"} for i in range(n)]


def buys(conds, when, n=40, ts_base=None):
    out = []
    for i in range(n):
        cid = conds[i % len(conds)]
        gs = GAMES.get(cid, NOW)
        ts = (gs - 1800) if when == "pre" else (gs + 1800)
        if ts_base:
            ts = ts_base
        out.append({"type": "TRADE", "side": "BUY", "conditionId": cid, "size": 1000, "price": 0.5,
                    "usdcSize": 500, "timestamp": ts, "eventSlug": "x", "name": "nm"})
    return out


class FakeAPI:
    def __init__(self):
        self.gamma_calls = 0
        g = list(GAMES)
        self.data = {
            SHARP: (closed_rows(g, 220, 700), buys(g, "pre")),        # sports, pre-game, active
            LIVE: (closed_rows(g, 220, 700), buys(g, "live")),        # sports but in-game
            WAR: (closed_rows(WARS, 220, 700), buys(WARS, "pre")),    # denizz-style
            COLD: (closed_rows(g, 220, 700), buys(g, "pre", ts_base=NOW - 20 * 86400)),
            SMALL: (closed_rows(g, 46, 3000), buys(g, "pre")),        # btystu-style
            TRACKED: (closed_rows(g, 220, 700), buys(g, "pre")),
        }

    async def leaderboard(self, period, category, limit, offset, order="PNL"):
        if offset:
            return []
        return [{"proxyWallet": a, "userName": n, "rank": str(i + 1), "vol": 2e6}
                for i, (a, n) in enumerate([(SHARP, "sharp"), (LIVE, "liveguy"), (WAR, "denizz"),
                                            (COLD, "coldguy"), (SMALL, "btystu"), (TRACKED, "mine")])]

    async def closed_positions(self, user, max_rows=500):
        return self.data[user][0]

    async def positions(self, user, market=None, redeemable=None):
        return []

    async def activity(self, user, start=None, limit=100):
        return self.data.get(user, ([], []))[1]

    async def gamma_markets(self, cids):
        self.gamma_calls += 1
        return [r for r in (gamma_row(c) for c in cids) if r]


def cfg(tmp_path, **kw):
    c = Config()
    c.db_path = str(tmp_path / "t.db")
    c.tg_token = c.tg_chat_id = "x"
    c.lb_slices = ["MONTH:SPORTS:PNL"]
    c.bundle_seconds = 0.2
    c.fee_retry_seconds = 0
    c.min_alert_usd = 1000
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def test_market_classification():
    g = classify_gamma(gamma_row(list(GAMES)[0]))
    assert g["sports"] and g["league"] == "mlb" and g["game_start"] == list(GAMES.values())[0]
    assert classify_gamma(gamma_row(FUTURE))["sports"]            # futures count as sports
    assert not classify_gamma(gamma_row(WARS[0]))["sports"]
    assert parse_ts("2026-09-30T14:05:00+00") == datetime(2026, 9, 30, 14, 5, tzinfo=ZoneInfo("UTC")).timestamp()
    assert is_live({"game_start": 1000}, 1000 + 121) and not is_live({"game_start": 1000}, 1100)


@pytest.mark.asyncio
async def test_slug_fallback_and_cache(tmp_path):
    api, st = FakeAPI(), Store(str(tmp_path / "m.db"))
    mk = Markets(api, st)
    m = await mk.get({SLUG_ONLY: "nfl-kc-buf-2026-10-04", WARS[0]: "israel-strike-iran"})
    assert m[SLUG_ONLY]["sports"] and m[SLUG_ONLY]["league"] == "nfl"
    assert not m[WARS[0]]["sports"]
    calls = api.gamma_calls
    await mk.get({WARS[0]: "x"})
    assert api.gamma_calls == calls  # cached


@pytest.mark.asyncio
async def test_suggest_keeps_only_active_pregame_sports_volume(tmp_path):
    c = cfg(tmp_path)
    api, st = FakeAPI(), Store(c.db_path)
    picks, summary = await suggest(api, Markets(api, st), c, exclude={TRACKED})
    assert [p["address"] for p in picks] == [SHARP]
    s = picks[0]["stats"]
    assert s["sports_share"] == 1.0 and s["live_share"] == 0.0 and s["n"] == 220
    assert s["leagues"] == ["MLB"]
    f = summary["fails"]
    assert f["live bettor"] == 1 and f["not sports"] == 1 and f["inactive"] == 1
    assert f["volume/ROI"] == 1 and f["already tracked/skipped"] == 1


@pytest.mark.asyncio
async def test_alert_filters_non_sports_and_live(tmp_path):
    c = cfg(tmp_path)
    api, st = FakeAPI(), Store(c.db_path)

    class TG:
        sent = []

        async def send(self, t, chat_id=None):
            self.sent.append(t)

    tg = TG()
    st.add_manual(SHARP, "sharp", {})
    wt = Watcher(c, api, st, tg, Markets(api, st))
    wt.reload_wallets()

    def fill(cid, tx, ts):
        return {"proxyWallet": SHARP, "size": 5000, "price": 0.5, "side": "BUY", "asset": tx,
                "conditionId": cid, "outcome": "Yes", "title": "T", "slug": "s", "eventSlug": "e",
                "transactionHash": tx, "timestamp": ts}

    past_game = list(GAMES)[0]
    await wt.ingest(normalize(fill(WARS[0], "0xa", NOW), "ws"))                          # war
    await wt.ingest(normalize(fill(past_game, "0xb", GAMES[past_game] + 3600), "ws"))    # live
    await wt.ingest(normalize(fill(UPCOMING, "0xc", NOW), "ws"))                          # pre-game
    await asyncio.sleep(0.4)
    assert len(tg.sent) == 1 and wt.skipped_filtered == 2
    assert "[NFL]" in tg.sent[0] and "starts in 3h 10m" in tg.sent[0]


def test_next_run_is_local_morning():
    from polysharp.main import next_run
    utc = ZoneInfo("UTC")
    # 2026-09-30 10:00 UTC = 07:00 ADT -> same day 08:00 ADT = 11:00 UTC
    assert next_run(datetime(2026, 9, 30, 10, 0, tzinfo=utc), "08:00", "America/Halifax") == \
        datetime(2026, 9, 30, 11, 0, tzinfo=utc)
    # past 08:00 local -> tomorrow
    assert next_run(datetime(2026, 9, 30, 12, 0, tzinfo=utc), "08:00", "America/Halifax") == \
        datetime(2026, 10, 1, 11, 0, tzinfo=utc)


def test_store_skip_and_manual(tmp_path):
    st = Store(str(tmp_path / "s.db"))
    st.add_manual(SHARP, "a", {"n": 1})
    st.db.execute("INSERT INTO wallets VALUES (?,?,?,?,1,?)", (LIVE, "auto1", "auto", "{}", 0))
    st.drop_auto_wallets()
    assert set(st.active_wallets()) == {SHARP}
    st.skip(WAR, 30)
    assert WAR in st.skipped()
    st.add_manual(WAR, "w")          # adding un-skips
    assert WAR not in st.skipped()
    assert st.remove(SHARP) and not st.remove(SHARP)
