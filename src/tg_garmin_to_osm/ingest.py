"""Cluster-internal HTTP ingest: the web site (garmin-osm.exiosv.com) delivers account links to the
bot here — the bot stays the single writer of its SQLite and PVC. Bearer-token authenticated,
fail-closed (health-sync scale-ingest pattern); no Ingress, so nothing outside the cluster sees it."""
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .redact import redact

log = logging.getLogger("tg_garmin_to_osm")


def make_handler(token: str, on_garmin, on_osm) -> type[BaseHTTPRequestHandler]:
    """on_garmin(uid, tokens_json) / on_osm(uid, access_token, display_name): the domain callbacks."""

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            import hmac
            header = self.headers.get("Authorization") or ""
            return bool(token) and hmac.compare_digest(header, f"Bearer {token}")

        def do_GET(self) -> None:
            if self.path == "/ping":  # health/readiness, no auth
                return self._reply(200, {"ok": True})
            return self._reply(404, {"error": "not found"})

        def do_POST(self) -> None:
            if not self._authorized():
                return self._reply(401, {"error": "unauthorized"})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            except (ValueError, json.JSONDecodeError):
                return self._reply(400, {"error": "bad json"})
            try:
                if self.path == "/ingest/garmin":
                    uid = int(body["uid"])
                    tokens = str(body["tokens"])
                    if not tokens.strip().startswith("{"):
                        raise ValueError("tokens must be the garmin_tokens.json content")
                    on_garmin(uid, tokens)
                elif self.path == "/ingest/osm":
                    uid = int(body["uid"])
                    access_token = str(body["access_token"])
                    name = str(body.get("name") or "")
                    if not access_token:
                        raise ValueError("access_token is empty")
                    on_osm(uid, access_token, name)
                else:
                    return self._reply(404, {"error": "not found"})
            except (KeyError, TypeError, ValueError) as e:
                return self._reply(400, {"error": str(e)[:200]})
            except Exception as e:  # never leak internals; the domain error is logged by the callback
                log.error("ingest %s failed: %s", self.path, redact(e))
                return self._reply(500, {"error": "internal"})
            self._reply(200, {"ok": True})

        def log_message(self, *args) -> None:  # silence per-request noise
            pass

    return Handler


def serve(port: int, token: str, on_garmin, on_osm) -> ThreadingHTTPServer:
    if not token:  # fail closed: an empty token would accept "Authorization: Bearer "
        raise RuntimeError("INGEST_TOKEN is empty: refusing to serve")
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(token, on_garmin, on_osm))
    threading.Thread(target=server.serve_forever, daemon=True, name="ingest").start()
    return server


def token_from_env() -> str:
    return os.environ.get("INGEST_TOKEN", "")
