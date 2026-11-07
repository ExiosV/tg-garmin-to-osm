from tg_garmin_to_osm.state import State

from conftest_helpers import link_user


def make_state(tmp_path):
    return State(str(tmp_path / "data"))


def test_bootstrap_creates_tables(tmp_path):
    st = make_state(tmp_path)
    names = {r["name"] for r in st.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"users", "activities", "meta"} <= names


def test_meta_and_initialized(tmp_path):
    st = make_state(tmp_path)
    assert st.get_meta("visibility") is None
    st.set_meta("visibility", "trackable")
    assert st.get_meta("visibility") == "trackable"
    assert not st.initialized()
    st.set_initialized()
    assert st.initialized()


def test_user_row_and_settings(tmp_path):
    st = make_state(tmp_path)
    assert st.user(42) is None
    u = st.touch_user(42, -1001, "ru")
    assert u["chat_id"] == -1001 and u["lang"] == "ru"
    assert u["garmin_linked"] == 0 and u["auto"] == 1 and u["types"] == "bike"  # sane defaults
    st.touch_user(42, -1001, "en")  # the language is only remembered while empty
    assert st.user(42)["lang"] == "ru"
    st.set_setting(42, "lang", "en")
    st.set_setting(42, "types", "bike,walk")
    st.set_setting(42, "visibility", "trackable")
    st.set_setting(42, "auto", "0")
    u = st.user(42)
    assert (u["lang"], u["types"], u["visibility"], u["auto"]) == ("en", "bike,walk", "trackable", 0)


def test_settings_create_row_for_unknown_user(tmp_path):
    st = make_state(tmp_path)
    st.set_setting(7, "auto", "0")
    assert st.user(7)["auto"] == 0  # INTEGER column: SQLite stores the truthy flag as int


def test_osm_link_roundtrip(tmp_path):
    st = make_state(tmp_path)
    assert st.osm_users() == []
    st.save_osm_user(42, -1001, "TKN", "mapper")
    st.set_setting(42, "types", "bike,walk")
    st.save_osm_user(42, -1001, "TKN2", "mapper")  # re-login keeps the settings
    u = st.user(42)
    assert u["osm_access_token"] == "TKN2" and u["types"] == "bike,walk"
    st.unlink_osm(42)
    assert st.user(42)["osm_access_token"] is None and st.osm_users() == []


def test_garmin_link_flags(tmp_path):
    st = make_state(tmp_path)
    st.touch_user(42, -1001)
    assert st.garmin_users() == []
    st.set_garmin_linked(42, True)
    assert st.garmin_users()[0]["user_id"] == 42
    st.unlink_garmin(42)
    assert st.user(42)["garmin_linked"] == 0


def test_counts_users(tmp_path):
    st = make_state(tmp_path)
    link_user(st, uid=1)                       # full
    st.touch_user(2, -1002)                    # just said hi
    st.save_osm_user(3, -1003, "T3", "c")      # osm only
    st.set_garmin_linked(4, True)              # garmin only
    assert st.counts_users() == {"users": 4, "garmin": 2, "osm": 2}
    assert [u["user_id"] for u in st.garmin_osm_users()] == [1]


def test_insert_activity_dedupes_per_user(tmp_path):
    st = make_state(tmp_path)
    assert st.insert_activity(1, "101", "Утро", "cycling", 100, 25.0, "new")
    assert not st.insert_activity(1, "101", "Утро", "cycling", 100, 25.0, "ignored")
    assert st.insert_activity(2, "101", "Bob", "cycling", 100, 25.0, "new")  # same id, other user
    assert st.activity(1, "101")["status"] == "new"
    assert st.activity(2, "101")["name"] == "Bob"


def test_status_transitions(tmp_path):
    st = make_state(tmp_path)
    st.insert_activity(1, "101", "Утро", "cycling", 100, 25.0, "new")
    st.set_status(1, "101", "uploaded", 777)
    row = st.activity(1, "101")
    assert row["status"] == "uploaded" and row["trace_id"] == 777 and row["uploaded_ts"]
    st.set_status(1, "101", "done")
    assert st.activity(1, "101")["status"] == "done"


def test_record_failure_counts_attempts(tmp_path):
    st = make_state(tmp_path)
    st.insert_activity(1, "101", "Утро", "cycling", 100, 25.0, "new")
    assert st.record_failure(1, "101", "boom 1", max_attempts=3) == "new"
    assert st.record_failure(1, "101", "boom 2", max_attempts=3) == "new"
    assert st.record_failure(1, "101", "boom 3", max_attempts=3) == "failed"
    row = st.activity(1, "101")
    assert row["attempts"] == 3 and row["last_error"] == "boom 3"


def test_ready_to_upload_respects_delay(tmp_path):
    st = make_state(tmp_path)
    st.insert_activity(1, "old", "Old", "cycling", 50, 10.0, "new")
    st.insert_activity(1, "fresh", "Fresh", "cycling", 60, 10.0, "new")
    now = 20000
    st.db.execute("UPDATE activities SET created_ts=? WHERE activity_id='old'", (now - 3600,))
    st.db.execute("UPDATE activities SET created_ts=? WHERE activity_id='fresh'", (now - 2000,))
    st.db.commit()
    assert [r["activity_id"] for r in st.ready_to_upload(1, 30, now=now)] == ["old", "fresh"]
    assert st.ready_to_upload(1, 30, now=now - 3700) == []
    assert st.insert_activity(2, "x", "X", "cycling", 50, 10.0, "new")
    st.db.execute("UPDATE activities SET created_ts=? WHERE activity_id='x'", (now - 100,))
    st.db.commit()
    assert st.ready_to_upload(2, 0, now=now) != []  # other users' rows do not leak


def test_pending_failed_lists_are_scoped(tmp_path):
    st = make_state(tmp_path)
    st.insert_activity(1, "a", "A", "cycling", 1, 1.0, "new")
    st.insert_activity(2, "b", "B", "cycling", 2, 1.0, "new")
    st.set_status(1, "a", "uploaded", 5)
    st.record_failure(2, "b", "boom", 3)
    st.db.execute("UPDATE activities SET status='failed' WHERE user_id=2 AND activity_id='b'")
    st.db.commit()
    assert [r["activity_id"] for r in st.pending_traces(1)] == ["a"]
    assert [r["activity_id"] for r in st.failed_activities(2)] == ["b"]
    assert st.pending_traces(2) == [] and st.failed_activities(1) == []


def test_counts_scoped(tmp_path):
    st = make_state(tmp_path)
    st.insert_activity(1, "a", "A", "cycling", 1, 1.0, "new")
    st.insert_activity(2, "b", "B", "running", 2, 1.0, "ignored")
    assert st.counts(1) == {"new": 1}
    assert st.counts(2) == {"ignored": 1}
