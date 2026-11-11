"""Regression tests for the review of the web account API (items 1-13)."""
import json
import threading
import time
import urllib.request
import zlib

import tg_garmin_to_osm.ingest as ingest_mod
import tg_garmin_to_osm.runner as runner_mod
from tg_garmin_to_osm.ingest import serve
from tg_garmin_to_osm.osm import OsmError
from tg_garmin_to_osm.runner import WORK_LOCK, Runner
from tg_garmin_to_osm.state import State

from conftest_helpers import _Garmin, _Osm, _Tg, act, cfg, link_user, msg

TOKEN = "SECRETTOKEN1"


def crc(token):
    return zlib.crc32(token.encode()) or 1


def call(port, method, path, body=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", method=method,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def start(tmp_path, tg=None, **kw):
    c = cfg(tmp_path, ingest_token=TOKEN, garmin_token_dir=str(tmp_path / "gt"), **kw)
    st = State(c.data_dir)
    runner = Runner(c, tg, st)
    server = serve(0, TOKEN, None, None, c, st, runner)
    port = server.server_address[1]
    for _ in range(100):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=1):
                break
        except Exception:
            time.sleep(0.02)
    return c, st, runner, server, port


def patch_clients(monkeypatch, garmin):
    monkeypatch.setattr(runner_mod, "Osm", _Osm)
    monkeypatch.setattr(ingest_mod, "Osm", _Osm)
    monkeypatch.setattr(runner_mod, "GarminClient", lambda token_dir: garmin)
    monkeypatch.setattr(runner_mod, "GarminApiClient", lambda key: garmin)
    monkeypatch.setattr(runner_mod.time, "sleep", lambda s: None)


# -- 1. osm_uid is stored and kept ---------------------------------------------------------------------------------

def test_save_osm_user_keeps_osm_uid_when_none(tmp_path):
    st = State(str(tmp_path))
    st.save_osm_user(42, -1001, "T1", "a", 777)
    st.save_osm_user(42, -1001, "T2", "b")  # a bot flow without the id
    assert st.user(42)["osm_uid"] == 777 and st.user(42)["osm_access_token"] == "T2"


def test_bot_login_stores_osm_uid(tmp_path, monkeypatch):
    from tg_garmin_to_osm import osm as osmmod
    monkeypatch.setattr(osmmod, "exchange_code", lambda *a: {"access_token": "TKNX"})
    monkeypatch.setattr(runner_mod, "Osm", _Osm)
    c = cfg(tmp_path)
    st = State(c.data_dir)
    tg = _Tg()
    runner_mod._handle_message(c, tg, st, msg("/login"))
    runner_mod._handle_message(c, tg, st, msg("CODE"))
    assert st.user(42)["osm_uid"] == crc("TKNX")


def test_ingest_osm_stores_osm_uid_and_refuses_taken(tmp_path):
    c, st, runner, server, port = start(tmp_path)
    try:
        assert call(port, "POST", "/ingest/osm", {"uid": 42, "access_token": "A", "osm_uid": 5})[0] == 200
        assert st.user(42)["osm_uid"] == 5
        code, body = call(port, "POST", "/ingest/osm", {"uid": 43, "access_token": "B", "osm_uid": 5})
        assert (code, body) == (409, {"error": "osm_taken"})
    finally:
        server.shutdown()


def test_cycle_backfills_osm_uid_once_and_skips_errors(tmp_path, monkeypatch):
    c = cfg(tmp_path)
    st = State(c.data_dir)
    link_user(st, uid=1, garmin=False)               # token TKN-1
    link_user(st, uid=2, garmin=False)               # token TKN-2, OSM fails for it
    st.save_osm_user(3, 3, "TKN-1", "dup")           # same OSM account as user 1
    st.touch_user(4, 4)                              # no token: not touched

    class _Flaky(_Osm):
        def user_details(self):
            if self.token == "TKN-2":
                raise OsmError("GET /api/0.6/user/details.json failed", 401, "")
            return _Osm.user_details(self)

    monkeypatch.setattr(runner_mod, "Osm", _Flaky)
    runner_mod.cycle(c, None, st)
    assert st.user(1)["osm_uid"] == crc("TKN-1")
    assert st.user(2)["osm_uid"] is None             # skipped quietly, retried next cycle
    assert st.user(3)["osm_uid"] is None             # the id is taken by user 1
    assert st.user(4)["osm_uid"] is None


