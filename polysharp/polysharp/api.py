"""Thin async client for Polymarket's public Data API (no auth needed)."""
import asyncio
import logging

import httpx

log = logging.getLogger(__name__)
DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"


class PolyAPI:
    def __init__(self, concurrency: int = 6):
        # trust_env=True -> honours HTTPS_PROXY if Railway egress needs a proxy
        self.http = httpx.AsyncClient(
            base_url=DATA_API, timeout=20, trust_env=True,
            headers={"User-Agent": "polysharp/1.0"})
        self.gamma = httpx.AsyncClient(
            base_url=GAMMA_API, timeout=20, trust_env=True,
            headers={"User-Agent": "polysharp/1.0"})
        self.sem = asyncio.Semaphore(concurrency)

    async def close(self):
        await self.http.aclose()
        await self.gamma.aclose()

    async def _get(self, path, params, retries=4, client=None):
        client = client or self.http
        delay = 1.0
        for attempt in range(retries):
            try:
                async with self.sem:
                    r = await client.get(path, params=params)
                if r.status_code == 429 or r.status_code >= 500:
                    raise httpx.HTTPStatusError(f"{r.status_code}", request=r.request, response=r)
                r.raise_for_status()
                return r.json()
            except (httpx.HTTPError, ValueError) as e:
                if attempt == retries - 1:
                    raise
                log.debug("GET %s failed (%s), retry in %.1fs", path, e, delay)
                await asyncio.sleep(delay)
                delay *= 2

    # --- endpoints ----------------------------------------------------------
    async def leaderboard(self, period="MONTH", category="OVERALL", limit=50, offset=0, order="PNL"):
        return await self._get("/v1/leaderboard", {
            "timePeriod": period, "category": category, "orderBy": order,
            "limit": limit, "offset": offset}) or []

    async def user_pnl(self, user, period="MONTH", category="SPORTS"):
        """Polymarket's own P&L + volume for one wallet (what the profile page shows)."""
        rows = await self._get("/v1/leaderboard", {
            "timePeriod": period, "category": category, "user": user}) or []
        r = rows[0] if rows else {}
        return {"pnl": float(r.get("pnl") or 0), "vol": float(r.get("vol") or 0),
                "rank": int(r.get("rank") or 0) if r else None}

    async def traded_count(self, user):
        """Number of markets the wallet has ever traded (the profile's 'Predictions')."""
        r = await self._get("/traded", {"user": user}) or {}
        return int(r.get("traded") or 0)

    async def closed_positions(self, user, max_rows=500):
        out, offset = [], 0
        while len(out) < max_rows:
            page = await self._get("/closed-positions", {
                "user": user, "limit": 50, "offset": offset,
                "sortBy": "TIMESTAMP", "sortDirection": "DESC"}) or []
            out.extend(page)
            if len(page) < 50:
                break
            offset += 50
        return out[:max_rows]

    async def positions(self, user, market=None, redeemable=None, sort=None):
        params = {"user": user, "sizeThreshold": 1, "limit": 500}
        if sort:
            params.update(sortBy=sort, sortDirection="DESC")
        if market:
            params["market"] = market
        if redeemable is not None:
            params["redeemable"] = str(bool(redeemable)).lower()
        return await self._get("/positions", params) or []

    async def activity(self, user, start=None, limit=100):
        params = {"user": user, "type": "TRADE", "limit": limit,
                  "sortBy": "TIMESTAMP", "sortDirection": "DESC"}
        if start:
            params["start"] = int(start)
        return await self._get("/activity", params) or []

    async def gamma_markets(self, condition_ids):
        """Market metadata for up to ~20 condition ids (open and closed)."""
        out = []
        for closed in ("true", "false"):
            params = [("condition_ids", c) for c in condition_ids]
            params += [("closed", closed), ("limit", len(condition_ids) + 5)]
            out += await self._get("/markets", params, client=self.gamma) or []
        return out
