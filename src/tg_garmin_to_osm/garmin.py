"""Garmin Connect client (the `garminconnect` library).

Multi-user linking happens in the bot chat: the user sends email, then password (both messages are
deleted immediately and nothing is stored), then an MFA code if Garmin asks for one. Only the
resulting token store (garmin_tokens.json, one directory per user on the PVC) is persisted —
`garminconnect` 0.3.20 refreshes it automatically, so the password is needed exactly once.
"""
import json
import logging
import os
import threading
import time
from pathlib import Path

from .redact import redact

log = logging.getLogger("tg_garmin_to_osm")

SSO_EMBED = "https://sso.garmin.com/sso/embed"


def is_auth_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(hint in text for hint in
               ("auth", "401", "403", "unauthorized", "forbidden", "login", "credential", "token"))


def user_token_dir(base_dir: str, user_id: int) -> str:
    """One token directory per Telegram user: <base>/<uid>/garmin_tokens.json."""
    return str(Path(base_dir).expanduser() / str(user_id))


def begin_login(email: str, password: str) -> tuple:
    """First leg of the chat linking flow. Returns ("ok", api, None) or
    ("needs_mfa", api, client_state_json). Never logs the credentials; the caller shows a generic
    error instead of the exception (it can carry account details)."""
    from garminconnect import Garmin

    api = Garmin(email=email, password=password, return_on_mfa=True, prompt_mfa=None)
    status, client_state = api.login()
    if status == "needs_mfa":
        return "needs_mfa", api, json.dumps(client_state)
    return "ok", api, None


def finish_mfa(api, client_state_json: str, code: str, token_dir: str) -> None:
    """Complete an MFA login and persist the tokens."""
    api.resume_login(json.loads(client_state_json), code)
    api.client.dump(str(Path(token_dir).expanduser()))


def save_tokens(api, token_dir: str) -> None:
    """Persist tokens after a clean (non-MFA) login."""
    api.client.dump(str(Path(token_dir).expanduser()))


class GarminClient:
    """One lazy, locked Garmin client working on a per-user token store. No credentials are kept:
    a stale store means the user has to /garmin again."""

    def __init__(self, token_dir: str):
        self.token_dir = str(Path(token_dir).expanduser())
        self._api = None
        self._lock = threading.Lock()
        self._courses = None  # lazy {courseId: courseName} cache for this user's own courses

    def _client(self):
        with self._lock:
            if self._api is None:
                from garminconnect import Garmin  # imported lazily: heavy, and not needed to import the module

                api = Garmin()  # no credentials: only saved tokens
                api.login(self.token_dir)
                self._api = api
            return self._api

    def reset(self) -> None:
        """Drop the logged-in client so the next call logs in again (after an auth-looking error)."""
        with self._lock:
            self._api = None

    def recent_activities(self, limit: int = 10) -> list:
        """The newest `limit` activities, newest first, as raw Garmin summary dicts."""
        return self.activities_page(0, limit)

    def activities_page(self, offset: int, limit: int = 100) -> list:
        """One page of activity summaries, newest first (for deep history scans)."""
        api = self._client()
        return api.get_activities(int(offset), int(limit)) or []

    def activity(self, activity_id) -> dict:
        return self._client().get_activity(int(activity_id))

    def download_gpx(self, activity_id) -> bytes:
        api = self._client()
        data = api.download_activity(int(activity_id), dl_fmt=_gpx_format())
        return data if isinstance(data, bytes) else bytes(data)

    def course_name(self, course_id: int) -> str:
        """Name of one of this user's own courses; '' for foreign/deleted ids (get_course 404s on
        courses that are not the account's own, so the name is resolved through get_courses)."""
        if self._courses is None:
            try:
                self._courses = {int(c.get("courseId") or 0): (c.get("courseName") or "").strip()
                                 for c in self._client().get_courses()}
            except Exception as e:
                log.warning("course list fetch failed: %s", redact(e))
                self._courses = {}
        return self._courses.get(int(course_id), "")


def course_id(detail: dict) -> int | None:
    """The Garmin course a ride followed (activity detail, metadataDTO); None when it rode free."""
    cid = (detail or {}).get("metadataDTO", {}).get("associatedCourseId")
    try:
        return int(cid) if cid else None
    except (TypeError, ValueError):
        return None


def _gpx_format():
    """The library's GPX download enum, resolved defensively across garminconnect versions."""
    from garminconnect import Garmin

    enum_cls = getattr(Garmin, "ActivityDownloadFormat", None) or getattr(Garmin, "DownloadFormat")
    return enum_cls.GPX