# -- 2. chat_id is never wiped -------------------------------------------------------------------------------------

def test_save_osm_user_keeps_chat_id_when_none(tmp_path):
    st = State(str(tmp_path))
    st.touch_user(42, -1001)
    st.save_osm_user(42, None, "T", "n", 9)
    assert st.user(42)["chat_id"] == -1001


def test_accounts_osm_existing_telegram_user_keeps_chat_id(tmp_path):
    c, st, runner, server, port = start(tmp_path)
    st.touch_user(42, 42, "ru")
    st.save_osm_user(42, 42, "OLD", "mapper", 9)
    try:
        code, body = call(port, "POST", "/api/accounts/osm",
                          {"osm_uid": 9, "osm_name": "mapper", "access_token": "NEW", "lang": "en"})
        assert (code, body) == (200, {"uid": 42, "created": False})
        u = st.user(42)
        assert u["chat_id"] == 42 and u["osm_access_token"] == "NEW"
        assert u["lang"] == "ru"  # item 11: an existing user keeps their language
        assert u["channel"] == "telegram"
    finally:
        server.shutdown()


# -- 3. Telegram errors do not stop the cycle ----------------------------------------------------------------------

def test_step_runs_cycle_when_get_updates_always_raises(tmp_path, monkeypatch):
    class _DeadTg(_Tg):
        def updates(self, offset, wait=25):
            raise RuntimeError("getUpdates failed: bot123:SECRET")

    ran = []
    monkeypatch.setattr(runner_mod, "cycle", lambda c, tg, st: ran.append("cycle"))
    monkeypatch.setattr(runner_mod.time, "sleep", lambda s: None)
    c = cfg(tmp_path, garmin_poll_seconds=600)
    st = State(c.data_dir)
    r = Runner(c, _DeadTg(), st)
    r.step()
    assert ran == ["cycle"] and r.last_scan > 0
    r.last_scan = time.time() - 601  # the poll interval elapsed
    r.step()
    assert ran == ["cycle", "cycle"]


# -- 4. one work lock for Telegram, the cycle and the API ----------------------------------------------------------

def _lock_free_elsewhere() -> bool:
    out = []

    def probe():
        got = WORK_LOCK.acquire(timeout=0)
        if got:
            WORK_LOCK.release()
        out.append(got)

    t = threading.Thread(target=probe)
    t.start()
    t.join()
    return out[0]


def test_handle_updates_holds_lock_per_update_not_during_poll(tmp_path, monkeypatch):
    seen = {}

    class _ProbeTg(_Tg):
        def updates(self, offset, wait=25):
            seen["poll"] = _lock_free_elsewhere()
            return [{"update_id": 1, "message": msg("/help")}]

    def fake_handle(cfg_, tg, st, m):
        seen["handle"] = _lock_free_elsewhere()
        return True

    monkeypatch.setattr(runner_mod, "_handle_message", fake_handle)
    c = cfg(tmp_path)
    runner_mod.handle_updates(c, _ProbeTg(), State(c.data_dir))
    assert seen == {"poll": True, "handle": False}


def test_runner_and_api_share_the_module_lock(tmp_path):
    c = cfg(tmp_path)
    assert Runner(c, None, State(c.data_dir)).work_lock is WORK_LOCK
    assert ingest_mod.WORK_LOCK is WORK_LOCK


