import calendar
import time

import tg_garmin_to_osm.runner as runner_mod
from tg_garmin_to_osm.config import expand_types
from tg_garmin_to_osm.garmin import GarminClient  # noqa: F401  (import guards module wiring)
from tg_garmin_to_osm.osm import OsmError
from tg_garmin_to_osm.runner import (
    PENDING, activity_start_ts, check_pending, cycle, expand_types_user, handle_updates, is_ride,
    scan_activities, type_key, user_cycle,
)
from tg_garmin_to_osm.state import State

from conftest_helpers import (
    VALID_GPX, _Garmin, _Osm, _Tg, act, cfg, link_user, msg, seeded, upload_all,
)


# -- scan ----------------------------------------------------------------------------------------------------------

def test_first_scan_marks_backlog(tmp_path):
    c = cfg(tmp_path)
    st = State(c.data_dir)
    u = link_user(st)
    garmin = _Garmin(activities=[act(1), act(2)])
    assert scan_activities(c, garmin, st, u) == 2
    assert st.counts(42) == {"backlog": 2}
    assert st.get_meta("init:42") == "1"
    assert scan_activities(c, garmin, st, u) == 0  # nothing new, nothing re-marked


def test_scan_after_init_marks_new_and_ignored(tmp_path):
    c = cfg(tmp_path)
    st = State(c.data_dir)
    u = link_user(st)
    garmin = _Garmin()
    scan_activities(c, garmin, st, u)  # init with an empty page
    garmin.activities = [act(1, type_key="cycling"), act(2, type_key="running")]
    assert scan_activities(c, garmin, st, u) == 2
    assert st.activity(42, "1")["status"] == "new"
    assert st.activity(42, "2")["status"] == "ignored"
    assert st.activity(42, "1")["start_ts"] == calendar.timegm(
        time.strptime("2026-11-07 06:12:34", "%Y-%m-%d %H:%M:%S"))


def test_scan_respects_user_types(tmp_path):
    c = cfg(tmp_path)
    st = State(c.data_dir)
    u = link_user(st, types="bike,walk")  # subscribe to biking AND walking
    garmin = _Garmin()
    scan_activities(c, garmin, st, u)
    garmin.activities = [act(1, type_key="walking"), act(2, type_key="running"), act(3, type_key="hiking")]
    scan_activities(c, garmin, st, u)
    assert st.activity(42, "1")["status"] == "new"
    assert st.activity(42, "2")["status"] == "ignored"
    assert st.activity(42, "3")["status"] == "new"


def test_scan_knows_existing_rows(tmp_path):
    c = cfg(tmp_path)
    st = State(c.data_dir)
    u = link_user(st)
    garmin = _Garmin(activities=[act(1)])
    scan_activities(c, garmin, st, u)  # backlog
    st.set_status(42, "1", "skipped")
    assert scan_activities(c, garmin, st, u) == 0  # known row: never re-marked 'new'
    assert st.activity(42, "1")["status"] == "skipped"


def test_is_ride_and_expand_types():
    types = expand_types("bike")
    assert "cycling" in types and "gravel_cycling" in types
    assert "running" not in types
    mixed = expand_types("bike,swimming")
    assert "swimming" in mixed and "cycling" in mixed  # group + raw key
    assert is_ride(types, act(type_key="Cycling"))  # case-insensitive
    assert not is_ride(types, act(type_key="running"))
    assert type_key(act()) == "cycling"
    assert type_key({"activityType": "swimming"}) == "swimming"


def test_expand_types_user_fallback_default(tmp_path):
    c = cfg(tmp_path, default_types="bike")
    assert expand_types_user({"types": ""}, c) == expand_types("bike")
    assert "walking" in expand_types_user({"types": "bike,walk"}, c)


def test_activity_start_ts():
    assert activity_start_ts(act()) == calendar.timegm(
        time.strptime("2026-11-07 06:12:34", "%Y-%m-%d %H:%M:%S"))
    assert activity_start_ts({"startTimeGMT": "garbage"}) == 0
    assert activity_start_ts({}) == 0


# -- upload --------------------------------------------------------------------------------------------------------

