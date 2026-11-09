import json
import logging
import os
import sqlite3
import threading
import time
from typing import Optional

log = logging.getLogger("tg_garmin_to_osm")


class State:
    """SQLite persistence: users (OSM + Garmin links, per-user settings), activities, meta, events.
    Thread-safe: single connection with check_same_thread=False, guarded by an internal RLock."""

    def __init__(self, data_dir: str):
        os.makedirs(data_dir, exist_ok=True)
        # timeout=30: the backfill Job may briefly share the DB with the main loop's writes
        # check_same_thread=False: ingest.py runs a ThreadingHTTPServer that calls State methods
        self.db = sqlite3.connect(os.path.join(data_dir, "state.db"), timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        # Schema bootstrap must fail fast: a half-initialised DB is worse than a k8s restart (a missing
        # `activities` table would make bookkeeping raise after the OSM upload already happened).
        with self._lock:
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS users ("
                "user_id INTEGER PRIMARY KEY, chat_id INTEGER, osm_access_token TEXT, osm_name TEXT, "
                "created_ts INTEGER, garmin_linked INTEGER DEFAULT 0, auto INTEGER DEFAULT 1, "
                "types TEXT DEFAULT 'bike', visibility TEXT DEFAULT 'identifiable', lang TEXT DEFAULT '', "
                "garmin_api_key TEXT, channel TEXT DEFAULT 'telegram', osm_uid INTEGER, "
                "garmin_owner_id TEXT, garmin_name TEXT)"
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
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS events ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, ts INTEGER, kind TEXT, data TEXT)"
            )
            self.db.execute("CREATE INDEX IF NOT EXISTS idx_events_user_ts ON events(user_id, ts DESC)")
            self._migrate_users()
            self.db.commit()

    def _migrate_users(self) -> None:
        """Idempotent column additions for pre-multi-user databases (PRAGMA check, no marker needed)."""
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(users)")}
        for name, ddl in (("garmin_linked", "INTEGER DEFAULT 0"), ("auto", "INTEGER DEFAULT 1"),
                          ("types", "TEXT DEFAULT 'bike'"),
                          ("visibility", "TEXT DEFAULT 'identifiable'"), ("lang", "TEXT DEFAULT ''"),
                          ("garmin_api_key", "TEXT"),
                          ("channel", "TEXT DEFAULT 'telegram'"),
                          ("osm_uid", "INTEGER"),
                          ("garmin_owner_id", "TEXT"),
                          ("garmin_name", "TEXT")):
            if name not in cols:
                self.db.execute(f"ALTER TABLE users ADD COLUMN {name} {ddl}")
        # UNIQUE index on osm_uid where not null
        idx = {r[1] for r in self.db.execute("PRAGMA index_list(users)")}
        if "uix_users_osm_uid" not in idx:
            self.db.execute("CREATE UNIQUE INDEX uix_users_osm_uid ON users(osm_uid) WHERE osm_uid IS NOT NULL")

    # -- meta / global settings -----------------------------------------------------------------------------------
    def get_meta(self, k: str, default: Optional[str] = None) -> Optional[str]:
        with self._lock:
            row = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
            return row["v"] if row else default

    def set_meta(self, k: str, v: str) -> None:
        with self._lock:
            self.db.execute(
                "INSERT INTO meta(k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v))
            )
            self.db.commit()

    def delete_meta(self, k: str) -> None:
        with self._lock:
            self.db.execute("DELETE FROM meta WHERE k=?", (k,))
            self.db.commit()

    def initialized(self) -> bool:
        with self._lock:
            return self.db.execute("SELECT 1 FROM meta WHERE k='init'").fetchone() is not None

    def set_initialized(self) -> None:
        with self._lock:
            self.db.execute("INSERT OR IGNORE INTO meta(k, v) VALUES ('init', '1')")
            self.db.commit()

    # -- users ------------------------------------------------------------------------------------------------------
    def user(self, user_id: int) -> Optional[dict]:
        with self._lock:
            row = self.db.execute("SELECT * FROM users WHERE user_id=?", (int(user_id),)).fetchone()
            return dict(row) if row else None

    def touch_user(self, user_id: int, chat_id: Optional[int], lang: str = "") -> dict:
        """Make sure a row exists (first contact); returns the row. chat_id may be NULL for web users."""
        with self._lock:
            self.db.execute(
                "INSERT OR IGNORE INTO users(user_id, chat_id, created_ts) VALUES (?,?,?)",
                (int(user_id), int(chat_id) if chat_id is not None else None, int(time.time())),
            )
            if lang:
                self.db.execute("UPDATE users SET lang=? WHERE user_id=? AND lang=''", (lang, int(user_id)))
            self.db.commit()
            return self.user(user_id)

    def save_osm_user(self, user_id: int, chat_id: Optional[int], access_token: str, osm_name: str,
                      osm_uid: Optional[int] = None) -> None:
        """Save OSM user. Keeps existing chat_id and osm_uid when new values are None (COALESCE)."""
        with self._lock:
            self.db.execute(
                "INSERT INTO users(user_id, chat_id, osm_access_token, osm_name, created_ts, osm_uid) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "chat_id=COALESCE(excluded.chat_id, users.chat_id), "
                "osm_access_token=excluded.osm_access_token, "
                "osm_name=excluded.osm_name, "
                "osm_uid=COALESCE(excluded.osm_uid, users.osm_uid)",
                (int(user_id), int(chat_id) if chat_id is not None else None, access_token, osm_name,
                 int(time.time()), osm_uid),
            )
            self.db.commit()

    def unlink_osm(self, user_id: int) -> None:
        with self._lock:
            self.db.execute(
                "UPDATE users SET osm_access_token=NULL, osm_name='', osm_uid=NULL WHERE user_id=?", (int(user_id),)
            )
            self.db.commit()

    def set_garmin_linked(self, user_id: int, flag: bool) -> None:
        with self._lock:
            self.db.execute(
                "INSERT INTO users(user_id, garmin_linked, created_ts) VALUES (?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET garmin_linked=excluded.garmin_linked",
                (int(user_id), int(bool(flag)), int(time.time())),
            )
            self.db.commit()

    def unlink_garmin(self, user_id: int) -> None:
        with self._lock:
            self.db.execute(
                "UPDATE users SET garmin_linked=0, garmin_api_key=NULL, garmin_owner_id=NULL, garmin_name=NULL WHERE user_id=?", (int(user_id),)
            )
            # a later link may be another Garmin account: its first scan gets the backlog treatment again
            self.db.execute("DELETE FROM meta WHERE k IN (?, ?)", (f"init:{user_id}", f"garmin_fail:{user_id}"))
            self.db.commit()

    def set_garmin_key(self, user_id: int, api_key: str) -> None:
        """Store the Personal API key; it IS the Garmin link (legacy garmin_linked kept for the
        web-site token-file path)."""
        self.set_setting(user_id, "garmin_api_key", api_key)
        self.set_garmin_linked(user_id, True)

    def set_setting(self, user_id: int, column: str, value) -> None:
        with self._lock:
            assert column in ("auto", "types", "visibility", "lang", "garmin_api_key"), column
            self.touch_user(user_id, user_id)  # settings work even before any link exists
            self.db.execute(f"UPDATE users SET {column}=? WHERE user_id=?", (str(value), int(user_id)))
            self.db.commit()

    def set_garmin_owner(self, user_id: int, owner_id: str, name: str) -> None:
        with self._lock:
            self.db.execute(
                "UPDATE users SET garmin_owner_id=?, garmin_name=? WHERE user_id=?",
                (owner_id, name, int(user_id)),
            )
            self.db.commit()

    def set_osm_uid(self, user_id: int, osm_uid: int) -> None:
        with self._lock:
            self.db.execute("UPDATE users SET osm_uid=? WHERE user_id=?", (int(osm_uid), int(user_id)))
            self.db.commit()

    def set_channel(self, user_id: int, channel: str) -> None:
        """Set user channel (telegram/web) under the State lock."""
        with self._lock:
            self.db.execute("UPDATE users SET channel=? WHERE user_id=?", (channel, int(user_id)))
            self.db.commit()

    def garmin_osm_users(self) -> list:
        """Users with both a Garmin link (Personal API key or legacy token file) and an OSM token:
        the ones the cycle serves."""
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM users WHERE garmin_linked=1 AND osm_access_token IS NOT NULL ORDER BY user_id"
            ).fetchall()
            return [dict(r) for r in rows]

    def osm_users(self) -> list:
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM users WHERE osm_access_token IS NOT NULL ORDER BY user_id"
            ).fetchall()
            return [dict(r) for r in rows]

    def garmin_users(self) -> list:
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM users WHERE garmin_linked=1 ORDER BY user_id"
            ).fetchall()
            return [dict(r) for r in rows]

    def counts_users(self) -> dict:
        with self._lock:
            row = self.db.execute(
                "SELECT COUNT(*) AS n, SUM(garmin_linked) AS g, "
                "SUM(CASE WHEN osm_access_token IS NOT NULL THEN 1 ELSE 0 END) AS o FROM users"
            ).fetchone()
            return {"users": row["n"], "garmin": row["g"] or 0, "osm": row["o"] or 0}

    def count_web_users(self) -> int:
        with self._lock:
            row = self.db.execute("SELECT COUNT(*) AS n FROM users WHERE channel='web'").fetchone()
            return row["n"] or 0

    def find_by_osm_uid(self, osm_uid: int) -> Optional[dict]:
        with self._lock:
            row = self.db.execute("SELECT * FROM users WHERE osm_uid=?", (int(osm_uid),)).fetchone()
            return dict(row) if row else None

    # -- activities (scoped by user) ---------------------------------------------------------------------------------
    def insert_activity(self, user_id: int, activity_id: str, name: str, type_key: str, start_ts: int,
                        distance_km: float, status: str) -> bool:
        """Remember a scanned activity. An already-known one is never overwritten; True if it was new."""
        with self._lock:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO activities(user_id, activity_id, name, type, start_ts, distance_km, "
                "status, created_ts) VALUES (?,?,?,?,?,?,?,?)",
                (int(user_id), str(activity_id), name, type_key, start_ts, distance_km, status, int(time.time())),
            )
            self.db.commit()
            return cur.rowcount > 0

    def activity(self, user_id: int, activity_id: str) -> Optional[dict]:
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM activities WHERE user_id=? AND activity_id=?", (int(user_id), str(activity_id))
            ).fetchone()
            return dict(row) if row else None

    def set_status(self, user_id: int, activity_id: str, status: str, trace_id: Optional[int] = None) -> None:
        with self._lock:
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
        with self._lock:
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
        with self._lock:
            cutoff = int((now if now is not None else time.time()) - delay_minutes * 60)
            rows = self.db.execute(
                "SELECT * FROM activities WHERE user_id=? AND status='new' AND created_ts<=? ORDER BY start_ts",
                (int(user_id), cutoff),
            ).fetchall()
            return [dict(r) for r in rows]

    def rides_by_status(self, user_id: int, status: str) -> list:
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM activities WHERE user_id=? AND status=? ORDER BY start_ts",
                (int(user_id), status),
            ).fetchall()
            return [dict(r) for r in rows]

    def pending_traces(self, user_id: int) -> list:
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM activities WHERE user_id=? AND status='uploaded' ORDER BY uploaded_ts",
                (int(user_id),),
            ).fetchall()
            return [dict(r) for r in rows]

    def failed_activities(self, user_id: int, limit: int = 10) -> list:
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM activities WHERE user_id=? AND status='failed' ORDER BY start_ts DESC LIMIT ?",
                (int(user_id), int(limit)),
            ).fetchall()
            return [dict(r) for r in rows]

    def counts(self, user_id: int) -> dict:
        with self._lock:
            rows = self.db.execute(
                "SELECT status, COUNT(*) AS n FROM activities WHERE user_id=? GROUP BY status", (int(user_id),)
            ).fetchall()
            return {r["status"]: r["n"] for r in rows}

    def recent_activities(self, user_id: int, limit: int = 30) -> list:
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM activities WHERE user_id=? ORDER BY start_ts DESC LIMIT ?",
                (int(user_id), int(limit)),
            ).fetchall()
            return [dict(r) for r in rows]

    # -- events (per-user outbox) ------------------------------------------------------------------------------------
    def add_event(self, user_id: int, kind: str, data: dict) -> None:
        """Write an event row; keep at most 200 per user (drop oldest)."""
        with self._lock:
            self.db.execute(
                "INSERT INTO events(user_id, ts, kind, data) VALUES (?,?,?,?)",
                (int(user_id), int(time.time()), kind, json.dumps(data, separators=(",", ":"))),
            )
            # trim to 200
            self.db.execute(
                "DELETE FROM events WHERE user_id=? AND id NOT IN ("
                "SELECT id FROM events WHERE user_id=? ORDER BY ts DESC, id DESC LIMIT 200)",
                (int(user_id), int(user_id)),
            )
            self.db.commit()

    def events(self, user_id: int, limit: int = 30) -> list:
        with self._lock:
            rows = self.db.execute(
                "SELECT ts, kind, data FROM events WHERE user_id=? ORDER BY ts DESC, id DESC LIMIT ?",
                (int(user_id), int(limit)),
            ).fetchall()
            return [{"ts": r["ts"], "kind": r["kind"], "data": json.loads(r["data"])} for r in rows]

    # -- deletion ----------------------------------------------------------------------------------------------------
    def delete_user(self, user_id: int) -> None:
        """Remove users row, activities, events, per-user meta keys, and legacy token dir (handled by caller)."""
        with self._lock:
            self.db.execute("DELETE FROM users WHERE user_id=?", (int(user_id),))
            self.db.execute("DELETE FROM activities WHERE user_id=?", (int(user_id),))
            self.db.execute("DELETE FROM events WHERE user_id=?", (int(user_id),))
            self.db.execute("DELETE FROM meta WHERE k LIKE ?", (f"%:{user_id}",))
            self.db.commit()