def test_busy_lock_keeps_timers(tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(runner_mod, "cycle", lambda c, tg, st: ran.append(1))
    c = cfg(tmp_path)
    r = Runner(c, None, State(c.data_dir))
    r.lock_timeout = 0.05
    held, release = threading.Event(), threading.Event()

    def holder():
        with WORK_LOCK:
            held.set()
            release.wait(5)

    t = threading.Thread(target=holder)
    t.start()
    held.wait(5)
    try:
        r.step()
        assert ran == [] and r.last_scan == 0 and r.last_check == 0  # retried on the next pass
    finally:
        release.set()
        t.join()
    r.step()
    assert ran == [1] and r.last_scan > 0


# -- 5. Telegram key wrapper: one Garmin probe, one message --------------------------------------------------------

def test_apply_garmin_key_wrapper_probes_once(tmp_path, monkeypatch):
    made = []

    def client(key):
        made.append(key)
        return _Garmin(activities=[act(1), act(2)])

    monkeypatch.setattr(runner_mod, "GarminApiClient", client)
    c = cfg(tmp_path)
    st = State(c.data_dir)
    link_user(st)
    tg = _Tg()
    runner_mod.apply_garmin_key(c, tg, st, 42, "KEY123456789")
    assert made == ["KEY123456789"]
    assert len(tg.sent) == 1 and "Personal API" in tg.sent[0][1] and "2" in tg.sent[0][1]
    assert [e["kind"] for e in st.events(42)] == ["garmin_linked"]


def test_notify_without_tg_or_text_writes_event_only(tmp_path):
    c = cfg(tmp_path)
    st = State(c.data_dir)
    u = link_user(st)
    tg = _Tg()
    runner_mod._notify(c, None, st, u, "garmin_expired", {}, "garmin_expired")
    runner_mod._notify(c, tg, st, u, "garmin_linked", {"name": "x"})
    assert tg.sent == [] and len(st.events(42)) == 2


# -- 6. web actions of a Telegram user still reach Telegram --------------------------------------------------------

def test_api_upload_notifies_telegram_user(tmp_path, monkeypatch):
    garmin = _Garmin(activities=[act(101)])
    patch_clients(monkeypatch, garmin)
    tg = _Tg()
    c, st, runner, server, port = start(tmp_path, tg=tg)
    link_user(st)
    st.insert_activity(42, "101", "Утро", "cycling", 100, 25.0, "new")
    try:
        code, body = call(port, "POST", "/api/users/42/activities/101/upload", {})
        assert (code, body) == (200, {"ok": True, "status": "uploaded"})
        assert any("загружен в OSM" in t for _, t in tg.sent)
    finally:
        server.shutdown()


def test_api_sync_uses_runner_tg(tmp_path, monkeypatch):
    garmin = _Garmin()
    patch_clients(monkeypatch, garmin)
    tg = _Tg()
    c, st, runner, server, port = start(tmp_path, tg=tg)
    link_user(st)
    st.set_meta("init:42", "1")
    garmin.activities = [act(101)]
    try:
        code, body = call(port, "POST", "/api/users/42/sync", {})
        assert (code, body) == (200, {"ok": True, "new": 1, "uploaded": 1})
        assert any("загружен в OSM" in t for _, t in tg.sent)
    finally:
        server.shutdown()


# -- 7. activity rows in the contract shape ------------------------------------------------------------------------

ACTIVITY_KEYS = {"activity_id", "name", "type", "start_ts", "distance_km", "status", "trace_id",
                 "trace_url", "edit_url", "last_error", "actions"}


def test_get_user_activity_shape_and_links(tmp_path):
    c, st, runner, server, port = start(tmp_path)
    link_user(st, name="mapper")
    st.insert_activity(42, "1", "A", "cycling", 300, 10.0, "new")
    st.insert_activity(42, "2", "B", "cycling", 200, 10.0, "uploaded")
    st.set_status(42, "2", "uploaded", 7)
    st.insert_activity(42, "3", "C", "cycling", 100, 10.0, "uploaded")
    st.set_status(42, "3", "done", 8)
    st.insert_activity(42, "4", "D", "cycling", 50, 10.0, "uploaded")
    st.set_status(42, "4", "done", 9)
    # edit links are deterministic from the trace id now; the processed event no longer carries one

    try:
        code, body = call(port, "GET", "/api/users/42")
        assert code == 200
        rows = {a["activity_id"]: a for a in body["activities"]}
        assert all(set(a) == ACTIVITY_KEYS for a in body["activities"])
        assert rows["1"]["trace_url"] is None and rows["1"]["edit_url"] is None
        assert rows["2"]["trace_url"] == "https://www.openstreetmap.org/user/mapper/traces/7"
        assert rows["2"]["edit_url"] is None  # only 'done' rows get an edit link
        assert rows["3"]["edit_url"] == "https://www.openstreetmap.org/edit?editor=id&gpx=8"
        assert rows["4"]["edit_url"] == "https://www.openstreetmap.org/edit?editor=id&gpx=9"
    finally:
        server.shutdown()


# -- 8. garmin.failing ---------------------------------------------------------------------------------------------

def test_garmin_failing_set_and_cleared(tmp_path, monkeypatch):
    garmin = _Garmin()
    patch_clients(monkeypatch, garmin)
    c, st, runner, server, port = start(tmp_path)
    u = link_user(st)
    try:
        assert call(port, "GET", "/api/users/42")[1]["garmin"]["failing"] is False
        st.set_meta("garmin_fail:42", "2026-11-09")
        assert call(port, "GET", "/api/users/42")[1]["garmin"]["failing"] is True
        runner_mod.user_cycle(c, None, st, u)  # a scan that works clears it
        assert call(port, "GET", "/api/users/42")[1]["garmin"]["failing"] is False
        st.set_meta("garmin_fail:42", "2026-11-09")
        code, body = call(port, "POST", "/api/users/42/garmin-key", {"key": "NEWKEY123456"})
        assert body["ok"] is True
        assert call(port, "GET", "/api/users/42")[1]["garmin"]["failing"] is False
    finally:
        server.shutdown()


# -- 9. actions per status -----------------------------------------------------------------------------------------

def test_actions_per_status(tmp_path):
    c, st, runner, server, port = start(tmp_path)
    link_user(st)
    for i, status in enumerate(("new", "backlog", "ignored", "skipped", "failed", "uploaded", "done")):
        st.insert_activity(42, status, status, "cycling", 1000 - i, 1.0, status)
    try:
        rows = {a["activity_id"]: a["actions"] for a in call(port, "GET", "/api/users/42")[1]["activities"]}
        assert rows == {"new": ["upload", "skip"], "backlog": ["upload"], "ignored": ["upload"],
                        "skipped": ["upload"], "failed": ["upload", "skip"], "uploaded": [], "done": []}
    finally:
        server.shutdown()


# -- 10. channel through State -------------------------------------------------------------------------------------

def test_set_channel_and_no_raw_db_in_ingest(tmp_path):
    import inspect
    st = State(str(tmp_path))
    st.touch_user(-5, None)
    st.set_channel(-5, "web")
    assert st.user(-5)["channel"] == "web" and st.count_web_users() == 1
    assert "state.db" not in inspect.getsource(ingest_mod)


# -- 13. /link points at the Telegram sign-in page -----------------------------------------------------------------

def test_link_points_at_telegram_page(tmp_path):
    import tg_garmin_to_osm.onboarding as onb
    c = cfg(tmp_path)
    st = State(c.data_dir)
    tg = _Tg()
    runner_mod._handle_message(c, tg, st, msg("/link", lang="ru"))
    assert "https://garmin-osm.exiosv.com/ru/telegram" in tg.sent[-1][1]
    runner_mod._handle_message(c, tg, st, msg("/link", user=43, lang="en"))
    assert "https://garmin-osm.exiosv.com/telegram" in tg.sent[-1][1]
    assert "/ru/telegram" not in tg.sent[-1][1]
    runner_mod._handle_callback(c, tg, st, {"id": "q", "data": onb.CB_OSM_SITE,
                                            "from": {"id": 42, "language_code": "ru"},
                                            "message": {"chat": {"id": -1001, "type": "private"}}})
    assert "https://garmin-osm.exiosv.com/ru/telegram" in tg.sent[-1][1]


# -- round 3 -------------------------------------------------------------------------------------------------------

def test_non_ascii_authorization_header_is_401(tmp_path):
    import http.client
    c, st, runner, server, port = start(tmp_path)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.putrequest("GET", "/api/users/42")
        conn.putheader("Authorization", "Bearer ключ".encode("utf-8"))
        conn.endheaders()
        resp = conn.getresponse()
        assert resp.status == 401 and json.load(resp) == {"error": "unauthorized"}
        conn.close()
        assert urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=5).status == 200
    finally:
        server.shutdown()


