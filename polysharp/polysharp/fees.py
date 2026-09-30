"""Taker-fee detection: who paid to cross the spread?

Polymarket charges takers only (makers pay nothing):
    fee = contracts * rate * p * (1 - p)        (USDC, added to buys / taken from sells)

The Data API's /activity rows carry `size` (contracts), `price` and `usdcSize`
(USDC actually moved). So for any fill:
    BUY : fee = usdcSize - size*price
    SELL: fee = size*price - usdcSize
    implied_rate = fee / (size * p * (1-p))

Maker fills back out to exactly 0. Taker fills back out to the market's fee rate
(e.g. 0.03 sports in mid-2026, 0.05 after the category schedule change). A wallet
that normally rests limit orders and suddenly pays fees to take liquidity is
telling you it wants in *now* — that's the conviction signal.

Caveats: fee-free markets (e.g. geopolitics) give no signal; tiny fills near 0/1
round to zero fee. The WS feed has no usdcSize, so WS fills are enriched from
/activity by tx hash before the alert goes out.
"""

MIN_RATE = 0.005   # below this treat as rounding noise
MAX_RATE = 0.20    # above this the row is malformed, not a fee


def analyze_fill(size, price, usdc, side):
    """Return dict(fee, notional, rate, taker) or None if usdc unknown."""
    if usdc is None or size <= 0 or not (0 < price < 1):
        return None
    notional = size * price
    fee = (usdc - notional) if side == "BUY" else (notional - usdc)
    curve = size * price * (1 - price)
    rate = fee / curve if curve > 0 else 0.0
    taker = MIN_RATE <= rate <= MAX_RATE and fee >= 0.001
    return {"fee": max(fee, 0.0) if taker else 0.0, "notional": notional,
            "rate": rate if taker else 0.0, "taker": taker}


def summarize(fills):
    """Aggregate analyze_fill() results over fills that have usdc info."""
    known = [f for f in fills if f.get("fee_info")]
    if not known:
        return None
    notional = sum(f["fee_info"]["notional"] for f in known)
    taker_notional = sum(f["fee_info"]["notional"] for f in known if f["fee_info"]["taker"])
    fees = sum(f["fee_info"]["fee"] for f in known)
    rates = [f["fee_info"]["rate"] for f in known if f["fee_info"]["taker"]]
    return {
        "coverage": len(known) / len(fills),
        "taker_share": taker_notional / notional if notional else 0.0,
        "fees": fees,
        "fee_pct": fees / notional if notional else 0.0,
        "rate": max(set(rates), key=rates.count) if rates else 0.0,
    }


def taker_baseline(activity_rows):
    """$-weighted share of a wallet's recent trades that were taker fills."""
    tot = tk = 0.0
    n = 0
    for r in activity_rows:
        if (r.get("type") or "TRADE") != "TRADE":
            continue
        a = analyze_fill(float(r.get("size") or 0), float(r.get("price") or 0),
                         _f(r.get("usdcSize")), (r.get("side") or "").upper())
        if not a:
            continue
        n += 1
        tot += a["notional"]
        tk += a["notional"] if a["taker"] else 0.0
    return {"taker_share": round(tk / tot, 3) if tot else None, "sample": n}


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None
