import threading
import tg_garmin_to_osm.backfill as bf
from test_backfill import gact, seeded_history


def test_debug3(tmp_path, monkeypatch):
    history = [gact(i, f"Ride {i}", "cycling", f"2026-09-{i:02d} 06:00:00") for i in range(1, 6)]
    c, st, garmin, tg, u = seeded_history(tmp_path, monkeypatch, history)
    monkeypatch.setattr(bf, "garmin_client_for", lambda cfg_, user: garmin)
    bf.scan_history(c, garmin, st, u, since="2026-08-22")
    rows = st.rides_by_status(42, "new")
    print("ROWS:", len(rows))
    uploads = []

    def fake_upload_one(cfg_, garmin_, osm_, state_, user_, row_, tg_, vis_, notify=True):
        uploads.append(row_["activity_id"])
        bf.cancel_backfill(state_, user_["user_id"])
        state_.set_status(user_["user_id"], row_["activity_id"], "uploaded", 900 + len(uploads))
        return 900 + len(uploads)

    monkeypatch.setattr(bf, "upload_one", fake_upload_one)

    def loud(*a, **k):
        import traceback
        traceback.print_exc()

    monkeypatch.setattr(bf.log, "error", loud)

    def fake_target():
        print("THREAD: starting")
        bf._thread_target(cfg=c, tg=tg, state=st, user=u)
        print("THREAD: done")

    th = threading.Thread(target=fake_target)
    th.start()
    th.join(timeout=20)
    print("THREAD alive:", th.is_alive())
    print("UPLOADS:", uploads)
