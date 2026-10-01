"""Market metadata: is it sports, which league, and when does the game start?

Source of truth is Polymarket's Gamma API:
  * feeType "sports_fees_v*"  -> sports market (games AND futures)
  * gameStartTime             -> set on game markets; lets us tell pre-game from live
  * sportsMarketType          -> moneyline / spreads / totals / ...
Fallback when Gamma misses a market: the event slug's league prefix (mlb-, nfl-, ...).

A trade is LIVE if it happened more than LIVE_GRACE seconds after gameStartTime.
Results are cached in SQLite; games that haven't started are re-checked hourly
(postponements move gameStartTime).
"""
import json
import logging
import re
import time
from datetime import datetime, timezone

log = logging.getLogger(__name__)

LIVE_GRACE = 120  # seconds after scheduled start before a fill counts as live

LEAGUES = {
    "mlb", "nfl", "nba", "nhl", "wnba", "cfb", "cbb", "ncaab", "ncaaf", "ncaaw", "cfl",
    "mls", "epl", "ucl", "uel", "uecl", "lal", "laliga", "sea", "seriea", "bun", "bundesliga",
    "fl1", "ligue1", "ere", "eredivisie", "por", "arg", "bra", "mex", "tur", "sco", "efl",
    "fifa", "wc", "uefa", "concacaf", "copa", "kbo", "npb", "atp", "wta", "tennis", "ufc",
    "mma", "boxing", "pga", "golf", "liv", "f1", "nascar", "indycar", "ipl", "cricket",
    "afl", "nrl", "rugby", "euroleague", "nbl", "khl", "shl", "ahl", "xfl", "ufl",
}
SPORTS = {   # sport bucket -> (topic title, leagues)
    "football": ("🏈 Football", {"nfl", "cfb", "ncaaf", "cfl", "xfl", "ufl"}),
    "baseball": ("⚾ Baseball", {"mlb", "kbo", "npb"}),
    "basketball": ("🏀 Basketball", {"nba", "wnba", "cbb", "ncaab", "ncaaw", "euroleague", "nbl"}),
    "hockey": ("🏒 Hockey", {"nhl", "khl", "shl", "ahl"}),
    "soccer": ("⚽ Soccer", {"mls", "epl", "ucl", "uel", "uecl", "lal", "laliga", "sea", "seriea", "bun",
                            "bundesliga", "fl1", "ligue1", "ere", "eredivisie", "por", "arg", "bra", "mex",
                            "tur", "sco", "efl", "fifa", "wc", "uefa", "unl", "concacaf", "copa"}),
    "tennis": ("🎾 Tennis", {"atp", "wta", "tennis"}),
    "fighting": ("🥊 Fighting", {"ufc", "mma", "boxing"}),
    "other": ("🎯 Other", set()),
}
LEAGUES |= set().union(*(lg for _, lg in SPORTS.values()))


def sport_of(league):
    lg = (league or "").lower()
    for key, (_, leagues) in SPORTS.items():
        if lg in leagues:
            return key
    return "other"


_PREFIX = re.compile(r"^([a-z0-9]+)-")


def league_of(slug):
    m = _PREFIX.match((slug or "").lower())
    return m.group(1) if m and m.group(1) in LEAGUES else None


def parse_ts(s):
    if not s:
        return None
    s = str(s).strip().replace(" ", "T").replace("Z", "+00:00")
    if re.search(r"[+-]\d\d$", s):
        s += ":00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def classify_gamma(m):
    ev = (m.get("events") or [{}])[0] or {}
    slug = ev.get("slug") or m.get("slug") or ""
    ft = (m.get("feeType") or "").lower()
    league = league_of(slug) or league_of(m.get("slug"))
    sports = ft.startswith("sports") or bool(m.get("gameStartTime")) \
        or bool(m.get("sportsMarketType")) or league is not None
    return {"sports": sports, "game_start": parse_ts(m.get("gameStartTime")),
            "type": m.get("sportsMarketType") or "", "league": league or "", "slug": slug,
            "event_title": ev.get("title") or ""}


def classify_slug(slug):
    lg = league_of(slug)
    return {"sports": lg is not None, "game_start": None, "type": "", "league": lg or "",
            "slug": slug or ""}


def is_live(meta, trade_ts):
    gs = meta.get("game_start") if meta else None
    return bool(gs) and trade_ts > gs + LIVE_GRACE


class Markets:
    def __init__(self, api, store):
        self.api, self.store = api, store
        store.db.execute("""CREATE TABLE IF NOT EXISTS markets (
            condition_id TEXT PRIMARY KEY, meta TEXT, fetched REAL, source TEXT)""")
        store.db.commit()

    def _cached(self, cids):
        out = {}
        now = time.time()
        for i in range(0, len(cids), 400):
            chunk = cids[i:i + 400]
            q = ",".join("?" * len(chunk))
            for r in self.store.db.execute(
                    f"SELECT condition_id, meta, fetched, source FROM markets WHERE condition_id IN ({q})",
                    chunk):
                meta = json.loads(r[1])
                gs = meta.get("game_start")
                stale = (gs and gs > now and now - r[2] > 3600) or \
                        (r[3] == "slug" and now - r[2] > 86400) or \
                        (r[3] == "gamma" and "event_title" not in meta)   # pre-opponent cache
                if not stale:
                    out[r[0]] = meta
        return out

    def _save(self, cid, meta, source):
        self.store.db.execute("INSERT OR REPLACE INTO markets VALUES (?,?,?,?)",
                              (cid, json.dumps(meta), time.time(), source))

    async def get(self, slugs_by_cid):
        """slugs_by_cid: {conditionId: eventSlug-or-slug}. Returns {conditionId: meta}."""
        cids = [c for c in slugs_by_cid if c]
        out = self._cached(cids)
        missing = [c for c in cids if c not in out]
        for i in range(0, len(missing), 20):
            batch = missing[i:i + 20]
            try:
                rows = await self.api.gamma_markets(batch)
            except Exception as e:
                log.debug("gamma lookup failed: %s", e)
                rows = []
            for m in rows:
                cid = m.get("conditionId")
                if cid in slugs_by_cid and cid not in out:
                    out[cid] = classify_gamma(m)
                    self._save(cid, out[cid], "gamma")
        for c in missing:
            if c not in out:
                out[c] = classify_slug(slugs_by_cid[c])
                self._save(c, out[c], "slug")
        self.store.db.commit()
        return out
