import asyncio
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

NOW = int(time.time())
A, B, C = ("0x" + ch * 40 for ch in "abc")


def iso(ts):
    return datetime.fromtimestamp(ts, ZoneInfo("UTC")).isoformat()


def pos(i, title, outcome, cost, avg, cur, slug, redeemable=False):
    size = cost / avg
    return {"conditionId": f"0x{i:064x}", "asset": str(i), "title": title, "outcome": outcome, "size": size,
            "avgPrice": avg, "initialValue": cost, "curPrice": cur, "cashPnl": size * cur - cost,
            "redeemable": redeemable, "eventSlug": slug}


class API:
    async def positions(self, user, market=None, redeemable=None, sort=None):
        rows = [pos(1, "Spread: Browns (-3.5)", "Steelers", 27868, 0.7098, 0.705, "nfl-pit-cle-2026-10-02"),
                pos(2, "Baltimore Orioles vs. Colorado Rockies: O/U 11.5", "Over", 61742, 0.47, 0,
                    "mlb-bal-col-2026-09-02", redeemable=True),                       # resolved loser
                pos(3, "Will X win the election?", "Yes", 90000, 0.5, 0.6, "election-x")]  # non-sports
        rows += [pos(10 + i, f"Dodgers vs. Padres {i}", "Dodgers", 1000 * (i + 1), 0.5, 0.52,
                     f"mlb-lad-sd-{i}") for i in range(12)]
        return rows

    async def activity(self, user, start=None, limit=100, market=None):
        n = int(market, 16)
        return [{"asset": str(n), "side": "BUY"}] * (102 if n == 1 else 3) + [{"asset": "other", "side": "BUY"}]

    async def gamma_markets(self, cids):
        out = []
        for c in cids:
            n = int(c, 16)
            if n == 3:
                out.append({"conditionId": c, "feeType": None, "events": [{"slug": "election-x"}]})
            elif n == 1:
                out.append({"conditionId": c, "feeType": "sports_fees_v3", "gameStartTime": iso(NOW + 3600),
                            "events": [{"slug": "nfl-pit-cle-2026-10-02"}]})
            else:
                out.append({"conditionId": c, "feeType": "sports_fees_v3", "gameStartTime": iso(NOW - 600),
                            "events": [{"slug": "mlb-lad-sd"}]})
        return out


class TG:
    def __init__(self):
        self.sent, self.handlers, self.callbacks = [], {}, {}

    async def send(self, t, chat_id=None, buttons=None):
        self.sent.append((t, buttons))

    def command(self, n):
        def d(f):
            self.handlers[n] = f
            return f
        return d

    def callback(self, p):
        def d(f):
            self.callbacks[p] = f
            return f
        return d


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "a.db"))
    from polysharp.main import App
    from polysharp.markets import Markets
    a = App()
    a.api = API()
    a.markets = Markets(a.api, a.store)
    a.tg = TG()
    a._register()
    for addr, name in ((A, "alwaysfade"), (B, "HomeRunHazard"), (C, "177-letsgo")):
        a.store.add_manual(addr, name, {})
    a.watcher.reload_wallets()
    return a


def strip(t):
    return re.sub(r"<[^>]+>", "", t)


@pytest.mark.asyncio
async def test_top10_no_args_shows_buttons(app):
    text, buttons = await app.tg.handlers["top10"]([])
    labels = [lbl for row in buttons for lbl, _ in row]
    assert labels == ["177-letsgo", "alwaysfade", "HomeRunHazard"]
    assert all(data.startswith("top:0x") for row in buttons for _, data in row)
    assert all(len(data) <= 64 for row in buttons for _, data in row)     # Telegram limit


@pytest.mark.asyncio
async def test_top10_by_name_and_button(app):
    out = strip(await app.tg.handlers["top10"](["alwaysfade"]))
    lines = out.split("\n")
    assert "alwaysfade — top 10 open positions" in lines[0]
    # card for the biggest open sports position
    i = lines.index(next(l for l in lines if l.startswith("1. ")))
    card = lines[i:i + 7]
    assert card[0].startswith("1. 🔴 $27,868")
    assert card[1] == "[NFL] Spread: Browns (-3.5)"
    assert card[2] == "Outcome: Steelers +3.5"
    assert card[3] == "Trades: 102 | Shares: 39,262"
    assert card[4] == "Cost: $27,868 | Payout: $39,262"
    assert card[5] == "💰 Profit if it wins: +$11,394"
    assert card[6] == "Avg: 71.0¢ (-245) | Last: 70.5¢ (-239)"
    assert "Orioles" not in out and "election" not in out                  # resolved / non-sports out
    assert "+1 non-sports hidden" in out and "13 open sports positions" in out
    assert sum(1 for l in lines if re.match(r"\d+\. [🟢🔴]", l)) == 10
    assert "⏳ 0h 59m" in out or "⏳ 1h 00m" in out
    assert "🔴 in-game" in out
    # same result through the button
    assert strip(await app.tg.callbacks["top"](A)) == out


@pytest.mark.asyncio
async def test_top10_partial_and_ambiguous(app):
    assert "HomeRunHazard — top" in strip(await app.tg.handlers["top10"](["homerun"]))
    text, buttons = await app.tg.handlers["top10"](["a"])     # matches several names
    assert text == "Which one?" and len(buttons) >= 2
    text, buttons = await app.tg.handlers["top10"](["nobody"])
    assert "No wallet in your feed matches" in text and buttons


@pytest.mark.asyncio
async def test_telegram_callback_dispatch():
    from polysharp.telegram import Telegram
    tg = Telegram("x", "42")
    sent, answered = [], []

    async def fake_send(t, chat_id=None, buttons=None):
        sent.append((t, chat_id))

    async def fake_answer(cb_id, text=None):
        answered.append(cb_id)
    tg.send, tg.answer_callback = fake_send, fake_answer

    @tg.callback("top")
    async def _cb(data):
        return f"got {data}"
    await tg._on_callback({"id": "1", "data": "top:0xabc", "message": {"chat": {"id": 42}}})
    await tg._on_callback({"id": "2", "data": "top:0xabc", "message": {"chat": {"id": 99}}})  # stranger
    assert sent == [("got 0xabc", "42")] and answered == ["1", "2"]
    await tg.close()
