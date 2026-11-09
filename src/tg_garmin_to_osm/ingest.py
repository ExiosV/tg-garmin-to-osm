"""Cluster-internal HTTP ingest + web account API: the web site (garmin-osm.exiosv.com) drives
sign-up and all settings through this API. The bot process stays the single writer of its SQLite
and PVC. Bearer-token authenticated, fail-closed; no Ingress, so nothing outside the cluster sees it.
"""
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from .config import Config, TYPE_GROUPS, expand_types
from .redact import redact
from .runner import Runner, apply_garmin_key_pure, user_cycle
from .state import State

log = logging.getLogger("tg_garmin_to_osm")


class ApiHandler(BaseHTTPRequestHandler):
    """HTTP handler for /ping, /ingest/*, and /api/* routes."""

    def __init__(self, cfg: Config, state: State, runner: Runner,
                 *args, **kwargs):
        self.cfg = cfg
        self.state = state
        self.runner = runner
        super().__init__(*args, **kwargs)

    def _reply(self, code: int, body: dict) -> None:
        data = json.dumps(body, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        import hmac
        header = self.headers.get("Authorization") or ""
        return bool(self.cfg.ingest_token) and hmac.compare_digest(header, f"Bearer {self.cfg.ingest_token}")

    def _get_json(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length == 0:
                self._reply(400, {"error": "empty body"})
                return None
            return json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            self._reply(400, {"error": "bad json"})
            return None

    def _parse_uid(self, path: str) -> Optional[int]:
        # path like /api/users/42/... -> 42
        parts = path.strip("/").split("/")
        if len(parts) >= 3 and parts[0] == "api" and parts[1] == "users":
            try:
                return int(parts[2])
            except ValueError:
                return None
        return None

    def _parse_activity_id(self, path: str) -> Optional[str]:
        # path like /api/users/42/activities/123/upload -> 123
        parts = path.strip("/").split("/")
        if len(parts) >= 5 and parts[0] == "api" and parts[1] == "users" and parts[3] == "activities":
            return parts[4]
        return None

    def _acquire_work_lock(self) -> bool:
        return self.runner.work_lock.acquire(timeout=20)

    def _release_work_lock(self) -> None:
        self.runner.work_lock.release()

    def do_GET(self) -> None:
        if self.path == "/ping":
            return self._reply(200, {"ok": True})
        if self.path.startswith("/api/users/"):
            if not self._authorized():
                return self._reply(401, {"error": "unauthorized"})
            uid = self._parse_uid(self.path)
            if uid is None:
                return self._reply(400, {"error": "bad uid"})
            user = self.state.user(uid)
            if not user:
                return self._reply(404, {"error": "not_found"})
            return self._reply(200, self._build_user_response(user))
        return self._reply(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._authorized():
            return self._reply(401, {"error": "unauthorized"})

        body = self._get_json()
        if body is None:
            return

        try:
            if self.path == "/ingest/garmin":
                uid = int(body["uid"])
                tokens = str(body["tokens"])
                if not tokens.strip().startswith("{"):
                    raise ValueError("tokens must be the garmin_tokens.json content")
                from .runner import ingest_garmin_link
                ingest_garmin_link(self.cfg, self.runner.tg, self.state, uid, tokens)
                return self._reply(200, {"ok": True})

            if self.path == "/ingest/osm":
                uid = int(body["uid"])
                access_token = str(body["access_token"])
                name = str(body.get("name") or "")
                if not access_token:
                    raise ValueError("access_token is empty")
                # optional osm_uid to detect collision
                if "osm_uid" in body:
                    osm_uid = int(body["osm_uid"])
                    existing = self.state.find_by_osm_uid(osm_uid)
                    if existing and existing["user_id"] != uid:
                        return self._reply(409, {"error": "osm_taken"})
                from .runner import ingest_osm_link
                ingest_osm_link(self.cfg, self.runner.tg, self.state, uid, access_token, name)
                return self._reply(200, {"ok": True})

            # --- /api/accounts/* ---
            if self.path == "/api/accounts/osm":
                return self._handle_accounts_osm(body)
            if self.path == "/api/accounts/telegram":
                return self._handle_accounts_telegram(body)

            # --- /api/users/{uid}/* ---
            if self.path.startswith("/api/users/"):
                return self._handle_user_routes(body)

            return self._reply(404, {"error": "not found"})

        except (KeyError, TypeError, ValueError) as e:
            return self._reply(400, {"error": str(e)[:200]})
        except Exception as e:
            log.error("ingest %s failed: %s", self.path, redact(e))
            return self._reply(500, {"error": "internal"})

    # -- account creation -------------------------------------------------------------------------------

    def _handle_accounts_osm(self, body: dict) -> None:
        osm_uid = int(body["osm_uid"])
        osm_name = str(body["osm_name"])
        access_token = str(body["access_token"])
        lang = str(body.get("lang") or "en")
        if lang not in ("en", "ru"):
            lang = "en"

        existing = self.state.find_by_osm_uid(osm_uid)
        if existing:
            # update token + name + lang for existing user
            self.state.save_osm_user(existing["user_id"], existing["chat_id"], access_token, osm_name, osm_uid)
            self.state.set_setting(existing["user_id"], "lang", lang)
            return self._reply(200, {"uid": existing["user_id"], "created": False})

        # new web user
        if self.state.count_web_users() >= self.cfg.max_web_users:
            return self._reply(403, {"error": "signup_closed"})

        uid = -osm_uid
        # ensure no collision (should not happen, but be safe)
        if self.state.user(uid):
            return self._reply(409, {"error": "uid_collision"})

        chat_id = None
        self.state.touch_user(uid, chat_id, lang)
        self.state.save_osm_user(uid, chat_id, access_token, osm_name, osm_uid)
        self.state.db.execute(
            "UPDATE users SET channel='web' WHERE user_id=?", (uid,))
        self.state.db.commit()
        log.info("created web user %s (osm_uid=%s)", uid, osm_uid)
        return self._reply(200, {"uid": uid, "created": True})

    def _handle_accounts_telegram(self, body: dict) -> None:
        tg_uid = int(body["tg_uid"])
        lang = str(body.get("lang") or "en")
        if lang not in ("en", "ru"):
            lang = "en"
        # ensure row exists with chat_id = tg_uid
        self.state.touch_user(tg_uid, tg_uid, lang)
        self.state.db.execute(
            "UPDATE users SET channel='telegram' WHERE user_id=?", (tg_uid,))
        self.state.db.commit()
        return self._reply(200, {"uid": tg_uid})

    # -- user routes ------------------------------------------------------------------------------------

    def _handle_user_routes(self, body: dict) -> None:
        uid = self._parse_uid(self.path)
        if uid is None:
            return self._reply(400, {"error": "bad uid"})
        user = self.state.user(uid)
        if not user:
            return self._reply(404, {"error": "not_found"})

        # acquire work lock for mutating operations
        need_lock = self.path not in (f"/api/users/{uid}",)  # GET already handled
        if need_lock and not self._acquire_work_lock():
            return self._reply(503, {"error": "busy"})
        try:
            if self.path == f"/api/users/{uid}/garmin-key":
                return self._handle_garmin_key(uid, body)
            if self.path == f"/api/users/{uid}/settings":
                return self._handle_settings(uid, body)
            if self.path == f"/api/users/{uid}/sync":
                return self._handle_sync(uid)
            aid = self._parse_activity_id(self.path)
            if aid and self.path.endswith("/upload"):
                return self._handle_activity_upload(uid, aid)
            if aid and self.path.endswith("/skip"):
                return self._handle_activity_skip(uid, aid)
            if self.path == f"/api/users/{uid}/unlink":
                return self._handle_unlink(uid, body)
            return self._reply(404, {"error": "not found"})
        finally:
            if need_lock:
                self._release_work_lock()

    def _handle_garmin_key(self, uid: int, body: dict) -> None:
        key = str(body.get("key") or "").strip()
        if not key:
            return self._reply(400, {"error": "bad_key"})
        ok, garmin_name, err = apply_garmin_key_pure(self.cfg, self.state, uid, key)
        if not ok:
            return self._reply(200, {"ok": False, "error": err or "bad_key"})
        return self._reply(200, {"ok": True, "garmin_name": garmin_name})

    def _handle_settings(self, uid: int, body: dict) -> None:
        # allowed: auto (bool), types (str), visibility (str), lang (en|ru)
        if "auto" in body:
            val = "1" if body["auto"] else "0"
            self.state.set_setting(uid, "auto", val)
        if "types" in body:
            val = str(body["types"]).strip().lower()
            if not expand_types(val):
                return self._reply(400, {"error": "bad_types"})
            self.state.set_setting(uid, "types", val)
        if "visibility" in body:
            val = str(body["visibility"]).strip().lower()
            if val not in ("identifiable", "trackable"):
                return self._reply(400, {"error": "bad_visibility"})
            self.state.set_setting(uid, "visibility", val)
        if "lang" in body:
            val = str(body["lang"]).strip().lower()
            if val not in ("en", "ru"):
                return self._reply(400, {"error": "bad_lang"})
            self.state.set_setting(uid, "lang", val)
        return self._reply(200, {"ok": True})

    def _handle_sync(self, uid: int) -> None:
        user = self.state.user(uid)
        if not user["garmin_linked"]:
            return self._reply(409, {"error": "not_ready"})
        if not user["osm_access_token"]:
            return self._reply(409, {"error": "not_ready"})
        # run user_cycle with a dummy tg (events still written)
        from .telegram import Telegram
        dummy_tg = Telegram("", self.cfg.api_base)
        res = user_cycle(self.cfg, dummy_tg, self.state, user)
        return self._reply(200, {"ok": True, "new": res["seen"], "uploaded": res["uploaded"]})

    def _handle_activity_upload(self, uid: int, aid: str) -> None:
        user = self.state.user(uid)
        row = self.state.activity(uid, aid)
        if row and row["status"] in ("uploaded", "done"):
            return self._reply(400, {"error": "already_uploaded"})
        if not user["garmin_linked"] or not user["osm_access_token"]:
            return self._reply(409, {"error": "not_ready"})
        from .runner import garmin_client_for, summary_row, upload_one, user_vis
        from .garmin import user_token_dir
        from .osm import Osm
        from .redact import redact

        if not row:
            # unknown id: use legacy GarminClient for token-file users, Personal API for key users
            garmin = garmin_client_for(self.cfg, user)
            try:
                raw = garmin.activity(aid)
            except Exception as e:
                log.error("activity lookup failed for %s/%s: %s", uid, aid, redact(e))
                return self._reply(404, {"error": "not_found"})
            row = summary_row(raw)
            self.state.insert_activity(uid, aid, row["name"], row["type"], row["start_ts"],
                                       row["distance_km"], "new")
        else:
            self.state.set_status(uid, aid, "new")

        garmin = garmin_client_for(self.cfg, self.state.user(uid))
        osm = Osm(self.cfg.osm_base, user["osm_access_token"])
        row = self.state.activity(uid, aid)
        trace_id = upload_one(self.cfg, garmin, osm, self.state, user, row, None,
                              user_vis(user, self.cfg), notify=True)
        if not trace_id:
            return self._reply(400, {"error": "upload_failed"})
        row = self.state.activity(uid, aid)
        return self._reply(200, {"ok": True, "status": row["status"]})

    def _handle_activity_skip(self, uid: int, aid: str) -> None:
        row = self.state.activity(uid, aid)
        if not row:
            return self._reply(404, {"error": "not_found"})
        if row["status"] in ("uploaded", "done"):
            return self._reply(400, {"error": "already_uploaded"})
        self.state.set_status(uid, aid, "skipped")
        return self._reply(200, {"ok": True})

    def _handle_unlink(self, uid: int, body: dict) -> None:
        what = str(body.get("what") or "")
        if what == "garmin":
            self.state.unlink_garmin(uid)
        elif what == "osm":
            self.state.unlink_osm(uid)
        else:
            return self._reply(400, {"error": "bad_what"})
        return self._reply(200, {"ok": True})

    # -- user delete ------------------------------------------------------------------------------------

    def do_DELETE(self) -> None:
        if not self._authorized():
            return self._reply(401, {"error": "unauthorized"})
        if self.path.startswith("/api/users/"):
            uid = self._parse_uid(self.path)
            if uid is None:
                return self._reply(400, {"error": "bad uid"})
            user = self.state.user(uid)
            if not user:
                return self._reply(404, {"error": "not_found"})
            if not self._acquire_work_lock():
                return self._reply(503, {"error": "busy"})
            try:
                self.state.delete_user(uid)
                # legacy token dir
                from .garmin import user_token_dir
                import shutil
                d = user_token_dir(self.cfg.garmin_token_dir, uid)
                if os.path.exists(d):
                    shutil.rmtree(d, ignore_errors=True)
                return self._reply(200, {"ok": True})
            finally:
                self._release_work_lock()
        return self._reply(404, {"error": "not found"})

    # -- helpers -----------------------------------------------------------------------------------------

    def _build_user_response(self, user: dict) -> dict:
        from .runner import expand_types_user, user_vis
        uid = user["user_id"]
        counts = self.state.counts(uid)
        activities = self.state.recent_activities(uid, 30)
        # actions per activity
        for act in activities:
            actions = []
            if act["status"] in ("new", "backlog", "ignored", "skipped", "failed"):
                actions.append("upload")
            if act["status"] in ("new", "backlog", "ignored", "skipped", "failed"):
                actions.append("skip")
            act["actions"] = actions
        return {
            "uid": uid,
            "channel": user.get("channel") or "telegram",
            "lang": user.get("lang") or "en",
            "osm": {"linked": bool(user.get("osm_access_token")), "name": user.get("osm_name")},
            "garmin": {
                "linked": bool(user.get("garmin_linked")),
                "name": user.get("garmin_name"),
                "failing": False,  # could track recent failures if needed
            },
            "settings": {
                "auto": bool(user.get("auto")),
                "types": user.get("types") or self.cfg.default_types,
                "visibility": user_vis(user, self.cfg),
            },
            "type_options": list(TYPE_GROUPS.keys()),
            "delay_minutes": self.cfg.upload_delay_minutes,
            "counts": counts,
            "activities": activities,
            "events": self.state.events(uid, 30),
        }

    def log_message(self, *args) -> None:
        pass


def make_handler(cfg: Config, state: State, runner: Runner) -> type[BaseHTTPRequestHandler]:
    class Handler(ApiHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(cfg, state, runner, *args, **kwargs)
    return Handler


def serve(port: int, token: str, on_garmin, on_osm, cfg: Config, state: State, runner: Runner) -> ThreadingHTTPServer:
    if not token:
        raise RuntimeError("INGEST_TOKEN is empty: refusing to serve")
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(cfg, state, runner))
    threading.Thread(target=server.serve_forever, daemon=True, name="ingest").start()
    return server


def token_from_env() -> str:
    return os.environ.get("INGEST_TOKEN", "")