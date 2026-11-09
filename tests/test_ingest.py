import json
import urllib.request

import pytest

from tg_garmin_to_osm.config import Config
from tg_garmin_to_osm.ingest import serve
from tg_garmin_to_osm.runner import Runner
from tg_garmin_to_osm.state import State
from tg_garmin_to_osm.telegram import Telegram


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


def make_test_runner(tmp_path):
    cfg = Config(token="", ingest_token="SECRETTOKEN1", data_dir=str(tmp_path), ingest_port=0,
                 garmin_token_dir=str(tmp_path / "garmin_tokens"))
    state = State(cfg.data_dir)
    tg = Telegram(cfg.token, cfg.api_base)
    runner = Runner(cfg, tg, state)
    return cfg, state, runner


def test_ingest_server_end_to_end(tmp_path):
    cfg, state, runner = make_test_runner(tmp_path)
    server = serve(0, "SECRETTOKEN1", lambda *a: None, lambda *a: None, cfg, state, runner)
    port = server.server_address[1]
    # wait for server to be ready
    import time
    for _ in range(100):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=1) as r:
                if r.status == 200:
                    break
        except Exception:
            time.sleep(0.02)
    try:
        assert urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=5).status == 200
        code, body = post(port, "/ingest/garmin", "SECRETTOKEN1", {"uid": 42, "tokens": '{"a": 1}'})
        assert (code, body["ok"]) == (200, True)
        # verify garmin tokens were written
        import os
        token_file = os.path.join(cfg.garmin_token_dir, "42", "garmin_tokens.json")
        assert os.path.exists(token_file)
        assert state.user(42)["garmin_linked"] == 1
        code, _ = post(port, "/ingest/garmin", "WRONG", {"uid": 42, "tokens": "{}"})
        assert code == 401
        code, _ = post(port, "/ingest/garmin", "SECRETTOKEN1", {"uid": 42, "tokens": "not-json"})
        assert code == 400
        code, _ = post(port, "/ingest/osm", "SECRETTOKEN1", {"uid": 42})
        assert code == 400  # access_token missing
        code, _ = post(port, "/ingest/other", "SECRETTOKEN1", {})
        assert code == 404
    finally:
        server.shutdown()


def test_serve_refuses_empty_token(tmp_path):
    cfg, state, runner = make_test_runner(tmp_path)
    with pytest.raises(RuntimeError):
        serve(0, "", lambda *a: None, lambda *a: None, cfg, state, runner)


def test_ingest_osm_link(tmp_path):
    cfg, state, runner = make_test_runner(tmp_path)
    server = serve(0, "SECRETTOKEN1", lambda *a: None, lambda *a: None, cfg, state, runner)
    port = server.server_address[1]
    import time
    for _ in range(100):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=1) as r:
                if r.status == 200:
                    break
        except Exception:
            time.sleep(0.02)
    try:
        code, body = post(port, "/ingest/osm", "SECRETTOKEN1",
                          {"uid": 42, "access_token": "OSMTKN", "name": "mapper"})
        assert (code, body["ok"]) == (200, True)
        user = state.user(42)
        assert user["osm_access_token"] == "OSMTKN"
        assert user["osm_name"] == "mapper"
    finally:
        server.shutdown()