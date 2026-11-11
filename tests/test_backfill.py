import datetime

import tg_garmin_to_osm.backfill as bf
from tg_garmin_to_osm.state import State

from conftest_helpers import VALID_GPX, _Garmin, _Osm, _Tg, cfg, link_user


def gact(aid, name, type_key, start):
    return {"activityId": aid, "activityName": name, "activityType": {"typeKey": type_key},
            "startTimeGMT": start, "distance": 20000}


def seeded_history(tmp_path, monkeypatch, history, existing_traces=()):
    c = cfg(tmp_path)
    st = State(c.data_dir)
    u = link_user(st, uid=42, lang="en")
    garmin = _Garmin(gpx=VALID_GPX)
    garmin.history = list(history)
    garmin.activities_page = lambda offset, limit=100: garmin.history[offset:offset + limit]
    tg = _Tg()
    _Osm.instances, _Osm.pending, _Osm.next_id = [], {}, 0

    class _OsmList(_Osm):
        traces = list(existing_traces)

        def user_traces(self):
            return list(self.traces)

    monkeypatch.setattr(bf, "Osm", lambda base, token: _OsmList(base, token))
    # backfill uses garmin_client_for which dispatches to GarminClient or GarminApiClient
    import tg_garmin_to_osm.runner as runner_mod
    monkeypatch.setattr(runner_mod, "GarminClient", lambda token_dir: garmin)
    monkeypatch.setattr(runner_mod, "GarminApiClient", lambda key: garmin)
    monkeypatch.setattr(bf.time, "sleep", lambda s: None)
    return c, st, garmin, tg, u


def test_scan_history_pages_and_stops_at_since(tmp_path, monkeypatch):
    history = [
        gact(1, "Ride Nov", "gravel_cycling", "2026-11-01 06:00:00"),
        gact(2, "Run", "running", "2026-10-20 07:00:00"),
        gact(3, "Ride Oct", "cycling", "2026-09-10 06:00:00"),
        gact(4, "Old Ride", "cycling", "2026-08-01 06:00:00"),  # before the window
    ]
    c, st, garmin, tg, u = seeded_history(tmp_path, monkeypatch, history)
    added, flipped = bf.scan_history(c, garmin, st, u, "2026-08-22")
    assert added == 3 and flipped == 0
    assert st.activity(42, "1")["status"] == "new"
    assert st.activity(42, "2")["status"] == "ignored"
    assert st.activity(42, "3")["status"] == "new"
    assert st.activity(42, "4") is None  # out of the window: never recorded


def test_scan_history_respects_user_types(tmp_path, monkeypatch):
    history = [gact(1, "Walk", "walking", "2026-09-01 06:00:00")]
    c, st, garmin, tg, u = seeded_history(tmp_path, monkeypatch, history)
    st.set_setting(42, "types", "bike,walk")
    added, _ = bf.scan_history(c, garmin, st, st.user(42), "2026-08-22")
    assert added == 1 and st.activity(42, "1")["status"] == "new"  # walking is in the user's groups


def test_scan_history_flips_backlog_rides(tmp_path, monkeypatch):
    history = [gact(1, "Ride", "cycling", "2026-09-01 06:00:00")]
    c, st, garmin, tg, u = seeded_history(tmp_path, monkeypatch, history)
    st.insert_activity(42, "1", "Ride", "cycling", 100, 20.0, "backlog")  # known from the first scan
    added, flipped = bf.scan_history(c, garmin, st, u, "2026-08-22")
    assert added == 0 and flipped == 1
    assert st.activity(42, "1")["status"] == "new"


def test_backfill_skips_dates_already_on_osm(tmp_path, monkeypatch):
    history = [
        gact(1, "Uploaded manually", "cycling", "2026-09-15 06:00:00"),
        gact(2, "Gap ride", "cycling", "2026-09-20 06:00:00"),
    ]
    traces = [{"id": "10", "timestamp": "2026-09-16T12:00:00Z", "name": "x"}]  # +1 day of ride 1
    c, st, garmin, tg, u = seeded_history(tmp_path, monkeypatch, history, existing_traces=traces)
    bf.run_backfill_user(c, tg, st, u, "2026-08-22", dry_run=True)
    assert st.activity(42, "1")["status"] == "new"  # dry run changes nothing
    bf.run_backfill_user(c, tg, st, u, "2026-08-22", dry_run=False)
    assert st.activity(42, "1")["status"] == "skipped"
    assert st.activity(42, "2")["status"] == "uploaded" and st.activity(42, "2")["trace_id"] == 1
    assert any("uploaded 1" in t and "(skipped) 1" in t for _, t in tg.sent)


