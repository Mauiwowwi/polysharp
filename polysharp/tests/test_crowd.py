"""Agree / oppose / hedge / live-gating behaviour of the alert pipeline."""
import asyncio
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from polysharp.config import Config
from polysharp.markets import Markets
from polysharp.store import Store
from polysharp.watcher import Watcher, normalize

NOW = int(time.time())
A, B, C, D = ("0x" + ch * 40 for ch in "abcd")
PRE = "0x" + "p" * 64        # game starts in 3h
LIVE = "0x" + "l" * 64       # game started 1h ago
YES, NO = "111", "222"       # outcome tokens of whichever market


def iso(ts):
    return datetime.fromtimestamp(ts, ZoneInfo("UTC")).isoformat()


class FakeAPI:
    def __init__(self):
        self.books = {}      # (wallet, cond) -> list of position rows

    def hold(self, wallet, cond, asset, outcome, size, avg):
        self.books.setdefault((wallet, cond), []).append(
            {"asset": asset, "outcome": outcome, "size": size, "avgPrice": avg,
             "initialValue": size * avg, "currentValue": size * avg})

    async def positions(self, user, market=None, redeemable=None):
        return self.books.get((user, market), [])

    async def activity(self, user, start=None, limit=100):
        return []

    async def gamma_markets(self, cids):
        out = []
        for c in cids:
            gs = NOW + 3 * 3600 if c == PRE else NOW - 3600
            out.append({"conditionId": c, "feeType": "sports_fees_v3", "gameStartTime": iso(gs),
                        "events": [{"slug": "mlb-lad-sd-2026-10-01"}]})
        return out


class TG:
    def __init__(self):
        self.sent = []

    async def send(self, t, chat_id=None):
        self.sent.append(t)

    async def send_alert(self, text, sport="other", short=None):
        self.sent.append(text)


def setup(tmp_path, **kw):
    c = Config()
    c.db_path = str(tmp_path / "t.db")
    c.tg_token = c.tg_chat_id = "x"
    c.bundle_seconds = 0.15
    c.fee_retry_seconds = 0
    c.min_alert_usd = 1000
    for k, v in kw.items():
        setattr(c, k, v)
    api, st, tg = FakeAPI(), Store(c.db_path), TG()
    for addr, name in ((A, "alpha"), (B, "beta"), (C, "gamma"), (D, "delta")):
        st.add_manual(addr, name)
    w = Watcher(c, api, st, tg, Markets(api, st))
    w.reload_wallets()
    return c, api, st, tg, w


def fill(wallet, cond, asset, outcome, size, price, tx, side="BUY", ts=None):
    return normalize({"proxyWallet": wallet, "conditionId": cond, "asset": asset, "outcome": outcome,
                      "size": size, "price": price, "side": side, "transactionHash": tx,
                      "title": "Dodgers vs. Padres", "slug": "s", "eventSlug": "mlb-lad-sd",
                      "timestamp": ts or NOW}, "ws")


async def flush():
    await asyncio.sleep(0.35)


@pytest.mark.asyncio
async def test_live_buy_never_alerts_and_isnt_indexed(tmp_path):
    c, api, st, tg, w = setup(tmp_path)
    api.hold(A, LIVE, YES, "Dodgers", 10000, 0.5)
    await w.ingest(fill(A, LIVE, YES, "Dodgers", 10000, 0.5, "0x1"))
    await flush()
    assert tg.sent == [] and w.skipped_filtered == 1
    assert st.wallets_in_market(LIVE, 0) == []      # live fill can't feed agree/oppose


@pytest.mark.asyncio
async def test_agree_and_oppose(tmp_path):
    c, api, st, tg, w = setup(tmp_path)
    # beta already on Dodgers, gamma on Padres (both pre-game)
    api.hold(B, PRE, YES, "Dodgers", 8000, 0.48)
    api.hold(C, PRE, NO, "Padres", 6000, 0.52)
    await w.ingest(fill(B, PRE, YES, "Dodgers", 8000, 0.48, "0xb"))
    await w.ingest(fill(C, PRE, NO, "Padres", 6000, 0.52, "0xc"))
    await flush()
    tg.sent.clear()
    api.hold(A, PRE, YES, "Dodgers", 10000, 0.5)
    await w.ingest(fill(A, PRE, YES, "Dodgers", 10000, 0.5, "0xa"))
    await flush()
    msg = tg.sent[0]
    assert "🤝 AGREES ×1" in msg and "⚔️ OPPOSES ×1" in msg
    assert "beta also on Dodgers: 8,000 sh @ 0.480" in msg
    assert "gamma is on <b>Padres</b>: 6,000 sh @ 0.520" in msg
    assert "starts in 2h 59m" in msg or "starts in 3h 00m" in msg


