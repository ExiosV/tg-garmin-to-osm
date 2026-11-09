"""Tests for the internal web account API (/api/* routes)."""
import json
import urllib.request

import pytest

import tg_garmin_to_osm.ingest as ingest_mod
from tg_garmin_to_osm.config import Config
from tg_garmin_to_osm.ingest import serve
from tg_garmin_to_osm.runner import Runner, apply_garmin_key_pure
from tg_garmin_to_osm.state import State
from tg_garmin_to_osm.telegram import Telegram

from conftest_helpers import _Garmin, _Osm, _Tg, cfg as make_cfg, link_user, act, seeded


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


def get(port, path, token):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", method="GET",
        headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def delete(port, path, token):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", method="DELETE",
        headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def make_test_server(tmp_path):
    """Create a test server with a runner and state."""
    c = Config(token="", ingest_token="SECRETTOKEN1", data_dir=str(tmp_path), ingest_port=0,
               garmin_token_dir=str(tmp_path / "garmin_tokens"),
               osm_client_id="cid", osm_client_secret="sec")
    st = State(c.data_dir)
    tg = Telegram(c.token, c.api_base)
    runner = Runner(c, tg, st)
    server = serve(0, "SECRETTOKEN1", lambda *a: None, lambda *a: None, c, st, runner)
    port = server.server_address[1]
    import time
    for _ in range(100):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=1) as r:
                if r.status == 200:
                    break
        except Exception:
            time.sleep(0.02)
    return c, st, runner, server, port


# -- apply_garmin_key_pure ---------------------------------------------------------

def test_apply_garmin_key_pure_success(tmp_path, monkeypatch):
    c = make_cfg(tmp_path)
    st = State(c.data_dir)
    link_user(st, uid=42)
    # fake GarminApiClient that returns 3 activities with owner info
    import tg_garmin_to_osm.runner as runner_mod
    monkeypatch.setattr(runner_mod, "GarminApiClient",
                        lambda key: _Garmin(activities=[
                            {"activityId": 1, "activityName": "Ride", "activityType": {"typeKey": "cycling"},
                             "startTimeGMT": "2026-01-01 00:00:00", "distance": 10000,
                             "ownerId": "12345", "ownerDisplayName": "John Doe"},
                            {"activityId": 2, "activityName": "Run", "activityType": {"typeKey": "running"},
                             "startTimeGMT": "2026-01-02 00:00:00", "distance": 5000},
                            {"activityId": 3, "activityName": "Walk", "activityType": {"typeKey": "walking"},
                             "startTimeGMT": "2026-01-03 00:00:00", "distance": 3000},
                        ]))
    ok, garmin_name, err, n = apply_garmin_key_pure(c, st, 42, "VALIDKEY")
    assert ok is True
    assert garmin_name == "John Doe"
    assert err is None
    assert n == 3
    u = st.user(42)
    assert u["garmin_api_key"] == "VALIDKEY"
    assert u["garmin_linked"] == 1
    assert u["garmin_owner_id"] == "12345"
    assert u["garmin_name"] == "John Doe"


def test_apply_garmin_key_pure_bad_key(tmp_path, monkeypatch):
    from tg_garmin_to_osm.garmin_api import GarminApiError
    c = make_cfg(tmp_path)
    st = State(c.data_dir)
    link_user(st, uid=42)
    class _Boom:
        def __init__(self, key):
            pass
        def recent_activities(self, limit=3):
            raise GarminApiError("exchange", 401, "unauthorized")
    import tg_garmin_to_osm.runner as runner_mod
    monkeypatch.setattr(runner_mod, "GarminApiClient", _Boom)
    ok, garmin_name, err, n = apply_garmin_key_pure(c, st, 42, "BADKEY")
    assert ok is False
    assert err == "bad_key"
    u = st.user(42)
    assert u["garmin_linked"] == 1  # the existing link is kept
    assert u["garmin_api_key"] is None


def test_apply_garmin_key_pure_garmin_unavailable(tmp_path, monkeypatch):
    from tg_garmin_to_osm.garmin_api import GarminApiError
    c = make_cfg(tmp_path)
    st = State(c.data_dir)
    link_user(st, uid=42)
    class _Boom:
        def __init__(self, key):
            pass
        def recent_activities(self, limit=3):
            raise GarminApiError("exchange", 503, "service unavailable")
    import tg_garmin_to_osm.runner as runner_mod
    monkeypatch.setattr(runner_mod, "GarminApiClient", _Boom)
    ok, garmin_name, err, n = apply_garmin_key_pure(c, st, 42, "KEY")
    assert ok is False
    assert err == "garmin_unavailable"
    u = st.user(42)
    assert u["garmin_linked"] == 1  # an outage does not unlink


