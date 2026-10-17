"""Web dashboard: snapshot contents and key-gated HTTP serving."""
import asyncio
import json
import time

import pytest

from polysharp.dashboard import Dashboard, build_snapshot

NOW = 1_800_000_000
A, B, C = ("0x" + ch * 40 for ch in "abc")
EV = "cfb-uga-van-2026-10-03"


def pos(cond, asset, title, outcome, size, avg, cur=None, event=EV):
    return {"conditionId": cond, "asset": asset, "title": title, "outcome": outcome, "size": size,
            "avgPrice": avg, "curPrice": cur if cur is not None else avg, "initialValue": size * avg,
            "cashPnl": size * ((cur if cur is not None else avg) - avg), "eventSlug": event}


def meta(start, league="cfb", title="Georgia vs. Vanderbilt"):
    return {"sports": True, "game_start": start, "league": league, "event_title": title,
            "slug": EV}


def snapshot():
    m = {"c255": meta(NOW + 45 * 60), "c245": meta(NOW + 45 * 60), "c235": meta(NOW + 45 * 60),
         "ctot": meta(NOW + 45 * 60), "cnfl": meta(NOW + 5 * 3600, "nfl")}
    results = {
        A: ([pos("c255", "g", "Spread: Georgia (-25.5)", "Georgia", 12538, 0.48),
             pos("cnfl", "s", "Spread: Browns (-3.5)", "Steelers", 1000, 0.5, event="nfl-pit-cle")], m),
        B: ([pos("c255", "g", "Spread: Georgia (-25.5)", "Georgia", 12216, 0.48),
             pos("c245", "g2", "Spread: Georgia (-24.5)", "Georgia", 509, 0.52),
             pos("ctot", "o", "Georgia vs. Vanderbilt: O/U 52.5", "Over", 1000, 0.5)], m),
        C: ([pos("c235", "v", "Spread: Georgia (-23.5)", "Vanderbilt", 21190, 0.519),
             pos("c255", "v2", "Spread: Georgia (-25.5)", "Vanderbilt", 100, 0.5)], m),
    }
    names = {A: "HomeRunHazard", B: "UpTheBlues", C: "Kch-Temp"}
    return build_snapshot(results, names, crowd_min=250, now=NOW)


def test_snapshot_rows_sorted_with_cross_line_crowd():
    snap = snapshot()
    rows = snap["positions"]
    assert [r["cost"] for r in rows] == sorted((r["cost"] for r in rows), reverse=True)
    hrh = next(r for r in rows if r["account"] == "HomeRunHazard" and r["sport"] == "football"
               and r["league"] == "CFB")
    assert hrh["pick"] == "Georgia -25.5" and hrh["status"] == "upcoming"
    # UpTheBlues: same market + alt line, added up into one entry; totals bet is unrelated
    assert hrh["agree"] == [{"account": "UpTheBlues", "amount": round(12216 * 0.48 + 509 * 0.52, 2),
                             "picks": ["Georgia -25.5", "Georgia -24.5"]}]
    # Kch-Temp: Vanderbilt on two lines, summed (the $50 one alone would be under the floor)
    opp = hrh["oppose"][0]
    assert opp["account"] == "Kch-Temp" and opp["picks"] == ["Vanderbilt +23.5", "Vanderbilt +25.5"]
    assert hrh["oppose_total"] == round(21190 * 0.519 + 50, 2)
    assert not any(k.startswith("_") for k in hrh)
    assert {s["key"] for s in snap["sports"]} == {"football"}
    json.dumps(snap)                                   # JSON-ready


def test_small_crowd_is_ignored():
    m = {"c1": meta(NOW + 600)}
    results = {A: ([pos("c1", "x", "Spread: Georgia (-25.5)", "Georgia", 10000, 0.5)], m),
               B: ([pos("c1", "y", "Spread: Georgia (-25.5)", "Vanderbilt", 100, 0.5)], m)}
    rows = build_snapshot(results, {A: "a", B: "b"}, crowd_min=250, now=NOW)["positions"]
    assert rows[0]["oppose"] == [] and rows[0]["oppose_total"] == 0


