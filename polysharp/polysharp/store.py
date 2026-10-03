"""SQLite state: tracked wallets, dedupe keys, recent trades for consensus, settings."""
import json
import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS wallets (
    address TEXT PRIMARY KEY, name TEXT, source TEXT, stats TEXT,
    active INTEGER DEFAULT 1, added_ts REAL);
CREATE TABLE IF NOT EXISTS blocked (address TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, ts REAL);
CREATE TABLE IF NOT EXISTS trades (
    ts REAL, wallet TEXT, asset TEXT, condition_id TEXT, outcome TEXT,
    title TEXT, slug TEXT, side TEXT, usd REAL, price REAL);
CREATE INDEX IF NOT EXISTS trades_asset ON trades(asset, ts);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS expand (id TEXT PRIMARY KEY, full TEXT, short TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS alerted (wallet TEXT, condition_id TEXT, asset TEXT, ts REAL,
    PRIMARY KEY (wallet, condition_id, asset));
"""


class Store:
    def __init__(self, path):
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # --- kv -----------------------------------------------------------------
    def get(self, k, default=None):
        row = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(row["v"]) if row else default

    def set(self, k, v):
        self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (k, json.dumps(v)))
        self.db.commit()

    # --- tap-to-expand alerts -------------------------------------------------
    def save_expand(self, full, short):
        import hashlib
        eid = hashlib.sha1(f"{time.time()}|{full}".encode()).hexdigest()[:12]
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO expand VALUES (?,?,?,?)",
                            (eid, full, short, time.time()))
        return eid

    def get_expand(self, eid):
        row = self.db.execute("SELECT full, short FROM expand WHERE id=?", (eid,)).fetchone()
        return (row["full"], row["short"]) if row else None

    # --- wallets ------------------------------------------------------------
    def drop_auto_wallets(self):
        """Feed is manual-only now: retire any wallets an older version auto-added."""
        with self.db:
            self.db.execute("DELETE FROM wallets WHERE source='auto'")

    def add_manual(self, address, name=None, stats=None):
        with self.db:
            self.db.execute(
                """INSERT INTO wallets(address,name,source,stats,active,added_ts)
                   VALUES (?,?,'manual',?,1,?)
                   ON CONFLICT(address) DO UPDATE SET source='manual', active=1,
                   name=COALESCE(excluded.name, wallets.name), stats=excluded.stats""",
                (address.lower(), name, json.dumps(stats or {}), time.time()))
        self.unskip(address)

    def update_stats(self, address, stats):
        with self.db:
            self.db.execute("UPDATE wallets SET stats=? WHERE address=?",
                            (json.dumps(stats), address.lower()))

    def remove(self, address):
        with self.db:
            cur = self.db.execute("DELETE FROM wallets WHERE address=?", (address.lower(),))
        return cur.rowcount > 0

    # --- suggestion bookkeeping ---------------------------------------------
    def skip(self, address, days):
        d = self.get("skipped", {})
        d[address.lower()] = time.time() + days * 86400
        self.set("skipped", d)

    def unskip(self, address):
        d = self.get("skipped", {})
        if d.pop(address.lower(), None) is not None:
            self.set("skipped", d)

    def skipped(self):
        now = time.time()
        return {a for a, until in self.get("skipped", {}).items() if until > now}

    def mark_suggested(self, addresses):
        d = self.get("suggested", {})
        now = time.time()
        for a in addresses:
            d[a.lower()] = now
        d = {a: t for a, t in d.items() if now - t < 60 * 86400}
        self.set("suggested", d)

    def recently_suggested(self, days):
        now = time.time()
        return {a for a, t in self.get("suggested", {}).items() if now - t < days * 86400}

    def active_wallets(self):
        rows = self.db.execute("SELECT * FROM wallets WHERE active=1").fetchall()
        return {r["address"]: {"name": r["name"], "source": r["source"],
                               "stats": json.loads(r["stats"] or "{}")} for r in rows}

    # --- dedupe -------------------------------------------------------------
    def mark_seen(self, key):
        """Returns True if key is new."""
        try:
            self.db.execute("INSERT INTO seen VALUES (?,?)", (key, time.time()))
            self.db.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def has_seen(self, key):
        return self.db.execute("SELECT 1 FROM seen WHERE key=?", (key,)).fetchone() is not None

    # --- consensus ----------------------------------------------------------
    def record_trade(self, t):
        self.db.execute("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?)", (
            t["ts"], t["wallet"], t["asset"], t["condition_id"], t["outcome"],
            t["title"], t["slug"], t["side"], t["usd"], t["price"]))
        self.db.commit()

    def wallets_in_market(self, condition_id, since_ts):
        rows = self.db.execute(
            "SELECT DISTINCT wallet FROM trades WHERE condition_id=? AND ts>=?",
            (condition_id, since_ts)).fetchall()
        return [r["wallet"] for r in rows]

    def buy_bursts(self, wallet, asset, since_ts, gap=120):
        """Separate buying sessions on one outcome: (bursts, total_usd, first_ts)."""
        rows = self.db.execute(
            """SELECT ts, usd FROM trades WHERE wallet=? AND asset=? AND side='BUY' AND ts>=?
               ORDER BY ts""", (wallet, asset, since_ts)).fetchall()
        bursts, last, total = 0, None, 0.0
        for r in rows:
            if last is None or r["ts"] - last > gap:
                bursts += 1
            last = r["ts"]
            total += r["usd"]
        return bursts, total, (rows[0]["ts"] if rows else None)

    def mark_alerted(self, wallet, condition_id, asset):
        self.db.execute("INSERT OR REPLACE INTO alerted VALUES (?,?,?,?)",
                        (wallet, condition_id, asset, time.time()))
        self.db.commit()

    def was_alerted(self, wallet, condition_id, days=14):
        return self.db.execute(
            "SELECT 1 FROM alerted WHERE wallet=? AND condition_id=? AND ts>=?",
            (wallet, condition_id, time.time() - days * 86400)).fetchone() is not None

    def buyers_of(self, asset, since_ts):
        rows = self.db.execute(
            """SELECT wallet, SUM(usd) usd, AVG(price) px FROM trades
               WHERE asset=? AND side='BUY' AND ts>=? GROUP BY wallet""",
            (asset, since_ts)).fetchall()
        return [dict(r) for r in rows]

    def prune(self, days=7):
        cut = time.time() - days * 86400
        with self.db:
            self.db.execute("DELETE FROM seen WHERE ts<?", (cut,))
            self.db.execute("DELETE FROM trades WHERE ts<?", (cut,))
            self.db.execute("DELETE FROM alerted WHERE ts<?", (cut - 7 * 86400,))
            self.db.execute("DELETE FROM expand WHERE ts<?", (cut,))