@pytest.mark.asyncio
async def test_consensus_message_at_three(tmp_path):
    c, api, st, tg, w = setup(tmp_path)
    for i, who in enumerate((B, C)):
        api.hold(who, PRE, YES, "Dodgers", 5000, 0.5)
        await w.ingest(fill(who, PRE, YES, "Dodgers", 5000, 0.5, f"0x{i}"))
    await flush()
    assert not any("CONSENSUS" in m for m in tg.sent)   # 2 isn't enough
    api.hold(A, PRE, YES, "Dodgers", 5000, 0.5)
    await w.ingest(fill(A, PRE, YES, "Dodgers", 5000, 0.5, "0xa"))
    await flush()
    cons = [m for m in tg.sent if "CONSENSUS" in m]
    assert len(cons) == 1 and "3 of your wallets" in cons[0]


@pytest.mark.asyncio
async def test_hedge_pregame(tmp_path):
    c, api, st, tg, w = setup(tmp_path)
    api.hold(A, PRE, YES, "Dodgers", 10000, 0.5)
    await w.ingest(fill(A, PRE, YES, "Dodgers", 10000, 0.5, "0x1"))
    await flush()
    api.hold(A, PRE, NO, "Padres", 6000, 0.45)
    await w.ingest(fill(A, PRE, NO, "Padres", 6000, 0.45, "0x2"))
    await flush()
    h = tg.sent[-1]
    assert "🛡️ HEDGE BUY" in h and "Already holds <b>Dodgers</b> 10,000 sh" in h
    # cost 5000 + 2700 = 7700 -> Padres wins 6000-7700 = -1700, Dodgers wins 10000-7700 = +2300
    assert "Padres wins -1,700" in h and "Dodgers wins +2,300" in h
    assert "OPPOSES" not in h and "CONVICTION" not in h


@pytest.mark.asyncio
async def test_live_exit_on_tailed_position_only(tmp_path):
    c, api, st, tg, w = setup(tmp_path)
    st.mark_alerted(A, LIVE, YES)                      # we alerted A's Dodgers buy pre-game
    api.hold(A, LIVE, YES, "Dodgers", 2000, 0.5)       # still holds a bit after selling
    await w.ingest(fill(A, LIVE, YES, "Dodgers", 8000, 0.7, "0xs", side="SELL"))
    await w.ingest(fill(B, LIVE, YES, "Dodgers", 8000, 0.7, "0xt", side="SELL"))  # never tailed
    await flush()
    assert len(tg.sent) == 1
    m = tg.sent[0]
    assert "🔴 LIVE 📉 TRIM SELL" in m and "Getting off a position we alerted you on" in m
    st.set("live_hedges", False)                        # /livehedges off
    await w.ingest(fill(A, LIVE, YES, "Dodgers", 2000, 0.7, "0xs2", side="SELL"))
    await flush()
    assert len(tg.sent) == 1


@pytest.mark.asyncio
async def test_live_hedge_buy_on_tailed_position(tmp_path):
    c, api, st, tg, w = setup(tmp_path)
    st.mark_alerted(A, LIVE, YES)
    api.hold(A, LIVE, YES, "Dodgers", 10000, 0.5)
    api.hold(A, LIVE, NO, "Padres", 9000, 0.3)
    await w.ingest(fill(A, LIVE, NO, "Padres", 9000, 0.3, "0xh"))
    # a live non-hedge buy by the same wallet in another live market is still dropped
    api.hold(A, "0x" + "q" * 64, "333", "Over", 9000, 0.5)
    await w.ingest(fill(A, "0x" + "q" * 64, "333", "Over", 9000, 0.5, "0xo"))
    await flush()
    assert len(tg.sent) == 1 and "🔴 LIVE 🛡️ HEDGE BUY" in tg.sent[0]


