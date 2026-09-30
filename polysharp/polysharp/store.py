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

    # --- wallets ------------------------------------------------------------
    def replace_auto_wallets(self, picks):
        """picks: list of dicts with address, name, stats. Manual wallets untouched."""
        now = time.time()
        with self.db:
            self.db.execute("UPDATE wallets SET active=0 WHERE source='auto'")
            for p in picks:
                self.db.execute(
                    """INSERT INTO wallets(address,name,source,stats,active,added_ts)
                       VALUES (?,?,'auto',?,1,?)
                       ON CONFLICT(address) DO UPDATE SET name=excluded.name,
                       stats=excluded.stats, active=1
                       WHERE wallets.source='auto'""",
                    (p["address"].lower(), p["name"], json.dumps(p["stats"]), now))

    def add_manual(self, address, name=None, stats=None):
        with self.db:
            self.db.execute("DELETE FROM blocked WHERE address=?", (address.lower(),))
            self.db.execute(
                """INSERT INTO wallets(address,name,source,stats,active,added_ts)
                   VALUES (?,?,'manual',?,1,?)
                   ON CONFLICT(address) DO UPDATE SET source='manual', active=1,
                   name=COALESCE(excluded.name, wallets.name)""",
                (address.lower(), name, json.dumps(stats or {}), time.time()))

    def remove(self, address):
        with self.db:
            self.db.execute("DELETE FROM wallets WHERE address=?", (address.lower(),))
            self.db.execute("INSERT OR IGNORE INTO blocked VALUES (?)", (address.lower(),))

    def blocked(self):
        return {r["address"] for r in self.db.execute("SELECT address FROM blocked")}

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