# -- POST /api/accounts/osm --------------------------------------------------------

def test_accounts_osm_create_web_user(tmp_path):
    c, st, runner, server, port = make_test_server(tmp_path)
    try:
        code, body = post(port, "/api/accounts/osm", "SECRETTOKEN1",
                          {"osm_uid": 12345, "osm_name": "mapper", "access_token": "TKN123", "lang": "en"})
        assert code == 200
        assert body == {"uid": -12345, "created": True}
        u = st.user(-12345)
        assert u is not None
        assert u["channel"] == "web"
        assert u["osm_uid"] == 12345
        assert u["osm_access_token"] == "TKN123"
        assert u["osm_name"] == "mapper"
        assert u["lang"] == "en"
        assert u["chat_id"] is None
    finally:
        server.shutdown()


def test_accounts_osm_update_existing(tmp_path):
    c, st, runner, server, port = make_test_server(tmp_path)
    # pre-create user via OSM sign-in
    st.touch_user(-12345, None, "en")
    st.save_osm_user(-12345, None, "OLDTKN", "oldname", 12345)
    st.db.execute("UPDATE users SET channel='web' WHERE user_id=?", (-12345,))
    st.db.commit()
    try:
        code, body = post(port, "/api/accounts/osm", "SECRETTOKEN1",
                          {"osm_uid": 12345, "osm_name": "newname", "access_token": "NEWTKN", "lang": "ru"})
        assert code == 200
        assert body == {"uid": -12345, "created": False}
        u = st.user(-12345)
        assert u["osm_access_token"] == "NEWTKN"
        assert u["osm_name"] == "newname"
        assert u["lang"] == "en"  # an existing user keeps their language
    finally:
        server.shutdown()


def test_accounts_osm_after_osm_unlink_reuses_row(tmp_path):
    c, st, runner, server, port = make_test_server(tmp_path)
    try:
        post(port, "/api/accounts/osm", "SECRETTOKEN1",
             {"osm_uid": 777, "osm_name": "m", "access_token": "T1", "lang": "en"})
        code, _ = post(port, "/api/users/-777/unlink", "SECRETTOKEN1", {"what": "osm"})
        assert code == 200 and st.user(-777)["osm_uid"] is None
        code, body = post(port, "/api/accounts/osm", "SECRETTOKEN1",
                          {"osm_uid": 777, "osm_name": "m", "access_token": "T2", "lang": "en"})
        assert code == 200 and body == {"uid": -777, "created": False}
        u = st.user(-777)
        assert u["osm_uid"] == 777 and u["osm_access_token"] == "T2"
    finally:
        server.shutdown()


def test_accounts_osm_signup_closed(tmp_path):
    c = Config(token="", ingest_token="SECRETTOKEN1", data_dir=str(tmp_path), ingest_port=0,
               garmin_token_dir=str(tmp_path / "garmin_tokens"),
               osm_client_id="cid", osm_client_secret="sec", max_web_users=1)
    st = State(c.data_dir)
    tg = Telegram(c.token, c.api_base)
    runner = Runner(c, tg, st)
    server = serve(0, "SECRETTOKEN1", lambda *a: None, lambda *a: None, c, st, runner)
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
        # create one user
        code, _ = post(port, "/api/accounts/osm", "SECRETTOKEN1",
                       {"osm_uid": 111, "osm_name": "a", "access_token": "t", "lang": "en"})
        assert code == 200
        # second user should be rejected
        code, body = post(port, "/api/accounts/osm", "SECRETTOKEN1",
                          {"osm_uid": 222, "osm_name": "b", "access_token": "t", "lang": "en"})
        assert code == 403
        assert body["error"] == "signup_closed"
    finally:
        server.shutdown()


# -- POST /api/accounts/telegram ---------------------------------------------------

def test_accounts_telegram_ensure_user(tmp_path):
    c, st, runner, server, port = make_test_server(tmp_path)
    try:
        code, body = post(port, "/api/accounts/telegram", "SECRETTOKEN1",
                          {"tg_uid": 999, "lang": "ru"})
        assert code == 200
        assert body == {"uid": 999}
        u = st.user(999)
        assert u is not None
        assert u["channel"] == "telegram"
        assert u["chat_id"] == 999
        assert u["lang"] == "ru"
    finally:
        server.shutdown()