def test_upload_ready_happy_path(tmp_path, monkeypatch):
    c, st, garmin, tg, u = seeded(tmp_path, monkeypatch, [act()])
    assert upload_all(c, garmin, tg, st, u) == 1
    row = st.activity(42, "101")
    assert row["status"] == "uploaded" and row["trace_id"] == 1 and row["uploaded_ts"]
    fake = _Osm.instances[-1]
    assert fake.uploaded and fake.uploaded[0]["tags"] == "bike"
    assert fake.uploaded[0]["visibility"] == "identifiable"
    assert fake.uploaded[0]["token"] == "TKN-42"  # the user's own OSM token
    assert fake.uploaded[0]["description"] == "Утро (2026-11-07, 25 km)"
    chat, text = tg.sent[0]
    assert chat == -1001 and "загружен в OSM" in text and "user/osmuser/traces/1" in text


def test_upload_notification_english_user(tmp_path, monkeypatch):
    c, st, garmin, tg, u = seeded(tmp_path, monkeypatch, [act()], lang="en")
    assert upload_all(c, garmin, tg, st, u) == 1
    text = tg.sent[0][1]
    assert "Trace uploaded to OSM" in text and "Trace 1" in text
    assert "Трейк" not in text


def test_users_are_isolated(tmp_path, monkeypatch):
    c = cfg(tmp_path)
    st = State(c.data_dir)
    _Osm.instances, _Osm.pending, _Osm.next_id = [], {}, 0  # isolate from earlier tests
    ua = link_user(st, uid=1, name="alice")
    ub = link_user(st, uid=2, name="bob")
    garmin_a = _Garmin()
    scan_activities(c, garmin_a, st, ua)
    garmin_a.activities = [act(500)]
    scan_activities(c, garmin_a, st, ua)
    # bob scans his own account: the same activity id in his stream must not collide
    garmin_b = _Garmin()
    scan_activities(c, garmin_b, st, ub)  # init user B with an empty first page
    garmin_b.activities = [act(500, name="Bob ride")]
    scan_activities(c, garmin_b, st, ub)
    assert st.activity(1, "500")["status"] == "new" and st.activity(1, "500")["name"] == "Утро"
    assert st.activity(2, "500")["name"] == "Bob ride"
    monkeypatch.setattr(runner_mod, "Osm", _Osm)
    tg = _Tg()
    assert upload_all(c, garmin_a, tg, st, ua) == 1
    assert upload_all(c, _Garmin(activities=[act(500)]), tg, st, ub) == 1
    assert {inst.uploaded[0]["token"] for inst in _Osm.instances} == {"TKN-1", "TKN-2"}


def test_upload_ready_respects_delay(tmp_path, monkeypatch):
    c, st, garmin, tg, u = seeded(tmp_path, monkeypatch, [act()], upload_delay_minutes=60)
    assert upload_all(c, garmin, tg, st, u) == 0
    assert garmin.downloads == [] and st.activity(42, "101")["status"] == "new"


def test_upload_bad_gpx_does_not_burn_attempt(tmp_path, monkeypatch):
    bad = b"<gpx><trk><trkpt lat='1' lon='2'/></trk></gpx>"  # no timestamps
    c, st, garmin, tg, u = seeded(tmp_path, monkeypatch, [act()], gpx=bad)
    assert upload_all(c, garmin, tg, st, u) == 0
    row = st.activity(42, "101")
    assert row["status"] == "new" and row["attempts"] == 0
    assert tg.sent == []  # garmin/file problems are quiet, retried on the next scan


def test_upload_permanent_osm_error_fails_after_three(tmp_path, monkeypatch):
    c, st, garmin, tg, u = seeded(tmp_path, monkeypatch, [act()])
    fake = _Osm("", "")
    fake.upload_error = OsmError("upload_gpx", 400, "bad file")
    monkeypatch.setattr(runner_mod, "Osm", lambda base, token: fake)
    for _ in range(3):
        upload_all(c, garmin, tg, st, u)
    row = st.activity(42, "101")
    assert row["status"] == "failed" and row["attempts"] == 3
    assert any("Не смог загрузить" in t for _, t in tg.sent)


