"""Find sports sharps worth a look: leaderboard -> recency screen -> deep check.

Nothing here adds wallets to the feed. It produces a ranked shortlist for the
morning message; Colin decides who gets /add-ed.

Performance comes straight from Polymarket's own sports P&L / volume (the same
numbers as the profile page), for 1W / 1M / All-time. We do NOT rebuild P&L from
position samples any more: for high-frequency accounts the latest 500 settled
bets cover ~a day while unredeemed losers go back months, which produced
nonsense (e.g. -46% ROI on a +$1.7M sports winner).

Style checks still come from their recent fills:
  * sports_share: $ of recent buys in sports markets (drops politics/war/crypto)
  * live_share:   $ of recent sports-game buys placed after the game started
  * days since last trade, top leagues, taker share
Ranked by last-month sports P&L.
"""
import asyncio
import logging
import time
from collections import Counter

from .fees import taker_baseline
from .markets import is_live, parse_ts

log = logging.getLogger(__name__)


def trade_profile(acts, meta, now=None):
    """Sports share, live share, recency and leagues from recent BUY fills."""
    now = now or time.time()
    tot = sports = game = live = 0.0
    last = 0
    leagues = Counter()
    for a in acts:
        if (a.get("type") or "TRADE") != "TRADE":
            continue
        ts = int(a.get("timestamp") or 0)
        last = max(last, ts)
        if (a.get("side") or "").upper() != "BUY":
            continue
        usd = float(a.get("usdcSize") or 0) or float(a.get("size") or 0) * float(a.get("price") or 0)
        m = meta.get(a.get("conditionId")) or {}
        tot += usd
        if not m.get("sports"):
            continue
        sports += usd
        if m.get("league"):
            leagues[m["league"].upper()] += usd
        if m.get("game_start"):
            game += usd
            if is_live(m, ts):
                live += usd
    return {
        "sports_share": round(sports / tot, 3) if tot else None,
        "live_share": round(live / game, 3) if game else 0.0,
        "days_since_trade": round((now - last) / 86400, 1) if last else None,
        "leagues": [lg for lg, _ in leagues.most_common(3)],
        "recent_buy_usd": round(tot, 2),
    }


def win_sample(closed, dead, now=None):
    """Win rate over ONE consistent window: the span covered by the settled sample.

    Unredeemed losers (curPrice 0 in /positions?redeemable=true) only count if the
    market ended inside that same window and isn't already in the settled sample.
    """
    now = now or time.time()
    if not closed:
        return {"win_rate": None, "win_n": 0, "win_days": 0}
    start = min(int(p.get("timestamp") or now) for p in closed)
    seen = {str(p.get("asset")) for p in closed}
    wins = sum(1 for p in closed if float(p.get("realizedPnl") or 0) > 0)
    losses = len(closed) - wins
    for p in dead:
        if float(p.get("curPrice") or 0) > 0 or str(p.get("asset")) in seen:
            continue
        end = parse_ts(p.get("endDate"))
        if end and end >= start - 86400:
            losses += 1
    n = wins + losses
    return {"win_rate": round(wins / n, 4) if n else None, "win_n": n,
            "win_days": round((now - start) / 86400, 1)}


def passes(s, cfg):
    reasons = []
    d = s.get("days_since_trade")
    if d is None or d > cfg.max_days_inactive:
        reasons.append("inactive" if d is None else f"cold {d:.0f}d")
    if s.get("sports_share") is None or s["sports_share"] < cfg.min_sports_share:
        reasons.append(f"sports {s.get('sports_share') or 0:.0%}")
    if s.get("live_share", 0) > cfg.max_live_share:
        reasons.append(f"live {s['live_share']:.0%}")
    if "pnl_m" not in s:
        reasons.append("no P&L data")
        return False, reasons
    if s.get("predictions", 0) < cfg.min_predictions:
        reasons.append(f"{s.get('predictions', 0)} predictions")
    if s.get("avg_bet", 0) < cfg.min_avg_bet:
        reasons.append(f"avg bet ${s.get('avg_bet', 0):,.0f}")
    if s["vol_m"] < cfg.min_month_vol:
        reasons.append(f"1M vol ${s['vol_m'] / 1e3:,.0f}K")
    if s["pnl_m"] < cfg.min_month_pnl:
        reasons.append(f"1M P&L ${s['pnl_m']:,.0f}")
    if s.get("pnl_overall", s["pnl_all"]) < cfg.min_realized_pnl:
        reasons.append(f"overall P&L ${s.get('pnl_overall', s['pnl_all']):,.0f}")
    if s["margin_all"] < cfg.min_margin:
        reasons.append(f"margin {s['margin_all']:.2%}")
    if cfg.min_win_rate and (s.get("win_rate") or 0) < cfg.min_win_rate:
        reasons.append(f"win {s.get('win_rate') or 0:.0%}")
    return not reasons, reasons


def fail_bucket(reasons):
    r = reasons[0] if reasons else ""
    if r.startswith(("cold", "inactive")):
        return "inactive"
    if r.startswith("sports"):
        return "not sports"
    if r.startswith("live"):
        return "live bettor"
    if "predictions" in r or r.startswith("avg bet"):
        return "too few/small bets"
    return "P&L/volume"


