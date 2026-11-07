"""OpenStreetMap API 0.6 client: OAuth2 login (confidential app + out-of-band code), GPX trace upload,
trace metadata. Docs: wiki.openstreetmap.org/wiki/API_v0.6#GPS_traces."""
import logging
import xml.etree.ElementTree as ET
from urllib.parse import urlencode

import requests

log = logging.getLogger("tg_garmin_to_osm")

OOB_REDIRECT = "urn:ietf:wg:oauth:2.0:oob"  # osm.org shows the code on a page for copy-paste
SCOPES = "read_prefs read_gpx write_gpx"
VISIBILITIES = ("identifiable", "trackable")  # public/private are deprecated for API uploads (2026-08)


class OsmError(RuntimeError):
    """OSM API call failed. `status` is the HTTP status, 0 = transport error."""

    def __init__(self, what: str, status: int, detail: str = ""):
        super().__init__(f"{what}: HTTP {status}" + (f": {detail}" if detail else ""))
        self.what, self.status, self.detail = what, status, detail


def authorize_url(base: str, client_id: str) -> str:
    """The URL to open in a browser; OSM then shows the code the user pastes into the chat."""
    return f"{base.rstrip('/')}/oauth2/authorize?{urlencode({
        'client_id': client_id, 'response_type': 'code', 'redirect_uri': OOB_REDIRECT, 'scope': SCOPES})}"


def exchange_code(base: str, client_id: str, client_secret: str, code: str) -> dict:
    """Swap the pasted code for an access token (OSM access tokens do not expire automatically)."""
    r = requests.post(f"{base.rstrip('/')}/oauth2/token", data={
        "grant_type": "authorization_code", "code": code, "client_id": client_id,
        "client_secret": client_secret, "redirect_uri": OOB_REDIRECT,
    }, timeout=30)
    if r.status_code != 200:
        raise OsmError("token exchange failed", r.status_code, r.text[:200])
    token = r.json().get("access_token")
    if not token:
        raise OsmError("token exchange failed", r.status_code, "no access_token in response")
    return r.json()


class Osm:
    """Authenticated API 0.6 calls for one logged-in user."""

    def __init__(self, base: str, access_token: str):
        self.base = base.rstrip("/")
        self.token = access_token

    def _call(self, method: str, path: str, ok=(200,), timeout: int = 60, **kwargs) -> requests.Response:
        headers = dict(kwargs.pop("headers", {}), Authorization=f"Bearer {self.token}")
        try:
            r = requests.request(method, f"{self.base}{path}", headers=headers, timeout=timeout, **kwargs)
        except requests.RequestException as e:
            raise OsmError(f"{method} {path} failed", 0, str(e)) from None
        if r.status_code not in ok:
            raise OsmError(f"{method} {path} failed", r.status_code, r.text[:200])
        return r

    def user_name(self) -> str:
        data = self._call("GET", "/api/0.6/user/details.json").json()
        return (data.get("user") or {}).get("display_name") or ""

    def upload_gpx(self, gpx_bytes: bytes, description: str, tags: str = "",
                   visibility: str = "identifiable") -> int:
        """Upload one GPX as a new trace; returns the trace id. The trace starts `pending` and is processed
        asynchronously (the upload succeeds even for a file OSM later rejects)."""
        if not description.strip():
            raise OsmError("upload_gpx", 0, "description must not be empty")
        if visibility not in VISIBILITIES:
            raise OsmError("upload_gpx", 0, f"visibility must be one of {VISIBILITIES}")
        r = self._call("POST", "/api/0.6/gpx", files={
            "file": ("track.gpx", gpx_bytes, "application/gpx+xml"),
        }, data={"description": description[:255], "tags": tags, "visibility": visibility})
        return int(r.text.strip())

    def trace_meta(self, trace_id: int) -> dict:
        """Trace metadata: {id, pending, lat, lon, ...}. lat/lon is the trace's first point once processed."""
        data = self._call("GET", f"/api/0.6/gpx/{int(trace_id)}.json").json()
        meta = data.get("gpx_file") or {}
        return {
            "id": meta.get("id"),
            "pending": bool(meta.get("pending")),
            "lat": meta.get("lat"),
            "lon": meta.get("lon"),
            "description": meta.get("description"),
        }

    def user_traces(self) -> list:
        """All of the authenticated user's traces (used by the backfill to skip rides already on OSM):
        [{id, name, description, timestamp, visibility, pending}]."""
        r = self._call("GET", "/api/0.6/user/gpx_files")
        out = []
        for el in ET.fromstring(r.content).iter():
            if el.tag.rsplit("}", 1)[-1] != "gpx_file":
                continue
            desc = el.get("description")
            if desc is None:
                child = next((c for c in el if c.tag.rsplit("}", 1)[-1] == "description"), None)
                desc = child.text if child is not None else None
            out.append({
                "id": el.get("id"),
                "name": el.get("name"),
                "description": desc,
                "timestamp": el.get("timestamp"),
                "visibility": el.get("visibility"),
                "pending": el.get("pending"),
            })
        return out


def trace_link(base: str, trace_id: int, user_name: str = None) -> str:
    """Trace page. OSM (2026) serves it at /user/<name>/traces/<id>; the old /traces/<id> is 404."""
    if user_name:
        return f"{base.rstrip('/')}/user/{user_name}/traces/{int(trace_id)}"
    return f"{base.rstrip('/')}/traces/{int(trace_id)}"


def edit_link(base: str, trace_id: int, lat, lon, user_name: str = None) -> str:
    """iD with this trace loaded as a GPX layer; falls back to the trace page when OSM has no start point yet."""
    if lat is None or lon is None:
        return trace_link(base, trace_id, user_name)
    return f"{base.rstrip('/')}/edit?editor=id&gpx={int(trace_id)}#map=17/{float(lat):.5f}/{float(lon):.5f}"