def test_upload_transient_osm_error_keeps_new(tmp_path, monkeypatch):
    c, st, garmin, tg, u = seeded(tmp_path, monkeypatch, [act()])
    fake = _Osm("", "")
    fake.upload_error = OsmError("upload_gpx", 503, "down")
    monkeypatch.setattr(runner_mod, "Osm", lambda base, token: fake)
    assert upload_all(c, garmin, tg, st, u) == 0
    row = st.activity(42, "101")
    assert row["status"] == "new" and row["attempts"] == 0


def test_upload_401_asks_for_relogin(tmp_path, monkeypatch):
    c, st, garmin, tg, u = seeded(tmp_path, monkeypatch, [act()])
    fake = _Osm("", "")
    fake.upload_error = OsmError("upload_gpx", 401, "revoked")
    monkeypatch.setattr(runner_mod, "Osm", lambda base, token: fake)
    upload_all(c, garmin, tg, st, u)
    assert any("/login" in t for _, t in tg.sent)
    assert st.activity(42, "101")["status"] == "new"  # one attempt burned, retryable


def test_garmin_auth_error_resets_client_and_notifies_once_per_day(tmp_path, monkeypatch):
    c, st, garmin, tg, u = seeded(tmp_path, monkeypatch, [act()])
    garmin.fail_ids = {"101"}
    upload_all(c, garmin, tg, st, u)
    assert garmin.resets == 1
    assert st.activity(42, "101")["status"] == "new"
    assert sum(1 for _, t in tg.sent if "expired" in t or "истекла" in t) == 1
    upload_all(c, garmin, tg, st, u)  # same day: throttled
    assert sum(1 for _, t in tg.sent if "expired" in t or "истекла" in t) == 1


def test_auto_off_user_is_not_served_by_cycle(tmp_path, monkeypatch):
    c = cfg(tmp_path)
    st = State(c.data_dir)
    link_user(st, uid=1, auto="0")
    link_user(st, uid=2)
    dirs = []

    class _Spy(_Garmin):
        def __init__(self, token_dir=""):
            _Garmin.__init__(self, token_dir=token_dir)

        def recent_activities(self, limit=10):
            dirs.append(self.token_dir)
            return []

    monkeypatch.setattr(runner_mod, "GarminClient", _Spy)
    monkeypatch.setattr(runner_mod, "Osm", _Osm)
    cycle(c, _Tg(), st)
    assert len(dirs) == 1 and "2" in dirs[0]  # only user 2 (auto on) was scanned


def test_cycle_scopes_each_user_token_dir(tmp_path, monkeypatch):
    c = cfg(tmp_path)
    st = State(c.data_dir)
    link_user(st, uid=1)
    link_user(st, uid=2)
    dirs = []

    class _Spy(_Garmin):
        def __init__(self, token_dir=""):
            _Garmin.__init__(self, token_dir=token_dir)

        def recent_activities(self, limit=10):
            dirs.append(self.token_dir)
            return []

    monkeypatch.setattr(runner_mod, "GarminClient", _Spy)
    monkeypatch.setattr(runner_mod, "Osm", _Osm)
    cycle(c, _Tg(), st)
    assert len(dirs) == 2 and all(str(uid) in d for uid, d in zip((1, 2), dirs))


# -- trace processing ----------------------------------------------------------------------------------------------

def test_check_pending_flow(tmp_path, monkeypatch):
    c, st, garmin, tg, u = seeded(tmp_path, monkeypatch, [act()])
    assert upload_all(c, garmin, tg, st, u) == 1
    fake = _Osm.instances[-1]
    assert fake.pending[1] is True
    assert check_pending(c, tg, st, u) == 0  # still pending
    assert st.activity(42, "101")["status"] == "uploaded"
    fake.pending[1] = False
    assert check_pending(c, tg, st, u) == 1
    assert st.activity(42, "101")["status"] == "done"
    chat, text = tg.sent[-1]
    assert chat == -1001 and "gpx=1" in text and "map=17/55.75000/37.61000" in text