async def _get(port, path):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    await w.drain()
    data = await r.read()
    w.close()
    head, _, body = data.partition(b"\r\n\r\n")
    return int(head.split()[1]), body


@pytest.mark.asyncio
async def test_http_needs_key_and_serves_page_and_json():
    d = Dashboard(app=None, key="s3cret", port=0)
    d.snapshot = snapshot()
    server = await asyncio.start_server(d.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        assert (await _get(port, "/health"))[0] == 200
        assert (await _get(port, "/"))[0] == 403
        assert (await _get(port, "/?key=wrong"))[0] == 403
        code, body = await _get(port, "/?key=s3cret")
        assert code == 200 and b"Starting in the next 2 hours" in body
        code, body = await _get(port, "/api/data?key=s3cret")
        assert code == 200 and json.loads(body)["positions"][0]["account"] == "Kch-Temp"


def test_dashboard_off_without_key():
    assert not Dashboard(app=None, key="").enabled()


def test_fill_mix_taken_laid_mixed():
    from polysharp.fees import fill_mix
    # taker fill: usdc = size*price + fee (3% * p * (1-p) * size)
    taker = {"type": "TRADE", "side": "BUY", "asset": "1", "size": 1000, "price": 0.5,
             "usdcSize": 500 + 1000 * 0.03 * 0.25}
    maker = {"type": "TRADE", "side": "BUY", "asset": "1", "size": 1000, "price": 0.5, "usdcSize": 500}
    other = dict(maker, asset="2")
    assert fill_mix([taker] * 4 + [maker, other], "1")["style"] == "taken"
    assert fill_mix([maker] * 4 + [taker, other], "1")["style"] == "laid"
    m = fill_mix([maker, taker], "1")
    assert m["style"] == "mixed" and m["taker_share"] == 0.5 and m["fees"] == 7.5
    assert fill_mix([other], "1") is None


@pytest.mark.asyncio
async def test_dashboard_adds_fill_mix_and_caches_it():
    calls = []

    class API:
        async def activity(self, user, limit=100, market=None):
            calls.append((user, market))
            return [{"type": "TRADE", "side": "BUY", "asset": "g", "size": 100, "price": 0.5, "usdcSize": 50}]

    class App:
        api = API()
    d = Dashboard(App(), key="k")
    snap = snapshot()
    await d.add_fill_mix(snap)
    hrh = next(r for r in snap["positions"] if r["account"] == "HomeRunHazard" and r["asset"] == "g")
    assert hrh["fill"]["style"] == "laid"
    n = len(calls)
    snap2 = snapshot()
    await d.add_fill_mix(snap2)
    assert len(calls) == n                              # unchanged positions -> cached


def test_same_account_both_sides_is_flagged():
    m = {"o": meta(NOW + 600), "u": meta(NOW + 600)}
    results = {A: ([pos("o", "ov", "Olimpia Milano vs. BC Dubai: O/U 169.5", "Over", 15000, 0.6),
                    pos("o", "un", "Olimpia Milano vs. BC Dubai: O/U 169.5", "Under", 15000, 0.4)], m)}
    rows = build_snapshot(results, {A: "HomeRunHazard"}, crowd_min=250, now=NOW)["positions"]
    over = next(r for r in rows if r["pick"] == "Over 169.5")
    assert over["own_other"] == [{"pick": "Under 169.5", "amount": 6000.0, "kind": "opposite", "note": None}]
    assert over["oppose"] == [] and over["own_other_total"] == 6000.0


def test_page_has_three_charts():
    from polysharp.dashboard import PAGE
    html = open(PAGE, encoding="utf-8").read()
    for cid in ('id="chart"', 'id="chart-pre"', 'id="chart-live"'):
        assert cid in html
    assert "Top 10 pregame" in html and "Top 10 live now" in html and "Top 25 positions" in html
