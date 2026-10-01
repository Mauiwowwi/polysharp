import asyncio
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from polysharp.config import Config
from polysharp.markets import Markets, classify_gamma, is_live, parse_ts


def iso(ts):
    return datetime.fromtimestamp(ts, ZoneInfo('UTC')).isoformat()
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

    PERF = {  # (pnl_w, pnl_m, pnl_all, vol_m, vol_all)
        SHARP: (40e3, 120e3, 900e3, 8e6, 60e6),
        LIVE: (40e3, 120e3, 900e3, 8e6, 60e6),
        WAR: (40e3, 120e3, 900e3, 8e6, 60e6),
        COLD: (0, 120e3, 900e3, 8e6, 60e6),
        SMALL: (5e3, 20e3, 155e3, 150e3, 362e3),     # btystu: tiny volume
        TRACKED: (40e3, 120e3, 900e3, 8e6, 60e6),
    }

    async def user_pnl(self, user, period="MONTH", category="SPORTS"):
        w, m, a, vm, va = self.PERF[user]
        pnl = {"WEEK": w, "MONTH": m, "ALL": a}[period]
        vol = {"WEEK": vm / 4, "MONTH": vm, "ALL": va}[period]
        return {"pnl": pnl, "vol": vol, "rank": 10}

    async def traded_count(self, user):
        return 46 if user == SMALL else 5000

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
    assert s["sports_share"] == 1.0 and s["live_share"] == 0.0
    assert s["pnl_m"] == 120e3 and s["margin_all"] == 0.015
    assert s["leagues"] == ["MLB"]
    f = summary["fails"]
    assert f["live bettor"] == 1 and f["not sports"] == 1 and f["inactive"] == 1
    assert f["too few/small bets"] == 1 and f["already tracked/skipped"] == 1   # btystu: 46 predictions


@pytest.mark.asyncio
async def test_alert_filters_non_sports_and_live(tmp_path):
    c = cfg(tmp_path)
    api, st = FakeAPI(), Store(c.db_path)

    class TG:
        sent = []

        async def send(self, t, chat_id=None):
            self.sent.append(t)

        async def send_alert(self, t, sport="other"):
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


@pytest.mark.asyncio
async def test_homerunhazard_regression(tmp_path):
    """High-frequency winner: Polymarket's own sports P&L must drive the verdict.

    Real numbers 2026-10-01: 1M +$349K on $100.4M, all-time +$1.73M on $380.3M.
    Old sample-based math showed ROI -46% (1.5 days of settled bets vs 5 months
    of unredeemed losers). The new path never touches those samples.
    """
    from polysharp.selector import deep_eval, passes

    class API(FakeAPI):
        async def user_pnl(self, user, period="MONTH", category="SPORTS"):
            return {"WEEK": {"pnl": 544e3, "vol": 25e6, "rank": 5},
                    "MONTH": {"pnl": 349281.21, "vol": 100430072.68, "rank": 26},
                    "ALL": {"pnl": 1732524.54, "vol": 380277058.39, "rank": 62}}[period]

        async def traded_count(self, user):
            return 32454

        async def closed_positions(self, *a, **k):
            # 1.5 days of settled bets, 60% winners
            return [{"asset": str(i), "realizedPnl": 100 if i % 5 < 3 else -100,
                     "timestamp": NOW - 36 * 3600 + i} for i in range(1000)]

        async def positions(self, user, market=None, redeemable=None):
            # 99 unredeemed losers going back to May: only those ending in the window count
            return [{"asset": f"d{i}", "curPrice": 0, "initialValue": 6000,
                     "endDate": iso(NOW - (i * 86400 * 1.5))} for i in range(99)]

    c = cfg(tmp_path)
    api = API()
    s = await deep_eval(api, Markets(api, Store(c.db_path)), c, SHARP)
    assert s["pnl_m"] == 349281.21 and s["pnl_all"] == 1732524.54
    assert abs(s["margin_m"] - 0.00348) < 1e-5 and abs(s["margin_all"] - 0.00456) < 1e-5
    assert s["predictions"] == 32454 and abs(s["avg_bet"] - 380277058.39 / 32454) < 1
    # window = 1.5 days -> only the losers that ended in the last ~2.5 days count (2 of 99)
    assert s["win_n"] == 1002 and abs(s["win_rate"] - 600 / 1002) < 1e-3 and s["win_days"] == 1.5
    ok, why = passes(s, c)
    assert ok, why          # profitable, big volume, margin 0.46% >= 0.2% floor


def test_feed_wallet_card():
    import re
    from polysharp.main import fmt_feed_wallet
    s = {"pnl_w": 615e3, "pnl_m": 1.12e6, "pnl_all": 431e3, "pnl_overall": 447e3, "predictions": 135618,
         "avg_bet": 2100, "win_rate": 0.69, "margin_all": 0.0015, "taker_share": 1.0,
         "live_share": 0.54, "leagues": ["ATP", "WTA"], "days_since_trade": 9.0}
    card = re.sub(r"<[^>]+>", "", fmt_feed_wallet("0xabc", {"name": "UpTheBlues", "stats": s}))
    assert "P&L 1W +$615K · 1M +$1.12M · All +$447K" in card
    assert "135,618 preds · avg $2.1K · win 69%" in card and "taker 100%" in card
    assert "⚠️ cold 9d" in card and "⚠️ 54% live" in card
    assert "no stats yet" in fmt_feed_wallet("0xabc", {"name": "x", "stats": {"n": 5}})