def test_check_pending_404_is_silence(tmp_path, monkeypatch):
    c, st, garmin, tg, u = seeded(tmp_path, monkeypatch, [act()])
    upload_all(c, garmin, tg, st, u)
    fake = _Osm.instances[-1]
    fake.pending[1] = "404"  # OSM has not made the trace visible yet
    assert check_pending(c, tg, st, u) == 0
    assert st.activity(42, "101")["status"] == "uploaded"


# -- commands / onboarding ------------------------------------------------------------------------------------------

def test_start_gives_onboarding(tmp_path):
    c = cfg(tmp_path)
    st = State(c.data_dir)
    tg = _Tg()
    assert runner_mod._handle_message(c, tg, st, msg("/start")) is True
    text = tg.sent[0][1]
    assert "/link" in text and "iD" in text  # the RU onboarding points at /link, not a bare URL
    assert st.user(42)["chat_id"] == -1001  # the contact row appeared


def test_commands_only_in_private_chats(tmp_path):
    c = cfg(tmp_path)
    st = State(c.data_dir)
    tg = _Tg()
    assert runner_mod._handle_message(c, tg, st, msg("/help", chat_type="group")) is False
    assert tg.sent == []
    assert runner_mod._handle_message(c, tg, st, msg("/help", chat=-9999)) is True  # any private chat is fine


def test_bare_text_is_ignored_without_pending(tmp_path):
    c = cfg(tmp_path)
    st = State(c.data_dir)
    tg = _Tg()
    assert runner_mod._handle_message(c, tg, st, msg("random words")) is False
    assert tg.sent == []


def test_osm_login_flow(tmp_path, monkeypatch):
    from tg_garmin_to_osm import osm as osmmod

    def fake_exchange(base, cid, sec, code):
        if code != "CODE":
            raise OsmError("token exchange failed", 400, "bad code")
        return {"access_token": "TKN"}

    c = cfg(tmp_path)
    st = State(c.data_dir)
    tg = _Tg()
    monkeypatch.setattr(osmmod, "exchange_code", fake_exchange)
    monkeypatch.setattr(runner_mod, "Osm", _Osm)
    assert runner_mod._handle_message(c, tg, st, msg("/login")) is True
    assert any("oauth2/authorize" in t and "client_id=cid" in t for _, t in tg.sent)
    assert runner_mod._handle_message(c, tg, st, msg("CODE")) is True
    assert st.user(42)["osm_access_token"] == "TKN"
    assert any("OSM связан" in t for _, t in tg.sent)






def test_sync_command_needs_both_links(tmp_path, monkeypatch):
    c, st, garmin, tg, u = seeded(tmp_path, monkeypatch, [act()])
    st.unlink_osm(42)
    runner_mod._handle_command(c, tg, st, msg("/sync"), "/sync", "")
    assert any("/login" in t for _, t in tg.sent)
    st.save_osm_user(42, -1001, "TKN-42", "osmuser")
    st.unlink_garmin(42)
    runner_mod._handle_command(c, tg, st, msg("/sync"), "/sync", "")
    assert any("/garmin" in t for _, t in tg.sent)


def test_sync_command_runs_user_cycle(tmp_path, monkeypatch):
    c, st, garmin, tg, u = seeded(tmp_path, monkeypatch, [act()])
    monkeypatch.setattr(runner_mod, "Osm", _Osm)
    monkeypatch.setattr(runner_mod, "GarminClient", lambda token_dir: garmin)
    runner_mod._handle_command(c, tg, st, msg("/sync"), "/sync", "")
    assert st.activity(42, "101")["status"] == "uploaded"
    assert any("Готово" in t for _, t in tg.sent)


def test_skip_command(tmp_path, monkeypatch):
    c, st, garmin, tg, u = seeded(tmp_path, monkeypatch, [act()])
    assert runner_mod._handle_message(c, tg, st, msg("/skip 101")) is True
    assert st.activity(42, "101")["status"] == "skipped"
    runner_mod._handle_message(c, tg, st, msg("/skip 999"))
    assert any("не найдена" in t for _, t in tg.sent)
    assert upload_all(c, garmin, tg, st, u) == 0  # a skipped ride is never uploaded


