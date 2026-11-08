"""Shared fakes for the multi-user tests."""
import tg_garmin_to_osm.runner as runner_mod
from tg_garmin_to_osm.config import Config
from tg_garmin_to_osm.runner import scan_activities
from tg_garmin_to_osm.state import State

VALID_GPX = (b"<gpx><trk><trkpt lat='1' lon='2'>"
             b"<time>2026-11-07T06:00:00Z</time></trkpt></trk></gpx>")


def cfg(tmp_path, **kw):
    defaults = dict(token="T", operator_chat="-1001", osm_client_id="cid", osm_client_secret="sec",
                    data_dir=str(tmp_path))
    defaults.update(kw)
    return Config(**defaults)


def act(aid=101, name="Утро", type_key="cycling", dist=25000, start="2026-11-07 06:12:34"):
    return {"activityId": aid, "activityName": name, "activityType": {"typeKey": type_key},
            "startTimeGMT": start, "distance": dist}


class _Garmin:
    def __init__(self, activities=(), gpx=VALID_GPX, fail_ids=(), error="garmin download 401 unauthorized",
                 detail=None, courses=(), token_dir=""):
        self.token_dir = token_dir
        self.activities = list(activities)
        self.gpx = gpx
        self.fail_ids = {str(i) for i in fail_ids}
        self.error = error
        self.detail = detail              # activity detail returned by activity() (course probes)
        self.courses = dict(courses)      # courseId -> courseName
        self.downloads, self.resets, self.fetched = [], 0, 0

    def recent_activities(self, limit=10):
        self.fetched += 1
        return self.activities

    def activity(self, aid):
        if self.detail is not None:
            return self.detail
        for a in self.activities:
            if str(a["activityId"]) == str(aid):
                return a
        raise StopIteration(aid)

    def download_gpx(self, aid):
        self.downloads.append(str(aid))
        if str(aid) in self.fail_ids:
            raise RuntimeError(self.error)
        return self.gpx

    def course_name(self, cid):
        return self.courses.get(int(cid), "")

    def reset(self):
        self.resets += 1


class _Osm:
    """Fake OSM. `pending`/`next_id` are class-level: they model the OSM server shared by every
    instance the code constructs (upload, checks); `upload_error`/`uploaded` stay per-instance."""
    instances = []
    pending = {}
    next_id = 0

    def __init__(self, base, token):
        self.base, self.token = base, token
        self.uploaded = []
        self.upload_error = None
        _Osm.instances.append(self)

    def user_name(self):
        return "osmuser"

    def upload_gpx(self, data, description, tags="", visibility="identifiable"):
        if self.upload_error:
            raise self.upload_error
        self.uploaded.append({"description": description, "tags": tags, "visibility": visibility,
                              "token": self.token})
        _Osm.next_id += 1
        _Osm.pending[_Osm.next_id] = True
        return _Osm.next_id

    def trace_meta(self, trace_id):
        if _Osm.pending.get(trace_id) == "404":
            from tg_garmin_to_osm.osm import OsmError
            raise OsmError("GET /api/0.6/gpx/5 failed", 404, "")
        return {"id": trace_id, "pending": bool(_Osm.pending.get(trace_id, False)),
                "lat": 55.75, "lon": 37.61, "description": ""}


class _Tg:
    def __init__(self):
        self.sent = []       # (chat_id, text)
        self.deleted = []    # (chat_id, message_id)
        self.updates_result = []

    def send(self, chat_id, text, silent=False):
        self.sent.append((chat_id, text))
        return len(self.sent)

    def reply_msg(self, msg, text, silent=False):
        return self.send((msg.get("chat") or {}).get("id"), text, silent)

    def delete(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))
        return True

    def updates(self, offset, wait=25):
        self.updates_calls = getattr(self, "updates_calls", [])
        self.updates_calls.append(offset)
        return self.updates_result


def msg(text="/help", chat=-1001, user=42, lang="ru", chat_type="private"):
    return {"text": text, "chat": {"id": chat, "type": chat_type},
            "from": {"id": user, "language_code": lang}, "message_id": 7}


def link_user(st, uid=42, chat=-1001, garmin=True, osm=True, lang="ru", auto="1",
              types="bike", visibility="identifiable", name="osmuser"):
    """A fully (or partially) linked user row."""
    st.touch_user(uid, chat, lang)
    if garmin:
        st.set_garmin_linked(uid, True)
    if osm:
        st.save_osm_user(uid, chat, f"TKN-{uid}", name)
    st.set_setting(uid, "auto", auto)
    st.set_setting(uid, "types", types)
    st.set_setting(uid, "visibility", visibility)
    return st.user(uid)


def seeded(tmp_path, monkeypatch, activities, user=42, gpx=VALID_GPX, detail=None, courses=(),
           auto="1", types="bike", visibility="identifiable", lang="ru", **kw):
    """A linked user past the first-run backlog; the ride is 'new' (per-user settings apply)."""
    c = cfg(tmp_path, **kw)
    st = State(c.data_dir)
    u = link_user(st, uid=user, auto=auto, types=types, visibility=visibility, lang=lang)
    garmin = _Garmin(gpx=gpx, detail=detail, courses=courses)
    tg = _Tg()
    _Osm.instances, _Osm.pending, _Osm.next_id = [], {}, 0
    scan_activities(c, garmin, st, u)                   # first run: empty backlog, sets init
    garmin.activities = list(activities)                # the ride appears
    scan_activities(c, garmin, st, u)                   # marked new / ignored
    monkeypatch.setattr(runner_mod, "Osm", _Osm)
    return c, st, garmin, tg, u


def upload_all(c, garmin, tg, st, u):
    from tg_garmin_to_osm.runner import upload_ready
    return upload_ready(c, garmin, tg, st, u)
