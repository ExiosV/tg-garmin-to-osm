import json
import urllib.request

import pytest

from tg_garmin_to_osm.ingest import make_handler, serve


def post(port, path, token, body):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        data=json.dumps(body).encode())
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def test_ingest_server_end_to_end():
    got = []

    def on_garmin(uid, tokens):
        got.append(("garmin", uid, tokens))

    def on_osm(uid, access_token, name):
        got.append(("osm", uid, access_token, name))

    server = serve(0, "SECRETTOKEN1", on_garmin, on_osm)
    port = server.server_address[1]
    try:
        assert urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=5).status == 200
        code, body = post(port, "/ingest/garmin", "SECRETTOKEN1", {"uid": 42, "tokens": '{"a": 1}'})
        assert (code, body["ok"]) == (200, True)
        code, _ = post(port, "/ingest/garmin", "WRONG", {"uid": 42, "tokens": "{}"})
        assert code == 401
        code, _ = post(port, "/ingest/garmin", "SECRETTOKEN1", {"uid": 42, "tokens": "not-json"})
        assert code == 400
        code, _ = post(port, "/ingest/osm", "SECRETTOKEN1", {"uid": 42})
        assert code == 400  # access_token missing
        code, _ = post(port, "/ingest/other", "SECRETTOKEN1", {})
        assert code == 404
        assert got[0] == ("garmin", 42, '{"a": 1}')
    finally:
        server.shutdown()


def test_serve_refuses_empty_token():
    with pytest.raises(RuntimeError):
        serve(0, "", lambda *a: None, lambda *a: None)