def test_visibility_auto_types_lang_commands(tmp_path):
    c = cfg(tmp_path)
    st = State(c.data_dir)
    tg = _Tg()
    runner_mod._handle_message(c, tg, st, msg("/visibility trackable"))
    assert st.user(42)["visibility"] == "trackable"
    runner_mod._handle_message(c, tg, st, msg("/auto off"))
    assert st.user(42)["auto"] == 0
    runner_mod._handle_message(c, tg, st, msg("/types bike,walk"))
    assert st.user(42)["types"] == "bike,walk"
    runner_mod._handle_message(c, tg, st, msg("/lang en"))
    assert st.user(42)["lang"] == "en"
    runner_mod._handle_message(c, tg, st, msg("/status", lang="en"))
    text = tg.sent[-1][1]
    assert "Auto-upload: off" in text and "bike,walk" in text and "trackable" in text


def test_unlink_command(tmp_path):
    c = cfg(tmp_path)
    st = State(c.data_dir)
    link_user(st)
    tg = _Tg()
    runner_mod._handle_message(c, tg, st, msg("/unlink osm"))
    assert st.user(42)["osm_access_token"] is None
    runner_mod._handle_message(c, tg, st, msg("/unlink garmin"))
    assert st.user(42)["garmin_linked"] == 0
    runner_mod._handle_message(c, tg, st, msg("/unlink"))
    assert any("/unlink garmin" in t for _, t in tg.sent)


def test_upload_command_known_and_unknown(tmp_path, monkeypatch):
    c, st, garmin, tg, u = seeded(tmp_path, monkeypatch,
                                  [act(), act(9, name="Прогулка", type_key="running")])
    monkeypatch.setattr(runner_mod, "GarminClient", lambda token_dir: garmin)
    st.set_status(42, "101", "skipped")
    assert runner_mod._handle_message(c, tg, st, msg("/upload 101")) is True
    assert st.activity(42, "101")["status"] == "uploaded" and st.activity(42, "101")["trace_id"]
    assert runner_mod._handle_message(c, tg, st, msg("/upload 9")) is True  # fetched from Garmin live
    assert st.activity(42, "9")["status"] == "uploaded"


# -- course ----------------------------------------------------------------------------------------------------------

def test_course_from_own_garmin_course(tmp_path, monkeypatch):
    detail = {"metadataDTO": {"associatedCourseId": 777}}
    c, st, garmin, tg, u = seeded(tmp_path, monkeypatch, [act()],
                                  detail=detail, courses={777: "Lefkara Classic"})
    assert upload_all(c, garmin, tg, st, u) == 1
    fake = _Osm.instances[-1]
    assert 'по курсу «Lefkara Classic»' in fake.uploaded[0]["description"]


def test_foreign_course_id_is_dropped(tmp_path, monkeypatch):
    detail = {"metadataDTO": {"associatedCourseId": 999}}  # not among the user's courses
    c, st, garmin, tg, u = seeded(tmp_path, monkeypatch, [act()],
                                  detail=detail, courses={777: "Lefkara Classic"})
    assert upload_all(c, garmin, tg, st, u) == 1
    fake = _Osm.instances[-1]
    assert fake.uploaded[0]["description"] == "Утро (2026-11-07, 25 km)"


# -- updates plumbing -----------------------------------------------------------------------------------------------

def test_handle_updates_offset_and_dispatch(tmp_path):
    c = cfg(tmp_path)
    st = State(c.data_dir)
    tg = _Tg()
    tg.updates_result = [{"update_id": 10, "message": msg("/help")}, {"update_id": 11, "message": {}}]
    assert runner_mod.handle_updates(c, tg, st) == 1
    assert st.get_meta("updates_offset") == "12"
    assert any("/sync" in t for _, t in tg.sent)


def test_handle_updates_bad_message_does_not_stall(tmp_path, monkeypatch):
    c = cfg(tmp_path)
    st = State(c.data_dir)
    link_user(st)  # a linked user: /upload of an unknown id reaches the Garmin lookup
    tg = _Tg()
    tg.updates_result = [{"update_id": 1, "message": msg("/upload abc")}]  # garmin lookup raises

    class _Boom(_Garmin):
        def __init__(self, token_dir=""):
            _Garmin.__init__(self, token_dir=token_dir)

        def activity(self, aid):
            raise RuntimeError("garmin exploded")

    monkeypatch.setattr(runner_mod, "GarminClient", _Boom)
    assert runner_mod.handle_updates(c, tg, st) == 0  # not counted as handled...
    assert st.get_meta("updates_offset") == "2"      # ...but the offset advanced, the loop is not stalled
    assert any("Ошибка" in t for _, t in tg.sent)


