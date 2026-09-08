import sqlite3
import time
from threading import Lock


class Cache:
    """
    SQLite-backed release cache.

    Rows are keyed on (release, version). `version` is a fingerprint of the
    matching/validation logic (see srrdb.LOGIC_VERSION). This lets us
    invalidate the cache *cleanly* whenever that logic changes: rows written
    under an old version simply stop being matched by lookups under the new
    version, without deleting anything or needing a manual cache wipe. The
    cache itself (the DB file, and any rows still valid under the current
    version) is always preserved across restarts.
    """

    def __init__(self, path: str):
        self.lock = Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self._migrate_legacy_schema()
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS cache ("
            "release TEXT NOT NULL, "
            "version TEXT NOT NULL, "
            "found INTEGER NOT NULL, "
            "checked_at INTEGER NOT NULL, "
            "PRIMARY KEY (release, version))"
        )
        self.db.commit()

    def _migrate_legacy_schema(self):
        """
        One-time migration from the original (pre-versioning) schema, which
        had no `version` column and therefore no way to distinguish stale
        logic from fresh logic. We rename it out of the way instead of
        dropping it, so no data is destroyed; it's just no longer read.
        """
        cols = [
            row[1]
            for row in self.db.execute("PRAGMA table_info(cache)").fetchall()
        ]
        if cols and "version" not in cols:
            self.db.execute("ALTER TABLE cache RENAME TO cache_legacy_unversioned")
            self.db.commit()

    def get(self, release: str, version: str, positive_ttl: int, negative_ttl: int):
        with self.lock:
            row = self.db.execute(
                "SELECT found, checked_at FROM cache WHERE release=? AND version=?",
                (release, version)
            ).fetchone()

        if row is None:
            return None

        found, checked_at = row
        ttl = positive_ttl if found else negative_ttl

        if int(time.time()) - checked_at >= ttl:
            return None

        return bool(found)

    def put(self, release: str, version: str, found: bool):
        with self.lock:
            self.db.execute(
                "INSERT OR REPLACE INTO cache(release, version, found, checked_at) "
                "VALUES (?, ?, ?, ?)",
                (release, version, int(found), int(time.time()))
            )
            self.db.commit()