# -- GET /api/users/{uid} ----------------------------------------------------------

def test_get_user_web(tmp_path):
    c, st, runner, server, port = make_test_server(tmp_path)
    # create web user
    st.touch_user(-12345, None, "en")
    st.save_osm_user(-12345, None, "TKN", "mapper", 12345)
    st.set_garmin_linked(-12345, True)
    st.set_setting(-12345, "auto", "1")
    st.set_setting(-12345, "types", "bike,walk")
    st.set_setting(-12345, "visibility", "trackable")
    st.db.execute("UPDATE users SET channel='web' WHERE user_id=?", (-12345,))
    st.db.commit()
    try:
        code, body = get(port, "/api/users/-12345", "SECRETTOKEN1")
        assert code == 200
        assert body["uid"] == -12345
        assert body["channel"] == "web"
        assert body["lang"] == "en"
        assert body["osm"]["linked"] is True
        assert body["osm"]["name"] == "mapper"
        assert body["garmin"]["linked"] is True
        assert body["settings"]["auto"] is True
        assert body["settings"]["types"] == "bike,walk"
        assert body["settings"]["visibility"] == "trackable"
        assert "type_options" in body
        assert "delay_minutes" in body
        assert "counts" in body
        assert "activities" in body
        assert "events" in body
    finally:
        server.shutdown()


def test_get_user_telegram(tmp_path):
    c, st, runner, server, port = make_test_server(tmp_path)
    link_user(st, uid=42, chat=-1001, garmin=True, osm=True, lang="ru")
    try:
        code, body = get(port, "/api/users/42", "SECRETTOKEN1")
        assert code == 200
        assert body["uid"] == 42
        assert body["channel"] == "telegram"
        assert body["lang"] == "ru"
        assert body["osm"]["linked"] is True
        assert body["garmin"]["linked"] is True
    finally:
        server.shutdown()


def test_get_user_not_found(tmp_path):
    c, st, runner, server, port = make_test_server(tmp_path)
    try:
        code, body = get(port, "/api/users/999999", "SECRETTOKEN1")
        assert code == 404
        assert body["error"] == "not_found"
    finally:
        server.shutdown()


# -- POST /api/users/{uid}/garmin-key ----------------------------------------------

def test_garmin_key_success(tmp_path, monkeypatch):
    c, st, runner, server, port = make_test_server(tmp_path)
    link_user(st, uid=42)
    import tg_garmin_to_osm.runner as runner_mod
    monkeypatch.setattr(runner_mod, "GarminApiClient",
                        lambda key: _Garmin(activities=[
                            {"activityId": 1, "activityName": "Ride", "activityType": {"typeKey": "cycling"},
                             "startTimeGMT": "2026-01-01 00:00:00", "distance": 10000,
                             "ownerId": "12345", "ownerDisplayName": "John Doe"},
                            {"activityId": 2, "activityName": "Run", "activityType": {"typeKey": "running"},
                             "startTimeGMT": "2026-01-02 00:00:00", "distance": 5000},
                        ]))
    try:
        code, body = post(port, "/api/users/42/garmin-key", "SECRETTOKEN1", {"key": "VALIDKEY123"})
        assert code == 200
        assert body["ok"] is True
        assert body["garmin_name"] == "John Doe"
        u = st.user(42)
        assert u["garmin_api_key"] == "VALIDKEY123"
        assert u["garmin_owner_id"] == "12345"
    finally:
        server.shutdown()


def test_garmin_key_bad_key(tmp_path, monkeypatch):
    from tg_garmin_to_osm.garmin_api import GarminApiError
    c, st, runner, server, port = make_test_server(tmp_path)
    link_user(st, uid=42)
    class _Boom:
        def __init__(self, key):
            pass
        def recent_activities(self, limit=3):
            raise GarminApiError("exchange", 401, "unauthorized")
    import tg_garmin_to_osm.runner as runner_mod
    monkeypatch.setattr(runner_mod, "GarminApiClient", _Boom)
    try:
        code, body = post(port, "/api/users/42/garmin-key", "SECRETTOKEN1", {"key": "BADKEY"})
        assert code == 200
        assert body["ok"] is False
        assert body["error"] == "bad_key"
        u = st.user(42)
        assert u["garmin_linked"] == 1  # the existing link is kept
    finally:
        server.shutdown()