# --- conviction score / tiers ------------------------------------------------
@pytest.mark.asyncio
async def test_conviction_tiers_and_tier_filter(tmp_path):
    c, api, st, tg, w = setup(tmp_path)
    w.wallets[A]["stats"] = {"avg_bet": 2000.0}
    # A volumes out (10k sh @0.5 = $5k = 2.5x usual), buys twice, beta agrees, nobody opposes
    api.hold(B, PRE, YES, "Dodgers", 8000, 0.48)
    await w.ingest(fill(B, PRE, YES, "Dodgers", 8000, 0.48, "0xb"))
    await flush()
    tg.sent.clear()
    api.hold(A, PRE, YES, "Dodgers", 4000, 0.5)
    await w.ingest(fill(A, PRE, YES, "Dodgers", 4000, 0.5, "0xa1", ts=NOW - 3600))
    await flush()
    first = tg.sent[-1]
    # $2k exposure = 1.0x usual (+0), 1 agree (+1) -> 1 = LOW
    assert first.startswith("<b>▫️ LOW") and "Conviction +1" in first and "no opposition" in first
    api.books[(A, PRE)] = []
    api.hold(A, PRE, YES, "Dodgers", 14000, 0.5)     # now $7k exposure = 3.5x
    await w.ingest(fill(A, PRE, YES, "Dodgers", 10000, 0.5, "0xa2"))
    await flush()
    second = tg.sent[-1]
    # 3.5x (+2) + 2nd buy (+1) + 1 agree (+1) = 4 -> HIGH
    assert second.startswith("<b>🔥 HIGH") and "3.5× their usual bet" in second
    assert "buy #2 on this side in 1h" in second

    # opposition drags it down, and /tier high hides it
    st.set("min_tier", "high")
    api.hold(C, PRE, NO, "Padres", 9000, 0.5)
    await w.ingest(fill(C, PRE, NO, "Padres", 9000, 0.5, "0xc"))   # gamma: no avg -> LOW, hidden
    await flush()
    n = len(tg.sent)
    api.books[(A, PRE)] = []
    api.hold(A, PRE, YES, "Dodgers", 16000, 0.5)
    await w.ingest(fill(A, PRE, YES, "Dodgers", 2000, 0.5, "0xa3"))
    await flush()
    # 4x (+2) + 3rd buy (+1) + agree (+1) + oppose (-2) = 2 -> MED -> hidden at /tier high
    assert len(tg.sent) == n and w.below_tier == 2


def test_win_sample_aligns_windows():
    from polysharp.selector import win_sample
    closed = [{"asset": str(i), "realizedPnl": 1 if i < 6 else -1, "timestamp": NOW - 86400 + i}
              for i in range(10)]
    dead = [{"asset": "x", "curPrice": 0, "endDate": iso(NOW - 3600)},          # in window -> loss
            {"asset": "y", "curPrice": 0, "endDate": iso(NOW - 90 * 86400)},    # old -> ignored
            {"asset": "1", "curPrice": 0, "endDate": iso(NOW - 3600)},          # already settled
            {"asset": "z", "curPrice": 1, "endDate": iso(NOW - 3600)}]          # a winner
    r = win_sample(closed, dead)
    assert r["win_n"] == 11 and abs(r["win_rate"] - 6 / 11) < 1e-4 and r["win_days"] == 1.0


def test_pick_label_and_american_odds():
    from polysharp.watcher import american, pick_label
    assert pick_label("Spread: Browns (-3.5)", "Steelers") == "Steelers +3.5"
    assert pick_label("Spread: Browns (-3.5)", "Browns") == "Browns -3.5"
    assert pick_label("Eagles vs. Titans: O/U 39.5", "Under") == "Under 39.5"
    assert pick_label("Dodgers vs. Padres", "Dodgers") == "Dodgers"
    assert american(0.71) == "-245" and american(0.40) == "+150" and american(0.5) == "-100"


@pytest.mark.asyncio
async def test_alert_layout_order(tmp_path):
    c, api, st, tg, w = setup(tmp_path)
    w.wallets[A]["name"] = "HomeRunHazard"
    api.hold(A, PRE, YES, "Steelers", 25980, 0.71)

    def spread_fill():
        f = fill(A, PRE, YES, "Steelers", 13004, 0.71, "0xs")
        f["title"] = "Spread: Browns (-3.5)"
        return f
    await w.ingest(spread_fill())
    await flush()
    lines = tg.sent[0].split("\n")
    assert "👤" in lines[0] and "HomeRunHazard</a>" in lines[0]           # name on the header line
    assert lines[1] == ""
    assert "Spread: Browns (-3.5)" in lines[2]                             # market
    assert "<b>Steelers +3.5 @ 0.710 (-245)</b> (13,004 sh)" in lines[3]   # the actual pick
    assert "sports 1M" not in tg.sent[0] and "avg bet" not in tg.sent[0]   # no stats clutter
    assert lines[-1].startswith("📦 Now holds 25,980 sh")                  # holdings last
    assert not any("after fill" in x for x in lines)