def test_user_cycle_first_scan_marks_backlog(tmp_path, monkeypatch):
    c = cfg(tmp_path)
    st = State(c.data_dir)
    u = link_user(st)
    garmin = _Garmin(activities=[act(1)])
    monkeypatch.setattr(runner_mod, "GarminClient", lambda token_dir: garmin)
    monkeypatch.setattr(runner_mod, "Osm", _Osm)
    res = user_cycle(c, _Tg(), st, u)
    assert res["seen"] == 1


# -- /link and ingest ------------------------------------------------------------------------------------------------

def test_link_command_sends_site(tmp_path):
    c = cfg(tmp_path)
    st = State(c.data_dir)
    tg = _Tg()
    assert runner_mod._handle_message(c, tg, st, msg("/link")) is True
    chat, text = tg.sent[0]
    assert "https://garmin-osm.exiosv.com" in text and "Telegram" in text


def test_login_fallback_flow_still_works(tmp_path, monkeypatch):
    from tg_garmin_to_osm import osm as osmmod

    monkeypatch.setattr(osmmod, "exchange_code",
                        lambda base, cid, sec, code: {"access_token": "TKN"} if code == "CODE"
                        else (_ for _ in ()).throw(OsmError("token exchange failed", 400, "bad")))
    monkeypatch.setattr(runner_mod, "Osm", _Osm)
    c = cfg(tmp_path)
    st = State(c.data_dir)
    tg = _Tg()
    assert runner_mod._handle_message(c, tg, st, msg("/login")) is True
    assert runner_mod._handle_message(c, tg, st, msg("CODE")) is True
    assert st.user(42)["osm_access_token"] == "TKN"
    assert runner_mod._handle_message(c, tg, st, msg("/cancel")) is True  # no dialog: friendly text


def test_ingest_garmin_link_writes_tokens_and_notifies(tmp_path):
    c = cfg(tmp_path)
    st = State(c.data_dir)
    link_user(st)
    tg = _Tg()
    tokens = '{"access": {"token": "x"}}'
    runner_mod.ingest_garmin_link(c, tg, st, 42, tokens)
    token_file = pathlib_path(c.garmin_token_dir) / "42" / "garmin_tokens.json"
    assert token_file.read_text() == tokens
    assert st.user(42)["garmin_linked"] == 1
    assert any("Garmin связан" in t for _, t in tg.sent)


def test_ingest_osm_link(tmp_path):
    c = cfg(tmp_path)
    st = State(c.data_dir)
    link_user(st)
    tg = _Tg()
    runner_mod.ingest_osm_link(c, tg, st, 42, "OSMTKN", "mapper")
    assert st.user(42)["osm_access_token"] == "OSMTKN" and st.user(42)["osm_name"] == "mapper"
    assert any("OSM связан" in t and "mapper" in t for _, t in tg.sent)


def pathlib_path(base):
    import pathlib
    return pathlib.Path(base).expanduser()


def test_handle_updates_resyncs_stale_offset(tmp_path):
    """A token swap on an existing DB: the stored offset belongs to the OLD bot; the server ignores
    it and re-delivers low ids. The bot must resync instead of looping on the same update."""
    c = cfg(tmp_path)
    st = State(c.data_dir)
    st.set_meta("updates_offset", "457905209")  # stale, from the previous bot
    tg = _Tg()
    tg.updates_result = [{"update_id": 242062024, "message": msg("/start", chat=1, user=1)}]
    class _Spy(_Garmin):
        pass
    assert handle_updates(c, tg, st) == 1
    assert st.get_meta("updates_offset") == "242062025"  # resynced from the server's true queue
    assert tg.updates_calls[-1] == 242062025  # the next poll starts after the delivered update