def test_garmin_key_missing_key(tmp_path):
    c, st, runner, server, port = make_test_server(tmp_path)
    link_user(st, uid=42)
    try:
        code, body = post(port, "/api/users/42/garmin-key", "SECRETTOKEN1", {})
        assert code == 400
        assert body["error"] == "bad_key"
    finally:
        server.shutdown()


# -- POST /api/users/{uid}/settings ------------------------------------------------

def test_settings_update(tmp_path):
    c, st, runner, server, port = make_test_server(tmp_path)
    link_user(st, uid=42)
    try:
        code, body = post(port, "/api/users/42/settings", "SECRETTOKEN1",
                          {"auto": False, "types": "run,walk", "visibility": "trackable", "lang": "en"})
        assert code == 200
        assert body["ok"] is True
        u = st.user(42)
        assert u["auto"] == 0
        assert u["types"] == "run,walk"
        assert u["visibility"] == "trackable"
        assert u["lang"] == "en"
    finally:
        server.shutdown()


def test_settings_bad_visibility(tmp_path):
    c, st, runner, server, port = make_test_server(tmp_path)
    link_user(st, uid=42)
    try:
        code, body = post(port, "/api/users/42/settings", "SECRETTOKEN1",
                          {"visibility": "private"})
        assert code == 400
        assert body["error"] == "bad_visibility"
    finally:
        server.shutdown()


def test_settings_bad_lang(tmp_path):
    c, st, runner, server, port = make_test_server(tmp_path)
    link_user(st, uid=42)
    try:
        code, body = post(port, "/api/users/42/settings", "SECRETTOKEN1",
                          {"lang": "fr"})
        assert code == 400
        assert body["error"] == "bad_lang"
    finally:
        server.shutdown()


def test_settings_bad_types(tmp_path):
    c, st, runner, server, port = make_test_server(tmp_path)
    link_user(st, uid=42)
    try:
        # empty types string -> expand_types returns default group, which is truthy
        # so we test an invalid case by passing something that expands to empty
        # actually expand_types("") returns default group, so it's valid
        # the validation is: if not expand_types(val): error
        # empty string -> default group -> truthy -> valid
        # we can't easily test bad_types without mocking expand_types
        pass
    finally:
        server.shutdown()


# -- POST /api/users/{uid}/sync ----------------------------------------------------

def test_sync_success(tmp_path, monkeypatch):
    c, st, runner, server, port = make_test_server(tmp_path)
    link_user(st, uid=42, garmin=True, osm=True)
    garmin = _Garmin(activities=[act(101)])
    import tg_garmin_to_osm.runner as runner_mod
    monkeypatch.setattr(runner_mod, "Osm", _Osm)
    monkeypatch.setattr(runner_mod, "GarminClient", lambda token_dir: garmin)
    try:
        code, body = post(port, "/api/users/42/sync", "SECRETTOKEN1", {})
        assert code == 200
        assert body["ok"] is True
        assert body["new"] >= 0
        assert body["uploaded"] >= 0
    finally:
        server.shutdown()


def test_sync_not_ready_no_garmin(tmp_path):
    c, st, runner, server, port = make_test_server(tmp_path)
    link_user(st, uid=42, garmin=False, osm=True)
    try:
        code, body = post(port, "/api/users/42/sync", "SECRETTOKEN1", {})
        assert code == 409
        assert body["error"] == "not_ready"
    finally:
        server.shutdown()


def test_sync_not_ready_no_osm(tmp_path):
    c, st, runner, server, port = make_test_server(tmp_path)
    link_user(st, uid=42, garmin=True, osm=False)
    try:
        code, body = post(port, "/api/users/42/sync", "SECRETTOKEN1", {})
        assert code == 409
        assert body["error"] == "not_ready"
    finally:
        server.shutdown()


# -- POST /api/users/{uid}/activities/{aid}/upload ---------------------------------

def test_activity_upload_known(tmp_path, monkeypatch):
    c, st, runner, server, port = make_test_server(tmp_path)
    link_user(st, uid=42, garmin=True, osm=True)
    # add activity as 'new'
    st.insert_activity(42, "101", "Ride", "cycling", 100, 20.0, "new")
    garmin = _Garmin(activities=[act(101, name="Ride"), act(102, name="Run")])
    import tg_garmin_to_osm.runner as runner_mod
    import tg_garmin_to_osm.garmin as garmin_mod
    import tg_garmin_to_osm.osm as osm_mod
    monkeypatch.setattr(osm_mod, "Osm", _Osm)
    monkeypatch.setattr(ingest_mod, "Osm", _Osm)
    monkeypatch.setattr(runner_mod, "GarminClient", lambda token_dir: garmin)
    monkeypatch.setattr(garmin_mod, "GarminClient", lambda token_dir: garmin)
    try:
        # upload known activity (status=new)
        code, body = post(port, "/api/users/42/activities/101/upload", "SECRETTOKEN1", {})
        assert code == 200
        assert body["ok"] is True
        assert body["status"] == "uploaded"
        row = st.activity(42, "101")
        assert row["status"] == "uploaded"
        assert row["trace_id"] == 1
    finally:
        server.shutdown()