async def fetch_perf(api, address):
    calls = [api.user_pnl(address, p, "SPORTS") for p in ("WEEK", "MONTH", "ALL")]
    calls += [api.user_pnl(address, "ALL", "OVERALL"), api.traded_count(address)]
    w, m, a, ov, traded = await asyncio.gather(*calls)
    return {
        "pnl_w": round(w["pnl"], 2), "pnl_m": round(m["pnl"], 2), "pnl_all": round(a["pnl"], 2),
        "vol_w": round(w["vol"], 2), "vol_m": round(m["vol"], 2), "vol_all": round(a["vol"], 2),
        "pnl_overall": round(ov["pnl"], 2), "vol_overall": round(ov["vol"], 2),
        "margin_m": round(m["pnl"] / m["vol"], 5) if m["vol"] else 0.0,
        "margin_all": round(a["pnl"] / a["vol"], 5) if a["vol"] else 0.0,
        "predictions": traded,
        "avg_bet": round(ov["vol"] / traded, 2) if traded else 0.0,
        "rank_m": m["rank"], "rank_all": a["rank"],
        "score": round(m["pnl"], 2),
    }


async def fetch_wins(api, cfg, address):
    closed, dead = await asyncio.gather(
        api.closed_positions(address, cfg.win_sample_size),
        api.positions(address, redeemable=True))
    return win_sample(closed, dead)


async def deep_eval(api, markets, cfg, address, acts=None, wins=True):
    acts = acts if acts is not None else await api.activity(address, limit=500)
    slugs = {}
    for row in acts:
        cid = row.get("conditionId")
        if cid:
            slugs.setdefault(cid, row.get("eventSlug") or row.get("slug") or "")
    meta, perf = await asyncio.gather(markets.get(slugs), fetch_perf(api, address))
    stats = dict(perf)
    stats.update(trade_profile(acts, meta))
    tb = taker_baseline(acts)
    stats["taker_share"], stats["taker_sample"] = tb["taker_share"], tb["sample"]
    if wins:
        stats.update(await fetch_wins(api, cfg, address))
    return stats


async def gather_candidates(api, cfg, errors=None):
    cands = {}
    for sl in cfg.lb_slices:
        parts = [x.strip().upper() for x in sl.split(":")]
        parts += [""] * (3 - len(parts))
        period, category, order = parts[0], parts[1] or "OVERALL", parts[2] or "PNL"
        if order not in ("PNL", "VOL"):
            order = "PNL"
        for offset in range(0, cfg.lb_depth, 50):
            try:
                rows = await api.leaderboard(period, category, min(50, cfg.lb_depth - offset),
                                             offset, order)
            except Exception as e:
                log.warning("leaderboard %s offset %d failed: %s", sl, offset, e)
                if errors is not None:
                    errors.append(f"{sl}: {str(e)[:90]}")
                break
            for r in rows:
                addr = (r.get("proxyWallet") or "").lower()
                if not addr:
                    continue
                c = cands.setdefault(addr, {"address": addr, "name": r.get("userName") or addr[:10],
                                            "ranks": {}, "lb_vol": 0.0})
                c["ranks"][sl] = int(r.get("rank") or 0)
                c["lb_vol"] = max(c["lb_vol"], float(r.get("vol") or 0))
            if len(rows) < 50:
                break
    return cands


async def suggest(api, markets, cfg, exclude=frozenset()):
    errors = []
    cands = await gather_candidates(api, cfg, errors)
    total = len(cands)
    pool = [c for a, c in cands.items() if a not in exclude and c["lb_vol"] >= cfg.min_lb_vol]
    fails = Counter()
    fails["already tracked/skipped"] = sum(1 for a in cands if a in exclude)
    fails["P&L/volume"] += total - len(pool) - fails["already tracked/skipped"]
    log.info("Suggest: screening %d of %d leaderboard wallets", len(pool), total)

    async def one(c):
        try:
            acts = await api.activity(c["address"], limit=500)
            last = max((int(x.get("timestamp") or 0) for x in acts), default=0)
            if not last or (time.time() - last) / 86400 > cfg.max_days_inactive:
                fails["inactive"] += 1
                return None
            c["stats"] = await deep_eval(api, markets, cfg, c["address"], acts, wins=False)
            ok, reasons = passes(c["stats"], cfg)
            if ok:      # win-rate sample is the expensive part: only for survivors
                c["stats"].update(await fetch_wins(api, cfg, c["address"]))
                ok, reasons = passes(c["stats"], cfg)
            if not ok:
                fails[fail_bucket(reasons)] += 1
                return None
            c["stats"]["ranks"] = c["ranks"]
            return c
        except Exception as e:
            log.warning("eval %s failed: %s", c["address"], e)
            fails["error"] += 1
            return None

    results = [c for c in await asyncio.gather(*(one(c) for c in pool)) if c]
    results.sort(key=lambda c: c["stats"]["score"], reverse=True)
    summary = {"candidates": total, "screened": len(pool), "passed": len(results),
               "fails": dict(fails), "errors": errors}
    log.info("Suggest summary: %s", summary)
    return results[: cfg.suggest_count], summary
