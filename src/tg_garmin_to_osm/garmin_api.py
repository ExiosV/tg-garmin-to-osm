"""Garmin Connect Personal API (official, self-service): the user generates an API key in their
Garmin Connect settings (https://connect.garmin.com/app/settings/personalApiToken, read-only, their
own data), the key is exchanged for a bearer token, and the bearer calls the endpoints below.
Docs: developer.garmin.com/gc-developer-program/personal/personal-api."""
import io
import json
import logging
import threading
import zipfile

import requests

from .redact import redact

log = logging.getLogger("tg_garmin_to_osm")

BASE = "https://personal.connectapi.garmin.com"
TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"
SEARCH_PATH = "/activitylist-service/activities/search/activities"
DETAIL_PATH = "/activity-service/activity/{id}"
GPX_PATH = "/download-service/export/gpx/activity/{id}"
FIT_PATH = "/download-service/files/activity/{id}"


class GarminApiError(RuntimeError):
    def __init__(self, what: str, status: int, detail: str = ""):
        super().__init__(f"{what}: HTTP {status}" + (f": {detail}" if detail else ""))
        self.what, self.status, self.detail = what, status, detail


def is_auth_error(exc: Exception) -> bool:
    return isinstance(exc, GarminApiError) and exc.status in (401, 403)


class GarminApiClient:
    """Official Personal API client for one user's API key. The bearer token is exchanged lazily and
    re-exchanged once on auth failures (key expired/revoked -> the caller asks for a new key)."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self._bearer = None
        self._lock = threading.Lock()

    def _token(self) -> str:
        with self._lock:
            if not self._bearer:
                r = requests.post(BASE + TOKEN_PATH, headers={"Authorization": f"ApiKey {self.api_key}"},
                                  timeout=30)
                if r.status_code != 200:
                    raise GarminApiError("api key exchange failed", r.status_code, r.text[:200])
                token = r.text.strip().strip('"')
                if not token:
                    raise GarminApiError("api key exchange failed", r.status_code, "empty token")
                self._bearer = token
            return self._bearer

    def _reset_token(self) -> None:
        with self._lock:
            self._bearer = None

    def _get(self, path: str, ok=(200,), timeout: int = 120) -> requests.Response:
        r = requests.get(BASE + path, headers={"Authorization": f"Bearer {self._token()}"},
                         timeout=timeout)
        if r.status_code in (401, 403):  # token expired: one clean re-exchange, then give up
            self._reset_token()
            r = requests.get(BASE + path, headers={"Authorization": f"Bearer {self._token()}"},
                             timeout=timeout)
        if r.status_code not in ok:
            raise GarminApiError(f"GET {path.split('?')[0]} failed", r.status_code, r.text[:200])
        return r

    # -- the same duck-typed surface GarminClient offers to the runner -------------------------------

    def recent_activities(self, limit: int = 10) -> list:
        return self.activities_page(0, limit)

    def activities_page(self, offset: int, limit: int = 100) -> list:
        r = self._get(f"{SEARCH_PATH}?start={int(offset)}&limit={int(limit)}")
        return json.loads(r.text) or []

    def activity(self, activity_id) -> dict:
        r = self._get(DETAIL_PATH.format(id=int(activity_id)))
        return json.loads(r.text)

    def download_gpx(self, activity_id) -> bytes:
        """GPX export when the endpoint exists; otherwise the FIT file converted to GPX."""
        aid = int(activity_id)
        try:
            r = self._get(GPX_PATH.format(id=aid), ok=(200, 404), timeout=180)
        except GarminApiError as e:
            if e.status not in (404,):
                raise
            return b""
        if r.status_code == 200 and r.content.lstrip()[:5] == b"<?xml":
            return r.content
        # Garmin returns a ZIP for many activities (e.g. walking): unwrap what we already have
        # instead of downloading the same file a second time.
        if r.status_code == 200 and r.content.lstrip()[:2] == b"PK":
            return fit_to_gpx(_unwrap_fit(r.content))
        return fit_to_gpx(self.download_fit(aid))

    def download_fit(self, activity_id) -> bytes:
        r = self._get(FIT_PATH.format(id=int(activity_id)), timeout=180)
        return _unwrap_fit(r.content)


def _unwrap_fit(data: bytes) -> bytes:
    """Unwrap a ZIP archive to its single .fit member, or return data unchanged.

    If `data` starts with b"PK" (a ZIP), open it and return the bytes of the
    single member whose name ends with ".fit" (case-insensitive).  If there is
    no such member, raise ValueError naming the members found.  Otherwise
    return `data` unchanged (plain FIT bytes).
    """
    if data.lstrip()[:2] != b"PK":
        return data
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        members = zf.namelist()
        fit_members = [m for m in members if m.lower().endswith(".fit")]
        if not fit_members:
            raise ValueError(
                f"ZIP archive contains no .fit file; members: {members}"
            )
        if len(fit_members) > 1:
            raise ValueError(
                f"ZIP archive contains {len(fit_members)} .fit files; "
                f"expected one: {fit_members}"
            )
        return zf.read(fit_members[0])


def _degrees(value) -> float:
    """FIT stores positions as semicircles (ints); fitdecode passes them through unconverted.
    Floats (already degrees) pass through — robust to library changes."""
    if isinstance(value, int):
        return value * 180.0 / (1 << 31)
    return float(value)


def fit_to_gpx(fit: bytes) -> bytes:
    """Minimal FIT -> GPX1.1 conversion: track records with a position and a timestamp become
    <trkpt><time> (exactly what OSM traces need)."""
    import xml.etree.ElementTree as ET

    import fitdecode

    gpx = ET.Element("gpx", {"version": "1.1", "creator": "tg-garmin-to-osm"})
    trk = ET.SubElement(gpx, "trk")
    ET.SubElement(trk, "name").text = "Activity"
    seg = ET.SubElement(trk, "trkseg")
    points = 0
    for frame in fitdecode.FitReader(fit):
        # Only record messages carry positions; other data frames (file_id, session, lap, ...)
        # have no such fields and fitdecode raises KeyError on a missing field.
        if frame.frame_type != fitdecode.FIT_FRAME_DATA or getattr(frame, "name", "") != "record":
            continue
        lat = frame.get_value("position_lat", fallback=None)
        lon = frame.get_value("position_long", fallback=None)
        ts = frame.get_value("timestamp", fallback=None)
        if lat is None or lon is None or ts is None:
            continue
        pt = ET.SubElement(seg, "trkpt", {
            "lat": f"{_degrees(lat):.6f}",
            "lon": f"{_degrees(lon):.6f}",
        })
        ET.SubElement(pt, "time").text = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
        points += 1
    log.info("fit->gpx: %d points", points)
    return ET.tostring(gpx, xml_declaration=True, encoding="UTF-8")