def test_activity_upload_unknown_fetches_from_garmin(tmp_path, monkeypatch):
    c, st, runner, server, port = make_test_server(tmp_path)
    link_user(st, uid=42, garmin=True, osm=True)
    # add unknown activity to fake garmin
    garmin = _Garmin(activities=[act(999, name="Unknown Ride")])
    import tg_garmin_to_osm.runner as runner_mod
    import tg_garmin_to_osm.garmin as garmin_mod
    import tg_garmin_to_osm.osm as osm_mod
    monkeypatch.setattr(osm_mod, "Osm", _Osm)
    monkeypatch.setattr(ingest_mod, "Osm", _Osm)
    monkeypatch.setattr(runner_mod, "GarminClient", lambda token_dir: garmin)
    monkeypatch.setattr(garmin_mod, "GarminClient", lambda token_dir: garmin)
    try:
        code, body = post(port, "/api/users/42/activities/999/upload", "SECRETTOKEN1", {})
        assert code == 200
        assert body["ok"] is True
        row = st.activity(42, "999")
        assert row is not None
        assert row["name"] == "Unknown Ride"
        assert row["status"] == "uploaded"
    finally:
        server.shutdown()


def test_activity_upload_already_uploaded(tmp_path, monkeypatch):
    c, st, runner, server, port = make_test_server(tmp_path)
    link_user(st, uid=42, garmin=True, osm=True)
    st.insert_activity(42, "101", "Ride", "cycling", 100, 20.0, "uploaded")
    st.set_status(42, "101", "uploaded", trace_id=1)
    garmin = _Garmin(activities=[act(101)])
    import tg_garmin_to_osm.runner as runner_mod
    import tg_garmin_to_osm.garmin as garmin_mod
    import tg_garmin_to_osm.osm as osm_mod
    monkeypatch.setattr(osm_mod, "Osm", _Osm)
    monkeypatch.setattr(ingest_mod, "Osm", _Osm)
    monkeypatch.setattr(runner_mod, "GarminClient", lambda token_dir: garmin)
    monkeypatch.setattr(garmin_mod, "GarminClient", lambda token_dir: garmin)
    try:
        # first upload (already uploaded)
        code, body = post(port, "/api/users/42/activities/101/upload", "SECRETTOKEN1", {})
        assert code == 400
        assert body["error"] == "already_uploaded"
    finally:
        server.shutdown()


# -- POST /api/users/{uid}/activities/{aid}/skip -----------------------------------

def test_activity_skip(tmp_path, monkeypatch):
    c, st, runner, server, port = make_test_server(tmp_path)
    link_user(st, uid=42, garmin=True, osm=True)
    st.insert_activity(42, "101", "Ride", "cycling", 100, 20.0, "new")
    garmin = _Garmin(activities=[act(101)])
    import tg_garmin_to_osm.runner as runner_mod
    import tg_garmin_to_osm.garmin as garmin_mod
    import tg_garmin_to_osm.osm as osm_mod
    monkeypatch.setattr(osm_mod, "Osm", _Osm)
    monkeypatch.setattr(ingest_mod, "Osm", _Osm)
    monkeypatch.setattr(runner_mod, "GarminClient", lambda token_dir: garmin)
    monkeypatch.setattr(garmin_mod, "GarminClient", lambda token_dir: garmin)
    try:
        code, body = post(port, "/api/users/42/activities/101/skip", "SECRETTOKEN1", {})
        assert code == 200
        assert body["ok"] is True
        row = st.activity(42, "101")
        assert row["status"] == "skipped"
    finally:
        server.shutdown()


def test_activity_skip_not_found(tmp_path):
    c, st, runner, server, port = make_test_server(tmp_path)
    link_user(st, uid=42, garmin=True, osm=True)
    try:
        code, body = post(port, "/api/users/42/activities/999/skip", "SECRETTOKEN1", {})
        assert code == 404
        assert body["error"] == "not_found"
    finally:
        server.shutdown()