class _RejectingApi:
    def __init__(self, key):
        self.key = key

    def recent_activities(self, limit=3):
        from tg_garmin_to_osm.garmin_api import GarminApiError
        raise GarminApiError("api key exchange failed", 401, "")


def test_bad_new_key_keeps_working_key(tmp_path, monkeypatch):
    c = cfg(tmp_path)
    st = State(c.data_dir)
    link_user(st, garmin=False)
    st.set_garmin_key(42, "GOODKEY12345")
    st.set_garmin_owner(42, "7", "Rider")
    monkeypatch.setattr(runner_mod, "GarminApiClient", _RejectingApi)
    ok, name, err, n = runner_mod.apply_garmin_key_pure(c, st, 42, "TYPOKEY12345")
    assert (ok, name, err, n) == (False, None, "bad_key", 0)
    u = st.user(42)
    assert u["garmin_api_key"] == "GOODKEY12345" and u["garmin_linked"] == 1
    assert u["garmin_name"] == "Rider"


def test_bad_key_without_link_stays_unlinked(tmp_path, monkeypatch):
    c = cfg(tmp_path)
    st = State(c.data_dir)
    link_user(st, garmin=False)
    monkeypatch.setattr(runner_mod, "GarminApiClient", _RejectingApi)
    ok, _, err, _ = runner_mod.apply_garmin_key_pure(c, st, 42, "TYPOKEY12345")
    assert (ok, err) == (False, "bad_key")
    u = st.user(42)
    assert u["garmin_linked"] == 0 and u["garmin_api_key"] is None