@pytest.mark.asyncio
async def test_dust_on_other_side_is_not_a_hedge(tmp_path):
    """UpTheBlues: 14 sh of Under ($7) left over, buys 2,152 sh of Over -> plain buy."""
    c, api, st, tg, w = setup(tmp_path)
    api.hold(A, PRE, NO, "Under", 14, 0.5)
    api.hold(A, PRE, YES, "Over", 3813, 0.508)

    def f():
        x = fill(A, PRE, YES, "Over", 2152, 0.51, "0xd")
        x["title"] = "Germany vs. Serbia: O/U 3.5"
        return x
    await w.ingest(f())
    await flush()
    m = tg.sent[0]
    assert "HEDGE" not in m and "Already holds" not in m and "Net after hedge" not in m
    assert "🟢 ADD BUY" in m and "<b>Over 3.5 @ 0.510 (-104)</b>" in m
    assert "Conviction" in m        # scored like any normal buy


@pytest.mark.asyncio
async def test_flip_when_new_side_outweighs_old(tmp_path):
    c, api, st, tg, w = setup(tmp_path)
    api.hold(A, PRE, NO, "Padres", 6000, 0.5)          # $3,000 on Padres
    api.hold(A, PRE, YES, "Dodgers", 20000, 0.5)       # now $10,000 on Dodgers
    await w.ingest(fill(A, PRE, YES, "Dodgers", 20000, 0.5, "0xf"))
    await flush()
    m = tg.sent[0]
    assert "⚖️ BOTH SIDES BUY" in m and "Also holds <b>Padres</b> 6,000 sh" in m and "FLIP" not in m
    assert "Conviction" in m and "HEDGE" not in m


def test_money_format_on_wallet_line(tmp_path):
    c, api, st, tg, w = setup(tmp_path)
    w.wallets[A]["stats"] = {"pnl_m": 1116000, "pnl_all": 447000, "pnl_overall": 447000}
    line = w._wallet_line(A)
    assert "1M +$1.12M" in line and "all +$447K" in line


def test_paid_fees_lead_the_header(tmp_path):
    c, api, st, tg, w = setup(tmp_path)
    w.wallets[A]["name"] = "HomeRunHazard"
    t = {"wallet": A, "side": "BUY", "title": "KK Crvena Zvezda vs. Anadolu Efes", "outcome": "KK Crvena Zvezda",
         "event_slug": "euroleague-x", "slug": "", "ts": time.time()}
    paid = {"taker_share": 1.0, "fees": 39.89, "fee_pct": 0.0215, "coverage": 1.0}
    maker = {"taker_share": 0.0, "fees": 0.0, "fee_pct": 0.0, "coverage": 1.0}
    m1 = w.format_alert(t, [{}], 1895, 3255, 0.57, None, paid)
    m2 = w.format_alert(t, [{}], 1895, 3255, 0.57, None, maker)
    assert m1.startswith("<b>💸 PAID $40 · 🟢 ADD BUY · $1,895</b>") and "<b>TAKER 100%</b>" in m1
    assert m2.startswith("<b>🟢 ADD BUY · $1,895</b>") and "🧱 MAKER" in m2


def test_paid_fees_add_conviction_points(tmp_path):
    c, api, st, tg, w = setup(tmp_path)
    t = {"wallet": A, "asset": YES}
    base = w.conviction_score(t, 1000, None, False, [], [])
    paid = w.conviction_score(t, 1000, None, False, [], [], paid=True)
    ooc = w.conviction_score(t, 1000, None, True, [], [], paid=True)
    assert paid["pts"] == base["pts"] + 1 and "paid fees" in paid["why"]
    assert ooc["pts"] == base["pts"] + 2 and "out of character" in ooc["why"]