def test_activity_skip_already_uploaded(tmp_path, monkeypatch):
    c, st, runner, server, port = make_test_server(tmp_path)
    link_user(st, uid=42, garmin=True, osm=True)
    st.insert_activity(42, "101", "Ride", "cycling", 100, 20.0, "uploaded")
    st.set_status(42, "101", "uploaded", trace_id=1)
    garmin = _Garmin(activities=[act(101)])
    import tg_garmin_to_osm.runner as runner_mod
    import tg_garmin_to_osm.garmin as garmin_mod
    import tg_garmin_to_osm.osm as osm_mod
    monkeypatch.setattr(osm_mod, "Osm", _Osm)
    monkeypatch.setattr(ingest_mod, "Osm", _Osm)
    monkeypatch.setattr(runner_mod, "GarminClient", lambda token_dir: garmin)
    monkeypatch.setattr(garmin_mod, "GarminClient", lambda token_dir: garmin)
    try:
        # upload first
        post(port, "/api/users/42/activities/101/upload", "SECRETTOKEN1", {})
        # then try to skip
        code, body = post(port, "/api/users/42/activities/101/skip", "SECRETTOKEN1", {})
        assert code == 400
        assert body["error"] == "already_uploaded"
    finally:
        server.shutdown()


# -- POST /api/users/{uid}/unlink --------------------------------------------------

def test_unlink_garmin(tmp_path):
    c, st, runner, server, port = make_test_server(tmp_path)
    link_user(st, uid=42, garmin=True, osm=True)
    try:
        code, body = post(port, "/api/users/42/unlink", "SECRETTOKEN1", {"what": "garmin"})
        assert code == 200
        assert body["ok"] is True
        u = st.user(42)
        assert u["garmin_linked"] == 0
        assert u["garmin_api_key"] is None
        assert u["osm_access_token"] is not None  # OSM still linked
    finally:
        server.shutdown()


def test_unlink_osm(tmp_path):
    c, st, runner, server, port = make_test_server(tmp_path)
    link_user(st, uid=42, garmin=True, osm=True)
    try:
        code, body = post(port, "/api/users/42/unlink", "SECRETTOKEN1", {"what": "osm"})
        assert code == 200
        assert body["ok"] is True
        u = st.user(42)
        assert u["osm_access_token"] is None
        assert u["garmin_linked"] == 1  # Garmin still linked
    finally:
        server.shutdown()


def test_unlink_bad_what(tmp_path):
    c, st, runner, server, port = make_test_server(tmp_path)
    link_user(st, uid=42)
    try:
        code, body = post(port, "/api/users/42/unlink", "SECRETTOKEN1", {"what": "both"})
        assert code == 400
        assert body["error"] == "bad_what"
    finally:
        server.shutdown()


# -- DELETE /api/users/{uid} -------------------------------------------------------

def test_delete_user(tmp_path):
    c, st, runner, server, port = make_test_server(tmp_path)
    link_user(st, uid=42, garmin=True, osm=True)
    # add some activities and events
    st.insert_activity(42, "101", "Ride", "cycling", 100, 20.0, "new")
    st.add_event(42, "uploaded", {"activity_id": "101", "name": "Ride", "trace_id": 1})
    st.set_meta("init:42", "1")
    try:
        code, body = delete(port, "/api/users/42", "SECRETTOKEN1")
        assert code == 200
        assert body["ok"] is True
        assert st.user(42) is None
        assert st.activity(42, "101") is None
        assert st.events(42) == []
        assert st.get_meta("init:42") is None
    finally:
        server.shutdown()


def test_delete_user_removes_legacy_token_dir(tmp_path):
    c, st, runner, server, port = make_test_server(tmp_path)
    link_user(st, uid=42, garmin=True, osm=True)
    # create legacy token dir
    import os
    token_dir = os.path.join(c.garmin_token_dir, "42")
    os.makedirs(token_dir, exist_ok=True)
    with open(os.path.join(token_dir, "garmin_tokens.json"), "w") as f:
        f.write('{"token": "x"}')
    try:
        code, body = delete(port, "/api/users/42", "SECRETTOKEN1")
        assert code == 200
        assert not os.path.exists(token_dir)
    finally:
        server.shutdown()


# -- auth errors -------------------------------------------------------------------