def test_settings_bad_field_changes_nothing(tmp_path):
    c, st, runner, server, port = start(tmp_path)
    link_user(st, auto="1", types="bike")
    try:
        code, body = call(port, "POST", "/api/users/42/settings", {"auto": False, "types": ""})
        assert (code, body) == (400, {"error": "bad_types"})
        code, body = call(port, "POST", "/api/users/42/settings", {"auto": False, "lang": "fr"})
        assert (code, body) == (400, {"error": "bad_lang"})
        u = st.user(42)
        assert u["auto"] == 1 and u["types"] == "bike" and u["lang"] == "ru"
    finally:
        server.shutdown()


def test_unlink_garmin_resets_first_scan_and_failing(tmp_path):
    c = cfg(tmp_path)
    st = State(c.data_dir)
    u = link_user(st)
    garmin = _Garmin()
    runner_mod.scan_activities(c, garmin, st, u)          # init done
    st.set_meta("garmin_fail:42", "2026-11-09")
    st.set_meta("garmin_fail:420", "keep")                # another user's key is not touched
    st.unlink_garmin(42)
    assert st.get_meta("init:42") is None and st.get_meta("garmin_fail:42") is None
    assert st.get_meta("garmin_fail:420") == "keep"
    st.set_garmin_linked(42, True)                        # a new Garmin account
    garmin.activities = [act(1), act(2)]
    runner_mod.scan_activities(c, garmin, st, st.user(42))
    assert st.counts(42) == {"backlog": 2}                # old history is backlog, not uploaded