def test_soccer_yes_no_labels():
    from polysharp.watcher import pick_label
    assert pick_label("Will Azerbaijan win on 2026-10-01?", "Yes") == "Azerbaijan YES"
    assert pick_label("Will Norway win on 2026-10-01?", "No") == "Norway NO"
    assert pick_label("Will Germany vs. Serbia end in a draw?", "No") == "Draw NO (Germany vs Serbia)"
    assert pick_label("Will Germany vs. Serbia end in a draw?", "Yes") == "Draw YES (Germany vs Serbia)"
    assert pick_label("Exact Score: Germany 1 - 1 Serbia?", "No") == "Germany 1-1 Serbia NO"
    assert pick_label("Germany vs. Serbia: Both Teams to Score", "Yes") == "BTTS YES"
    assert pick_label("Will Haaland score a goal?", "No") == "Haaland score a goal NO"
    # non Yes/No markets untouched
    assert pick_label("Spread: Browns (-3.5)", "Steelers") == "Steelers +3.5"


def test_soccer_no_side_in_alert(tmp_path):
    c, api, st, tg, w = setup(tmp_path)
    t = {"wallet": A, "side": "BUY", "title": "Will Athletic Club win on 2026-09-16?", "outcome": "No",
         "event_slug": "lal-ath-lev", "slug": "", "ts": time.time()}
    m = w.format_alert(t, [{}], 13254, 23377, 0.567, None)
    assert "<b>Athletic Club NO @ 0.567 (-131)</b>" in m


@pytest.mark.asyncio
async def test_combo_fill_without_market_is_dropped(tmp_path):
    """UpTheBlues 12:27: fill with no title/outcome/market -> was spammed as a giant HEDGE."""
    c, api, st, tg, w = setup(tmp_path)
    for i in range(500):                                   # their whole account
        api.hold(A, "", f"x{i}", "YES", 50000, 0.1)
    raw = {"proxyWallet": A, "conditionId": "", "asset": "999", "outcome": "", "title": "",
           "size": 940, "price": 0.967, "side": "BUY", "transactionHash": "0xcombo", "timestamp": NOW}
    await w.ingest(normalize(raw, "ws"))
    await flush()
    assert tg.sent == [] and w.skipped_filtered == 1


@pytest.mark.asyncio
async def test_position_lookup_never_lists_whole_account(tmp_path):
    c, api, st, tg, w = setup(tmp_path)

    async def everything(user, market=None, redeemable=None, sort=None):
        # simulate the API ignoring the market filter and returning the whole account
        return [{"asset": f"x{i}", "conditionId": f"0x{i}", "outcome": "YES", "size": 50000,
                 "avgPrice": 0.1, "initialValue": 5000} for i in range(500)]
    api.positions = everything
    await w.ingest(fill(A, PRE, YES, "Dodgers", 10000, 0.5, "0xn"))
    await flush()
    m = tg.sent[0]
    assert "HEDGE" not in m and "Already holds" not in m and m.count("\n") < 15



@pytest.mark.asyncio
async def test_both_sides_bottom_line_shows_each_side(tmp_path):
    c, api, st, tg, w = setup(tmp_path)
    api.hold(A, PRE, NO, "No", 244, 0.512)              # $125 on NO
    api.hold(A, PRE, YES, "Yes", 2098, 0.5)              # $1,049 on YES after this buy
    f = fill(A, PRE, YES, "Yes", 2098, 0.5, "0xh1")
    f["title"] = "Will Haiti win on 2026-10-01?"
    await w.ingest(f)
    await flush()
    last = tg.sent[0].split("\n")[-1]
    assert last == ("📦 <b>Haiti YES</b> $1,049 (2,098 sh) · <b>Haiti NO</b> $125 (244 sh)"
                    " → bigger on <b>Haiti YES</b>")
    assert "Now holds" not in tg.sent[0]


@pytest.mark.asyncio
async def test_hedge_bottom_line_bigger_on_old_side(tmp_path):
    c, api, st, tg, w = setup(tmp_path)
    api.hold(A, PRE, YES, "Dodgers", 10000, 0.5)         # $5,000
    api.hold(A, PRE, NO, "Padres", 6000, 0.45)           # $2,700 (this buy)
    await w.ingest(fill(A, PRE, NO, "Padres", 6000, 0.45, "0xh2"))
    await flush()
    last = tg.sent[0].split("\n")[-1]
    assert last.startswith("📦 <b>Dodgers</b> $5,000") and last.endswith("bigger on <b>Dodgers</b>")
