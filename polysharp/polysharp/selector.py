"""Pick 'winning' wallets: leaderboard candidates -> verify on their own trade history.

The leaderboard ranks by P&L, which rewards size and luck as much as skill, and it
hides unredeemed losers. So every candidate is re-scored from its settled positions:

  * closed positions (realised P&L per position)
  * PLUS resolved-but-unredeemed losers from /positions (curPrice == 0), which
    never show up as "closed" and otherwise inflate win rate.

Score = dollar-weighted ROI * sqrt(#settled positions). It prefers wallets with a
real margin across many bets over one-hit whales, and market makers (huge volume,
thin margin) fall out on MIN_ROI.
"""
import asyncio
import logging
import math
import time

from .fees import taker_baseline

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


def passes(stats, cfg):
    reasons = []
    if stats["n"] < cfg.min_closed:
        reasons.append(f"n={stats['n']}")
    if stats["cost"] < cfg.min_staked:
        reasons.append(f"staked=${stats['cost']:,.0f}")
    if stats["win_rate"] < cfg.min_win_rate:
        reasons.append(f"win={stats['win_rate']:.0%}")
    if stats["roi"] < cfg.min_roi:
        reasons.append(f"roi={stats['roi']:.1%}")
    if stats["pnl"] < cfg.min_realized_pnl:
        reasons.append(f"pnl={stats['pnl']:,.0f}")
    di = stats.get("days_inactive")
    if di is None or di > cfg.max_days_inactive:
        reasons.append(f"inactive={di}")
    return not reasons, reasons


async def gather_candidates(api, cfg):
    cands = {}
    for sl in cfg.lb_slices:
        parts = (sl.split(":") + ["OVERALL", "PNL"])[:3]
        period, category, order = parts[0], parts[1] or "OVERALL", parts[2] or "PNL"
        for offset in range(0, cfg.lb_depth, 50):
            try:
                rows = await api.leaderboard(period, category, min(50, cfg.lb_depth - offset),
                                             offset, order)
            except Exception as e:
                log.warning("leaderboard %s offset %d failed: %s", sl, offset, e)
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


async def evaluate(api, cfg, address):
    closed, dead, acts = await asyncio.gather(
        api.closed_positions(address, cfg.history_positions),
        api.positions(address, redeemable=True),
        api.activity(address, limit=500))
    stats = score_history(closed, dead)
    tb = taker_baseline(acts)
    stats["taker_share"], stats["taker_sample"] = tb["taker_share"], tb["sample"]
    return stats


async def select_wallets(api, cfg, blocked=frozenset(), progress=None):
    cands = await gather_candidates(api, cfg)
    total = len(cands)
    cands = {a: c for a, c in cands.items()
             if a not in blocked and c["lb_vol"] >= cfg.min_lb_vol}
    log.info("Evaluating %d of %d leaderboard candidates (vol pre-cut)", len(cands), total)

    async def one(c):
        try:
            c["stats"] = await evaluate(api, cfg, c["address"])
        except Exception as e:
            log.warning("eval %s failed: %s", c["address"], e)
            c["stats"] = None
        return c

    results = await asyncio.gather(*(one(c) for c in cands.values()))
    keep, dropped = [], 0
    for c in results:
        if not c["stats"]:
            continue
        ok, _ = passes(c["stats"], cfg)
        if ok:
            c["stats"]["ranks"] = c["ranks"]
            keep.append(c)
        else:
            dropped += 1
    keep.sort(key=lambda c: c["stats"]["score"], reverse=True)
    picks = keep[: cfg.max_wallets]
    summary = {"candidates": total, "evaluated": len(cands), "passed": len(keep),
               "picked": len(picks)}
    log.info("Selection: %s", summary)
    return picks, summary