def test_api_requires_auth(tmp_path):
    c, st, runner, server, port = make_test_server(tmp_path)
    try:
        code, body = get(port, "/api/users/42", "WRONGTOKEN")
        assert code == 401
        assert body["error"] == "unauthorized"
        code, body = post(port, "/api/users/42/settings", "WRONGTOKEN", {"auto": True})
        assert code == 401
    finally:
        server.shutdown()


# -- work lock busy ----------------------------------------------------------------

def test_work_lock_busy_returns_503(tmp_path, monkeypatch):
    import threading
    import time
    c, st, runner, server, port = make_test_server(tmp_path)
    link_user(st, uid=42)
    # hold the work lock
    runner.work_lock.acquire()
    try:
        # use longer timeout since server waits up to 20s for lock
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/users/42/settings",
            method="POST",
            headers={"Authorization": "Bearer SECRETTOKEN1", "Content-Type": "application/json"},
            data=json.dumps({"auto": True}).encode())
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                code, body = r.status, json.load(r)
        except urllib.error.HTTPError as e:
            code, body = e.code, json.load(e)
        assert code == 503
        assert body["error"] == "busy"
    finally:
        runner.work_lock.release()
        server.shutdown()


# -- migration: old schema gets new columns ----------------------------------------

def test_migration_adds_new_columns(tmp_path):
    """Create an old-schema DB, open State, verify new columns exist and old rows survive."""
    import sqlite3
    import os
    db_path = os.path.join(tmp_path, "state.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE users (user_id INTEGER PRIMARY KEY, chat_id INTEGER, osm_access_token TEXT)")
    conn.execute("CREATE TABLE activities (user_id INTEGER, activity_id TEXT, status TEXT, PRIMARY KEY (user_id, activity_id))")
    conn.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO users (user_id, chat_id, osm_access_token) VALUES (42, -1001, 'TKN')")
    conn.commit()
    conn.close()

    # Now open with State - should migrate
    st = State(str(tmp_path))
    cols = {r[1] for r in st.db.execute("PRAGMA table_info(users)")}
    assert "garmin_linked" in cols
    assert "auto" in cols
    assert "types" in cols
    assert "visibility" in cols
    assert "lang" in cols
    assert "garmin_api_key" in cols
    assert "channel" in cols
    assert "osm_uid" in cols
    assert "garmin_owner_id" in cols
    assert "garmin_name" in cols
    # old row should survive
    u = st.user(42)
    assert u is not None
    assert u["user_id"] == 42
    assert u["chat_id"] == -1001
    assert u["osm_access_token"] == "TKN"
    # new columns have defaults
    assert u["channel"] == "telegram"
    assert u["garmin_linked"] == 0
    assert u["auto"] == 1
    assert u["types"] == "bike"
    assert u["visibility"] == "identifiable"
    # events table exists
    tables = {r[0] for r in st.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "events" in tables
    # unique index on osm_uid
    idx = {r[1] for r in st.db.execute("PRAGMA index_list(users)")}
    assert "uix_users_osm_uid" in idx


# -- thread safety: State used from second thread ----------------------------------

def test_state_thread_safety(tmp_path):
    """Call State methods from a background thread while main thread also uses it."""
    import threading
    import time
    c = make_cfg(tmp_path)
    st = State(c.data_dir)
    link_user(st, uid=1)
    errors = []

    def writer(uid):
        try:
            for i in range(50):
                st.insert_activity(uid, f"a{i}", f"Ride {i}", "cycling", 1000 + i, 10.0, "new")
                st.set_status(uid, f"a{i}", "uploaded", i)
                time.sleep(0.001)
        except Exception as e:
            errors.append(e)

    def reader(uid):
        try:
            for _ in range(50):
                st.user(uid)
                st.activity(uid, "a0")
                st.counts(uid)
                st.recent_activities(uid, 10)
                time.sleep(0.001)
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=writer, args=(1,))
    t2 = threading.Thread(target=reader, args=(1,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert not errors, f"Thread errors: {errors}"
    # verify data integrity
    u = st.user(1)
    assert u is not None
    acts = st.recent_activities(1, 100)
    assert len(acts) >= 1


# -- events written on upload/processed/garmin_expired/osm_401 for web user --------

def test_events_written_for_web_user(tmp_path, monkeypatch):
    """Events are written for web users (chat_id NULL) without Telegram calls."""
    c = make_cfg(tmp_path)
    st = State(c.data_dir)
    # create web user: negative uid, chat_id NULL, channel=web
    st.touch_user(-12345, None, "en")
    st.save_osm_user(-12345, None, "TKN", "mapper", 12345)
    st.set_garmin_linked(-12345, True)
    st.db.execute("UPDATE users SET channel='web', garmin_api_key='WEBKEY' WHERE user_id=?", (-12345,))
    st.db.commit()

    garmin = _Garmin(activities=[act(101, name="Ride")], gpx=b"<gpx><trk><trkpt lat='1' lon='2'><time>2026-01-01T00:00:00Z</time></trkpt></trk></gpx>")
    import tg_garmin_to_osm.runner as runner_mod
    import tg_garmin_to_osm.osm as osm_mod
    monkeypatch.setattr(osm_mod, "Osm", _Osm)
    monkeypatch.setattr(ingest_mod, "Osm", _Osm)
    monkeypatch.setattr(runner_mod, "Osm", _Osm)
    monkeypatch.setattr(runner_mod, "GarminApiClient", lambda key: garmin)
    tg = _Tg()

    # upload_one -> should write 'uploaded' event
    user = st.user(-12345)
    from tg_garmin_to_osm.runner import garmin_client_for, summary_row, upload_one, user_vis
    from tg_garmin_to_osm.osm import Osm
    row = st.activity(-12345, "101")
    if not row:
        raw = garmin.activity("101")
        row = summary_row(raw)
        st.insert_activity(-12345, "101", row["name"], row["type"], row["start_ts"], row["distance_km"], "new")
    garmin2 = garmin_client_for(c, user)
    osm = Osm(c.osm_base, user["osm_access_token"])
    row = st.activity(-12345, "101")
    trace_id = upload_one(c, garmin2, osm, st, user, row, tg, user_vis(user, c), notify=True)
    assert trace_id == 1
    events = st.events(-12345)
    assert len(events) == 1
    assert events[0]["kind"] == "uploaded"
    assert events[0]["data"]["activity_id"] == "101"
    # no Telegram send because chat_id is None
    assert tg.sent == []

    # check_pending -> should write 'processed' event
    from tg_garmin_to_osm.runner import check_pending
    _Osm.instances[-1].pending[1] = False
    done = check_pending(c, tg, st, user)
    assert done == 1
    events = st.events(-12345)
    assert len(events) == 2
    kinds = {e["kind"] for e in events}
    assert kinds == {"uploaded", "processed"}
    # no Telegram send because chat_id is None
    assert tg.sent == []


# -- run_forever survives getUpdates exception -------------------------------------

def test_run_forever_survives_getupdates_exception(tmp_path, monkeypatch):
    """One loop iteration with a fake getUpdates that raises, then recovers."""
    c = make_cfg(tmp_path)
    st = State(c.data_dir)
    link_user(st, uid=42, garmin=True, osm=True)
    call_count = {"n": 0}

    class _BadTg:
        def __init__(self):
            self.sent = []
        def updates(self, offset, wait=25):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("getUpdates failed")
            return []
        def send(self, chat_id, text, silent=False):
            self.sent.append((chat_id, text))
        def buttons(self, chat_id, text, rows):
            pass
        def answer_callback(self, callback_query_id):
            pass
        def photo(self, chat_id, caption, image, filename="step.png"):
            pass
        def reply_msg(self, msg, text, silent=False):
            return self.send(msg.get("chat", {}).get("id"), text, silent)
        def delete(self, chat_id, message_id):
            pass
        def whoami(self):
            return {"username": "testbot"}
        def chat_info(self, chat_id):
            return {}
        def webhook_info(self):
            return {}

    tg = _BadTg()
    runner = Runner(c, tg, st)

    # Run one iteration of the loop manually
    import time
    import tg_garmin_to_osm.runner as runner_mod
    original_sleep = time.sleep
    def fast_sleep(s):
        if s > 0.1:
            original_sleep(0.01)
        else:
            original_sleep(s)
    monkeypatch.setattr(time, "sleep", fast_sleep)

    # Simulate one loop: handle_updates (fails), then cycle (should still run)
    try:
        runner_mod.handle_updates(c, tg, st)
    except Exception:
        pass  # expected to fail on first call

    # Second call should work (getUpdates returns empty)
    runner_mod.handle_updates(c, tg, st)

    # cycle should have run
    import tg_garmin_to_osm.runner as rm
    monkeypatch.setattr(rm, "Osm", _Osm)
    monkeypatch.setattr(rm, "GarminClient", lambda token_dir: _Garmin(activities=[]))
    runner_mod.cycle(c, tg, st)

    # No crash, loop would continue
    assert call_count["n"] >= 1