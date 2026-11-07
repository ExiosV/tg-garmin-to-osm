import logging
import os
import sqlite3
import time
from typing import Optional

log = logging.getLogger("tg_garmin_to_osm")


class State:
    """SQLite persistence: users (OSM + Garmin links, per-user settings), activities, meta.
    Single process, single writer (the backfill Job runs while the deployment is scaled to 0)."""

    def __init__(self, data_dir: str):
        os.makedirs(data_dir, exist_ok=True)
        # timeout=30: the backfill Job may briefly share the DB with the main loop's writes
        self.db = sqlite3.connect(os.path.join(data_dir, "state.db"), timeout=30)
        self.db.row_factory = sqlite3.Row
        # Schema bootstrap must fail fast: a half-initialised DB is worse than a k8s restart (a missing
        # `activities` table would make bookkeeping raise after the OSM upload already happened).
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS users ("
            "user_id INTEGER PRIMARY KEY, chat_id INTEGER, osm_access_token TEXT, osm_name TEXT, "
            "created_ts INTEGER, garmin_linked INTEGER DEFAULT 0, auto INTEGER DEFAULT 1, "
            "types TEXT DEFAULT 'bike', visibility TEXT DEFAULT 'identifiable', lang TEXT DEFAULT '')"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS activities ("
            "user_id INTEGER, activity_id TEXT, name TEXT, type TEXT, start_ts INTEGER, distance_km REAL, "
            "status TEXT, trace_id INTEGER, attempts INTEGER DEFAULT 0, last_error TEXT, "
            "created_ts INTEGER, uploaded_ts INTEGER, "
            "PRIMARY KEY (user_id, activity_id))"
        )
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_activities_status ON activities(status)")
        self.db.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        self._migrate_users()
        self.db.commit()

    def _migrate_users(self) -> None:
        """Idempotent column additions for pre-multi-user databases (PRAGMA check, no marker needed)."""
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(users)")}
        for name, ddl in (("garmin_linked", "INTEGER DEFAULT 0"), ("auto", "INTEGER DEFAULT 1"),
                          ("types", "TEXT DEFAULT 'bike'"),
                          ("visibility", "TEXT DEFAULT 'identifiable'"), ("lang", "TEXT DEFAULT ''")):
            if name not in cols:
                self.db.execute(f"ALTER TABLE users ADD COLUMN {name} {ddl}")

    # -- meta / global settings -----------------------------------------------------------------------------------
    def get_meta(self, k: str, default: Optional[str] = None) -> Optional[str]:
        row = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return row["v"] if row else default

    def set_meta(self, k: str, v: str) -> None:
        self.db.execute(
            "INSERT INTO meta(k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v))
        )
        self.db.commit()

    def initialized(self) -> bool:
        return self.db.execute("SELECT 1 FROM meta WHERE k='init'").fetchone() is not None

    def set_initialized(self) -> None:
        self.db.execute("INSERT OR IGNORE INTO meta(k, v) VALUES ('init', '1')")
        self.db.commit()

    # -- users ------------------------------------------------------------------------------------------------------
    def user(self, user_id: int) -> Optional[dict]:
        row = self.db.execute("SELECT * FROM users WHERE user_id=?", (int(user_id),)).fetchone()
        return dict(row) if row else None

    def touch_user(self, user_id: int, chat_id: int, lang: str = "") -> dict:
        """Make sure a row exists (first contact); returns the row."""
        self.db.execute(
            "INSERT OR IGNORE INTO users(user_id, chat_id, created_ts) VALUES (?,?,?)",
            (int(user_id), int(chat_id), int(time.time())),
        )
        if lang:  # remember the client language until the user pins one with /lang
            self.db.execute("UPDATE users SET lang=? WHERE user_id=? AND lang=''", (lang, int(user_id)))
        self.db.commit()
        return self.user(user_id)

    def save_osm_user(self, user_id: int, chat_id: int, access_token: str, osm_name: str) -> None:
        self.db.execute(
            "INSERT INTO users(user_id, chat_id, osm_access_token, osm_name, created_ts) VALUES (?,?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET chat_id=excluded.chat_id, "
            "osm_access_token=excluded.osm_access_token, osm_name=excluded.osm_name",
            (int(user_id), int(chat_id), access_token, osm_name, int(time.time())),
        )
        self.db.commit()

    def unlink_osm(self, user_id: int) -> None:
        self.db.execute(
            "UPDATE users SET osm_access_token=NULL, osm_name='' WHERE user_id=?", (int(user_id),)
        )
        self.db.commit()

    def set_garmin_linked(self, user_id: int, flag: bool) -> None:
        self.db.execute(
            "INSERT INTO users(user_id, garmin_linked, created_ts) VALUES (?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET garmin_linked=excluded.garmin_linked",
            (int(user_id), int(bool(flag)), int(time.time())),
        )
        self.db.commit()

    def unlink_garmin(self, user_id: int) -> None:
        self.set_garmin_linked(user_id, False)

    def set_setting(self, user_id: int, column: str, value) -> None:
        assert column in ("auto", "types", "visibility", "lang"), column
        self.touch_user(user_id, user_id)  # settings work even before any link exists
        self.db.execute(f"UPDATE users SET {column}=? WHERE user_id=?", (str(value), int(user_id)))
        self.db.commit()

    def garmin_osm_users(self) -> list:
        """Users with both a Garmin token store and an OSM token: the ones the cycle serves."""
        rows = self.db.execute(
            "SELECT * FROM users WHERE garmin_linked=1 AND osm_access_token IS NOT NULL ORDER BY user_id"
        ).fetchall()
        return [dict(r) for r in rows]

    def osm_users(self) -> list:
        rows = self.db.execute(
            "SELECT * FROM users WHERE osm_access_token IS NOT NULL ORDER BY user_id"
        ).fetchall()
        return [dict(r) for r in rows]

    def garmin_users(self) -> list:
        rows = self.db.execute(
            "SELECT * FROM users WHERE garmin_linked=1 ORDER BY user_id"
        ).fetchall()
        return [dict(r) for r in rows]

    def counts_users(self) -> dict:
        row = self.db.execute(
            "SELECT COUNT(*) AS n, SUM(garmin_linked) AS g, "
            "SUM(CASE WHEN osm_access_token IS NOT NULL THEN 1 ELSE 0 END) AS o FROM users"
        ).fetchone()
        return {"users": row["n"], "garmin": row["g"] or 0, "osm": row["o"] or 0}

    # -- activities (scoped by user) ---------------------------------------------------------------------------------
    def insert_activity(self, user_id: int, activity_id: str, name: str, type_key: str, start_ts: int,
                        distance_km: float, status: str) -> bool:
        """Remember a scanned activity. An already-known one is never overwritten; True if it was new."""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO activities(user_id, activity_id, name, type, start_ts, distance_km, "
            "status, created_ts) VALUES (?,?,?,?,?,?,?,?)",
            (int(user_id), str(activity_id), name, type_key, start_ts, distance_km, status, int(time.time())),
        )
        self.db.commit()
        return cur.rowcount > 0

    def activity(self, user_id: int, activity_id: str) -> Optional[dict]:
        row = self.db.execute(
            "SELECT * FROM activities WHERE user_id=? AND activity_id=?", (int(user_id), str(activity_id))
        ).fetchone()
        return dict(row) if row else None

    def set_status(self, user_id: int, activity_id: str, status: str, trace_id: Optional[int] = None) -> None:
        sets, args = ["status=?"], [status]
        if trace_id is not None:
            sets.append("trace_id=?")
            args.append(trace_id)
        if status == "uploaded":
            sets.append("uploaded_ts=?")
            args.append(int(time.time()))
        args += [int(user_id), str(activity_id)]
        self.db.execute(
            f"UPDATE activities SET {', '.join(sets)} WHERE user_id=? AND activity_id=?", args
        )
        self.db.commit()

    def record_failure(self, user_id: int, activity_id: str, error: str, max_attempts: int) -> str:
        """Count one failed upload; 'failed' after max_attempts, else back to 'new' for a retry. Returns the status."""
        row = self.db.execute(
            "UPDATE activities SET attempts=attempts+1, last_error=? WHERE user_id=? AND activity_id=? "
            "RETURNING attempts",
            (error[:500], int(user_id), str(activity_id)),
        ).fetchone()
        status = "failed" if row and row["attempts"] >= max_attempts else "new"
        self.db.execute(
            "UPDATE activities SET status=? WHERE user_id=? AND activity_id=?",
            (status, int(user_id), str(activity_id)),
        )
        self.db.commit()
        return status

    def ready_to_upload(self, user_id: int, delay_minutes: int, now: Optional[float] = None) -> list:
        """This user's rides with status 'new' whose UPLOAD_DELAY window (time to /skip) passed; oldest first."""
        cutoff = int((now if now is not None else time.time()) - delay_minutes * 60)
        rows = self.db.execute(
            "SELECT * FROM activities WHERE user_id=? AND status='new' AND created_ts<=? ORDER BY start_ts",
            (int(user_id), cutoff),
        ).fetchall()
        return [dict(r) for r in rows]

    def rides_by_status(self, user_id: int, status: str) -> list:
        rows = self.db.execute(
            "SELECT * FROM activities WHERE user_id=? AND status=? ORDER BY start_ts",
            (int(user_id), status),
        ).fetchall()
        return [dict(r) for r in rows]

    def pending_traces(self, user_id: int) -> list:
        rows = self.db.execute(
            "SELECT * FROM activities WHERE user_id=? AND status='uploaded' ORDER BY uploaded_ts",
            (int(user_id),),
        ).fetchall()
        return [dict(r) for r in rows]

    def failed_activities(self, user_id: int, limit: int = 10) -> list:
        rows = self.db.execute(
            "SELECT * FROM activities WHERE user_id=? AND status='failed' ORDER BY start_ts DESC LIMIT ?",
            (int(user_id), int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]

    def counts(self, user_id: int) -> dict:
        rows = self.db.execute(
            "SELECT status, COUNT(*) AS n FROM activities WHERE user_id=? GROUP BY status", (int(user_id),)
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}