def test_backfill_without_links(tmp_path, monkeypatch):
    c = cfg(tmp_path)
    st = State(c.data_dir)
    u = link_user(st, uid=7, garmin=False, osm=False)
    bf.run_backfill_user(c, _Tg(), st, u, "2026-08-22")
    assert st.counts(7) == {}


def test_run_backfill_selects_users(tmp_path, monkeypatch):
    c = cfg(tmp_path)
    st = State(c.data_dir)
    link_user(st, uid=1)
    link_user(st, uid=2, auto="0")  # auto off: the bulk run skips it
    ran = []
    monkeypatch.setattr(bf, "run_backfill_user",
                        lambda cfg_, tg, st_, u, since, dry: ran.append(u["user_id"]))
    bf.run_backfill(c, _Tg(), st, "2026-08-22")
    assert ran == [1]
    bf.run_backfill(c, _Tg(), st, "2026-08-22", user_id=2)
    assert ran == [1, 2]  # explicit --user works even with auto off


def test_trace_date_parsing_tolerates_garbage():
    assert bf._trace_dates([{"timestamp": "2026-09-16T12:00:00Z"},
                            {"timestamp": ""}, {"timestamp": None}]) == {datetime.date(2026, 9, 16)}


def test_scan_history_flips_ignored_after_types_widened(tmp_path, monkeypatch):
    """User ran the backfill with bike-only, then added walk to /types and presses
    'upload everything' again: their walks must join the upload queue."""
    history = [gact(1, "Walk", "walking", "2026-09-02 07:00:00"),
               gact(2, "Ride", "cycling", "2026-09-03 06:00:00")]
    c, st, garmin, tg, u = seeded_history(tmp_path, monkeypatch, history)
    st.set_setting(42, "types", "bike")  # first run: bike only
    added, _ = bf.scan_history(c, garmin, st, st.user(42), "2026-08-22")
    assert st.activity(42, "1")["status"] == "ignored"
    st.set_setting(42, "types", "bike,walk")  # the user widens their types
    added, flipped = bf.scan_history(c, garmin, st, st.user(42), "2026-08-22")
    assert added == 0 and flipped == 1
    assert st.activity(42, "1")["status"] == "new"  # the walk joins the next upload pass
    assert st.activity(42, "2")["status"] == "new"


def test_cancel_parks_remaining_and_thread_exits(tmp_path, monkeypatch):
    """Cancel: the thread stops uploading and parks the rest back to backlog."""
    import threading as _threading
    history = [gact(i, f"Ride {i}", "cycling", f"2026-09-{i:02d} 06:00:00") for i in range(1, 6)]
    c, st, garmin, tg, u = seeded_history(tmp_path, monkeypatch, history)
    monkeypatch.setattr(bf, "garmin_client_for", lambda cfg_, user: garmin)  # the thread must use the fake
    bf.scan_history(c, garmin, st, u, since="2026-08-22")
    rows = st.rides_by_status(42, "new")
    assert len(rows) == 5

    uploads = []

    def fake_upload_one(cfg_, garmin_, osm_, state_, user_, row_, tg_, vis_, notify=True):
        uploads.append(row_["activity_id"])
        bf.cancel_backfill(state_, user_["user_id"])  # cancel after the first upload
        state_.set_status(user_["user_id"], row_["activity_id"], "uploaded", 900 + len(uploads))
        return 900 + len(uploads)

    monkeypatch.setattr(bf, "upload_one", fake_upload_one)

    done = _threading.Event()

    def fake_target():
        bf._thread_target(cfg=c, tg=tg, state=st, user=u)

    # run the real thread body with the cancel-after-first-upload fake
    th = _threading.Thread(target=fake_target)
    th.start()
    th.join(timeout=15)
    assert not th.is_alive()
    assert uploads == ["1"]  # only one upload happened before the cancel
    rest = [st.activity(42, str(i))["status"] for i in range(2, 6)]
    assert rest == ["backlog", "backlog", "backlog", "backlog"]  # parked back
    assert st.activity(42, "1")["status"] == "uploaded"
    assert st.get_meta("backfill_running:42") is None
    assert any("остановлен" in t or "stopped" in t for _, t in tg.sent)


def test_fresh_start_clears_cancel_flag(tmp_path, monkeypatch):
    c, st, garmin, tg, u = seeded_history(tmp_path, monkeypatch, [])
    st.set_meta("backfill_cancel:42", "1")
    from tg_garmin_to_osm.backfill import start_user_backfill
    import tg_garmin_to_osm.backfill as bf
    bf._thread_target = lambda cfg, tg, state, user: None
    res = start_user_backfill(c, tg, st, st.user(42))
    assert res["ok"] is True
    assert st.get_meta("backfill_cancel:42") is None
    bf._BACKFILL_THREADS.pop(42, None)
    st.delete_meta("backfill_running:42")
