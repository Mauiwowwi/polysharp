"""Find sports sharps worth a look: leaderboard -> cheap screen -> deep sports/live check.

Nothing here adds wallets to the feed. It produces a ranked shortlist for the
morning message; Colin decides who gets /add-ed.

Deep check (per wallet, SPORTS positions only):
  * settled bets, $ staked, ROI, win rate, realised P&L -- from closed positions
    PLUS resolved-but-unredeemed losers (which never show as "closed")
  * sports_share: $ of recent buys in sports markets (drops politics/war/crypto)
  * live_share:   $ of recent sports-game buys placed after the game started
  * days since last trade, top leagues, taker share
Score = ROI * sqrt(settled bets): real margin over many bets beats one hot streak.
"""
import asyncio
import logging
import math
import time
from collections import Counter

from .fees import taker_baseline
from .markets import is_live

log = logging.getLogger(__name__)


def score_history(closed, dead_positions, now=None):
    """Return stats dict from closed positions + unredeemed losers."""
    now = now or time.time()
    pnl = cost = 0.0
    wins = n = 0
    last_ts = 0
    for p in closed:
        c = float(p.get("totalBought") or 0) * float(p.get("avgPrice") or 0)
        r = float(p.get("realizedPnl") or 0)
        if c <= 0:
            continue
        n += 1
        cost += c
        pnl += r
        wins += r > 0
        last_ts = max(last_ts, int(p.get("timestamp") or 0))
    for p in dead_positions:
        # resolved against them, never redeemed -> full loss of initial value
        if float(p.get("curPrice") or 0) > 0:
            continue
        c = float(p.get("initialValue") or 0)
        if c <= 0:
            continue
        n += 1
        cost += c
        pnl -= c
    win_rate = wins / n if n else 0.0
    roi = pnl / cost if cost else 0.0
    return {
        "n": n, "wins": wins, "win_rate": round(win_rate, 4), "roi": round(roi, 4),
        "pnl": round(pnl, 2), "cost": round(cost, 2),
        "score": round(roi * math.sqrt(n), 4) if n else 0.0,
        "last_ts": last_ts,
        "days_inactive": round((now - last_ts) / 86400, 1) if last_ts else None,
    }


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


def passes(s, cfg):
    reasons = []
    d = s.get("days_since_trade")
    if d is None or d > cfg.max_days_inactive:
        reasons.append("inactive" if d is None else f"cold {d:.0f}d")
    if s.get("sports_share") is None or s["sports_share"] < cfg.min_sports_share:
        reasons.append(f"sports {s.get('sports_share') or 0:.0%}")
    if s.get("live_share", 0) > cfg.max_live_share:
        reasons.append(f"live {s['live_share']:.0%}")
    if s["n"] < cfg.min_closed:
        reasons.append(f"n={s['n']}")
    if s["cost"] < cfg.min_staked:
        reasons.append(f"staked ${s['cost'] / 1e3:,.0f}K")
    if s["roi"] < cfg.min_roi:
        reasons.append(f"ROI {s['roi']:+.1%}")
    if s["win_rate"] < cfg.min_win_rate:
        reasons.append(f"win {s['win_rate']:.0%}")
    if s["pnl"] < cfg.min_realized_pnl:
        reasons.append(f"P&L ${s['pnl']:,.0f}")
    return not reasons, reasons


def fail_bucket(reasons):
    r = reasons[0] if reasons else ""
    if r.startswith(("cold", "inactive")):
        return "inactive"
    if r.startswith("sports"):
        return "not sports"
    if r.startswith("live"):
        return "live bettor"
    return "volume/ROI"


async def fetch_raw(api, cfg, address):
    closed, dead, acts = await asyncio.gather(
        api.closed_positions(address, cfg.history_positions),
        api.positions(address, redeemable=True),
        api.activity(address, limit=500))
    return {"closed": closed, "dead": dead, "acts": acts}


async def deep_eval(api, markets, cfg, address, raw=None):
    raw = raw or await fetch_raw(api, cfg, address)
    slugs = {}
    for row in raw["closed"] + raw["dead"] + raw["acts"]:
        cid = row.get("conditionId")
        if cid:
            slugs.setdefault(cid, row.get("eventSlug") or row.get("slug") or "")
    meta = await markets.get(slugs)
    sp = lambda r: (meta.get(r.get("conditionId")) or {}).get("sports")  # noqa: E731
    stats = score_history([r for r in raw["closed"] if sp(r)], [r for r in raw["dead"] if sp(r)])
    stats.update(trade_profile(raw["acts"], meta))
    tb = taker_baseline(raw["acts"])
    stats["taker_share"], stats["taker_sample"] = tb["taker_share"], tb["sample"]
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


def _cheap_ok(raw, cfg):
    """Fast screen before any market lookups: recent activity + enough overall size."""
    last = max((int(a.get("timestamp") or 0) for a in raw["acts"]), default=0)
    if not last or (time.time() - last) / 86400 > cfg.max_days_inactive:
        return False, "inactive"
    s = score_history(raw["closed"], raw["dead"])
    if s["n"] < cfg.min_closed * 0.6 or s["cost"] < cfg.min_staked * 0.6:
        return False, "volume/ROI"
    return True, ""


async def suggest(api, markets, cfg, exclude=frozenset()):
    errors = []
    cands = await gather_candidates(api, cfg, errors)
    total = len(cands)
    pool = [c for a, c in cands.items() if a not in exclude and c["lb_vol"] >= cfg.min_lb_vol]
    fails = Counter()
    fails["already tracked/skipped"] = sum(1 for a in cands if a in exclude)
    fails["volume/ROI"] += total - len(pool) - fails["already tracked/skipped"]
    log.info("Suggest: screening %d of %d leaderboard wallets", len(pool), total)

    async def one(c):
        try:
            acts = await api.activity(c["address"], limit=500)
            last = max((int(x.get("timestamp") or 0) for x in acts), default=0)
            if not last or (time.time() - last) / 86400 > cfg.max_days_inactive:
                fails["inactive"] += 1
                return None
            closed, dead = await asyncio.gather(
                api.closed_positions(c["address"], cfg.history_positions),
                api.positions(c["address"], redeemable=True))
            raw = {"closed": closed, "dead": dead, "acts": acts}
            ok, why = _cheap_ok(raw, cfg)
            if not ok:
                fails[why] += 1
                return None
            c["stats"] = await deep_eval(api, markets, cfg, c["address"], raw)
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
