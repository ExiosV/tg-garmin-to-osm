"""Cluster-internal HTTP ingest + web account API: the web site (garmin-osm.exiosv.com) drives
sign-up and all settings through this API. The bot process stays the single writer of its SQLite
and PVC. Bearer-token authenticated, fail-closed; no Ingress, so nothing outside the cluster sees it.
"""
import hmac
import json
import logging
import os
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from . import osm as osmmod
from .config import TYPE_GROUPS, VISIBILITIES, Config
from .garmin import user_token_dir
from .osm import Osm
from .backfill import backfill_status
from .metrics import METRICS
from .redact import redact
from .runner import (WORK_LOCK, Runner, apply_garmin_key_pure, garmin_client_for, ingest_garmin_link,
                     ingest_osm_link, summary_row, upload_one, user_cycle, user_vis)
from .state import State

log = logging.getLogger("tg_garmin_to_osm")

# what the web may offer per activity status: /upload works for every not-yet-uploaded row,
# /skip only matters for rows the cycle would upload (backlog/ignored never auto-upload)
UPLOAD_STATUSES = ("new", "backlog", "ignored", "skipped", "failed")
SKIP_STATUSES = ("new", "failed")


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
        if not self.cfg.ingest_token:
            return False
        try:  # bytes: compare_digest on str raises TypeError for non-ASCII input
            header = (self.headers.get("Authorization") or "").encode()
            return hmac.compare_digest(header, f"Bearer {self.cfg.ingest_token}".encode())
        except Exception:
            return False

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
        return WORK_LOCK.acquire(timeout=20)

    def _release_work_lock(self) -> None:
        WORK_LOCK.release()

    def do_GET(self) -> None:
        if self.path == "/ping":
            return self._reply(200, {"ok": True})
        if self.path == "/metrics":
            body = METRICS.render(self.cfg.data_dir, self.cfg).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/api/users/"):
            if not self._authorized():
                return self._reply(401, {"error": "unauthorized"})
            uid = self._parse_uid(self.path)
            if uid is None:
                return self._reply(400, {"error": "bad uid"})
            user = self.state.user(uid)
            if not user:
                return self._reply(404, {"error": "not_found"})
            if self.path == f"/api/users/{uid}/backfill":
                from .backfill import backfill_status
                return self._reply(200, {"ok": True, "status": backfill_status(self.state, uid)})
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
                ingest_garmin_link(self.cfg, self.runner.tg, self.state, uid, tokens)
                METRICS.inc("garmin_links_total", method="site_tokens")
                return self._reply(200, {"ok": True})

            if self.path == "/ingest/osm":
                uid = int(body["uid"])
                access_token = str(body["access_token"])
                name = str(body.get("name") or "")
                if not access_token:
                    raise ValueError("access_token is empty")
                osm_uid = int(body["osm_uid"]) if body.get("osm_uid") is not None else None
                if osm_uid is not None:
                    existing = self.state.find_by_osm_uid(osm_uid)
                    if existing and existing["user_id"] != uid:
                        return self._reply(409, {"error": "osm_taken"})
                ingest_osm_link(self.cfg, self.runner.tg, self.state, uid, access_token, name, osm_uid)
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

        # A web user who unlinked OSM keeps the row -osm_uid with osm_uid NULL: same OSM account, reuse it.
        existing = self.state.find_by_osm_uid(osm_uid) or self.state.user(-osm_uid)
        if existing:  # token + name only: the user's language stays as it is
            self.state.save_osm_user(existing["user_id"], existing["chat_id"], access_token, osm_name, osm_uid)
            return self._reply(200, {"uid": existing["user_id"], "created": False})

        # new web user
        if self.state.count_web_users() >= self.cfg.max_web_users:
            return self._reply(403, {"error": "signup_closed"})

        uid = -osm_uid

        chat_id = None
        self.state.touch_user(uid, chat_id, lang)
        self.state.save_osm_user(uid, chat_id, access_token, osm_name, osm_uid)
        self.state.set_channel(uid, "web")
        log.info("created web user %s (osm_uid=%s)", uid, osm_uid)
        return self._reply(200, {"uid": uid, "created": True})

    def _handle_accounts_telegram(self, body: dict) -> None:
        tg_uid = int(body["tg_uid"])
        lang = str(body.get("lang") or "en")
        if lang not in ("en", "ru"):
            lang = "en"
        # ensure row exists with chat_id = tg_uid
        self.state.touch_user(tg_uid, tg_uid, lang)
        self.state.set_channel(tg_uid, "telegram")
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
            if self.path == f"/api/users/{uid}/backfill":
                return self._handle_backfill_start(uid)
            if self.path == f"/api/users/{uid}/backfill/cancel":
                from .backfill import cancel_backfill
                cancel_backfill(self.state, uid)
                METRICS.inc("backfills_cancelled_total")
                return self._reply(200, {"ok": True})
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
        ok, garmin_name, err, _ = apply_garmin_key_pure(self.cfg, self.state, uid, key)
        if not ok:
            return self._reply(200, {"ok": False, "error": err or "bad_key"})
        return self._reply(200, {"ok": True, "garmin_name": garmin_name})

    def _handle_settings(self, uid: int, body: dict) -> None:
        # validate every field first, then write: a bad field changes nothing
        changes = {}
        if "auto" in body:
            if not isinstance(body["auto"], bool):
                return self._reply(400, {"error": "bad_auto"})
            changes["auto"] = "1" if body["auto"] else "0"
        if "types" in body:
            val = str(body["types"] or "").strip().lower()
            if not val:
                return self._reply(400, {"error": "bad_types"})
            changes["types"] = val
        if "visibility" in body:
            val = str(body["visibility"] or "").strip().lower()
            if val not in VISIBILITIES:
                return self._reply(400, {"error": "bad_visibility"})
            changes["visibility"] = val
        if "lang" in body:
            val = str(body["lang"] or "").strip().lower()
            if val not in ("en", "ru"):
                return self._reply(400, {"error": "bad_lang"})
            changes["lang"] = val
        for column, val in changes.items():
            self.state.set_setting(uid, column, val)
        return self._reply(200, {"ok": True})

    def _handle_backfill_start(self, uid: int) -> None:
        """Start "upload ALL history + keep auto-uploading new rides" in a background thread."""
        user = self.state.user(uid)
        if not user["garmin_linked"] or not user["osm_access_token"]:
            return self._reply(409, {"error": "not_ready"})
        from .backfill import start_user_backfill
        res = start_user_backfill(self.cfg, self.runner.tg, self.state, user)
        if not res["ok"]:
            return self._reply(409, res)
        METRICS.inc("backfills_started_total")
        return self._reply(200, {"ok": True, "status": res["status"]})

    def _handle_sync(self, uid: int) -> None:
        user = self.state.user(uid)
        if not user["garmin_linked"]:
            return self._reply(409, {"error": "not_ready"})
        if not user["osm_access_token"]:
            return self._reply(409, {"error": "not_ready"})
        res = user_cycle(self.cfg, self.runner.tg, self.state, user)
        return self._reply(200, {"ok": True, "new": res["seen"], "uploaded": res["uploaded"]})

    def _handle_activity_upload(self, uid: int, aid: str) -> None:
        user = self.state.user(uid)
        row = self.state.activity(uid, aid)
        if row and row["status"] in ("uploaded", "done"):
            return self._reply(400, {"error": "already_uploaded"})
        if not user["garmin_linked"] or not user["osm_access_token"]:
            return self._reply(409, {"error": "not_ready"})
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
        trace_id = upload_one(self.cfg, garmin, osm, self.state, user, row, self.runner.tg,
                              user_vis(user, self.cfg), notify=True)
        if not trace_id:
            row = self.state.activity(uid, aid) or {}
            err = redact(row.get("last_error") or "unknown error")
            if err.startswith("no_track"):
                detail = err[len("no_track"):].lstrip(": ").strip() or "unknown error"
                return self._reply(422, {"error": "no_track", "reason": detail[:200]})
            return self._reply(422, {"error": "upload_failed", "reason": err[:200]})
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
                d = user_token_dir(self.cfg.garmin_token_dir, uid)
                if os.path.exists(d):
                    shutil.rmtree(d, ignore_errors=True)
                return self._reply(200, {"ok": True})
            finally:
                self._release_work_lock()
        return self._reply(404, {"error": "not found"})

    # -- helpers -----------------------------------------------------------------------------------------

    def _activity_view(self, user: dict, act: dict, edit_urls: dict) -> dict:
        """One activities row in the contract shape (internal columns dropped)."""
        trace_id = act.get("trace_id")
        trace_url = edit_url = None
        if trace_id:
            trace_url = osmmod.trace_link(self.cfg.osm_base, trace_id, user.get("osm_name") or "")
            if act["status"] == "done":
                edit_url = (edit_urls.get(act["activity_id"])
                            or f"{self.cfg.osm_base.rstrip('/')}/edit?editor=id&gpx={int(trace_id)}")
        actions = []
        if act["status"] in UPLOAD_STATUSES:
            actions.append("upload")
        if act["status"] in SKIP_STATUSES:
            actions.append("skip")
        return {"activity_id": act["activity_id"], "name": act["name"] or "", "type": act["type"] or "",
                "start_ts": act["start_ts"] or 0, "distance_km": act["distance_km"],
                "status": act["status"], "trace_id": trace_id, "trace_url": trace_url,
                "edit_url": edit_url, "last_error": act["last_error"], "actions": actions}

    def _build_user_response(self, user: dict) -> dict:
        uid = user["user_id"]
        # edit links come from the newest 'processed' event of each activity
        edit_urls: dict = {}
        for ev in self.state.events(uid, 200):  # newest first
            if ev["kind"] == "processed" and ev["data"].get("edit_url"):
                edit_urls.setdefault(str(ev["data"].get("activity_id")), ev["data"]["edit_url"])
        activities = [self._activity_view(user, a, edit_urls) for a in self.state.recent_activities(uid, 200)]
        return {
            "uid": uid,
            "channel": user.get("channel") or "telegram",
            "lang": user.get("lang") or "en",
            "osm": {"linked": bool(user.get("osm_access_token")), "name": user.get("osm_name") or None},
            "garmin": {
                "linked": bool(user.get("garmin_linked")),
                "name": user.get("garmin_name") or None,
                "failing": bool(self.state.get_meta(f"garmin_fail:{uid}")),
            },
            "settings": {
                "auto": bool(user.get("auto")),
                "types": user.get("types") or self.cfg.default_types,
                "visibility": user_vis(user, self.cfg),
            },
            "type_options": list(TYPE_GROUPS.keys()),
            "delay_minutes": self.cfg.upload_delay_minutes,
            "counts": self.state.counts(uid),
            "activities": activities,
            "events": self.state.events(uid, 30),
            "backfill": backfill_status(self.state, uid),
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
