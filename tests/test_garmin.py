import sys
import types

import pytest

from tg_garmin_to_osm.garmin import (GarminClient, begin_login, course_id, finish_mfa,
                                     is_auth_error, save_tokens, user_token_dir)


def test_is_auth_error():
    assert is_auth_error(RuntimeError("Garmin API 401 unauthorized"))
    assert is_auth_error(RuntimeError("bad credentials for login"))
    assert not is_auth_error(RuntimeError("connection reset by peer"))


def test_user_token_dir_is_per_user(tmp_path):
    base = str(tmp_path / "garmin")
    assert user_token_dir(base, 42).endswith("/42")
    assert user_token_dir(base, 7) != user_token_dir(base, 42)


def test_course_id_extraction():
    assert course_id({"metadataDTO": {"associatedCourseId": 525118108}}) == 525118108
    assert course_id({"metadataDTO": {"associatedCourseId": None}}) is None
    assert course_id({}) is None
    assert course_id(None) is None


class _FakeGarmin:
    logins = 0

    class ActivityDownloadFormat:
        GPX = "GPX"

    def __init__(self, email=None, password=None, prompt_mfa=None, return_on_mfa=False):
        self.email, self.return_on_mfa = email, return_on_mfa
        self.logged_in = None
        self.login_result = (None, None)

    def login(self, tokenstore=None):
        type(self).logins += 1  # class counter: self.logins += 1 would shadow it per instance
        self.logged_in = tokenstore
        return self.login_result

    def resume_login(self, client_state, mfa_code):
        self.resumed = (client_state, mfa_code)
        return (None, None)

    class client:  # namespace for dump()
        dumps = []

        @classmethod
        def dump(cls, path):
            cls.dumps.append(path)

    def get_activities(self, start, limit):
        return [{"activityId": 1}, {"activityId": 2}][:limit]

    def get_activity(self, activity_id):
        return {"activityId": activity_id}

    def download_activity(self, activity_id, dl_fmt=None):
        assert dl_fmt == "GPX"
        return b"GPXBYTES"

    def get_courses(self):
        return [{"courseId": 5, "courseName": "Loop"}, {"courseId": 6, "courseName": ""}]


@pytest.fixture
def fake_garminconnect(monkeypatch):
    module = types.ModuleType("garminconnect")
    module.Garmin = _FakeGarmin
    monkeypatch.setitem(sys.modules, "garminconnect", module)
    _FakeGarmin.logins = 0
    _FakeGarmin.client.dumps = []
    return module


def test_begin_login_clean(fake_garminconnect):
    status, api, state = begin_login("me@example.com", "pw123456789")
    assert status == "ok" and state is None
    assert api.email == "me@example.com" and api.return_on_mfa is True


def test_begin_login_needs_mfa(fake_garminconnect, monkeypatch):
    def staged(self):
        pass

    monkeypatch.setattr(fake_garminconnect.Garmin, "login",
                        lambda self, tokenstore=None: ("needs_mfa", {"st": 1}))
    status, api, state = begin_login("me@example.com", "pw123456789")
    assert status == "needs_mfa"
    import json
    assert json.loads(state) == {"st": 1}


def test_finish_mfa_dumps_tokens(fake_garminconnect):
    api = _FakeGarmin()
    finish_mfa(api, '{"st": 1}', "123456", "/tmp/tok")
    assert api.resumed == ({"st": 1}, "123456")
    assert _FakeGarmin.client.dumps[-1] == "/tmp/tok"


def test_save_tokens(fake_garminconnect):
    save_tokens(_FakeGarmin(), "/tmp/tok2")
    assert _FakeGarmin.client.dumps[-1] == "/tmp/tok2"


def test_client_lazy_login_and_reuse(tmp_path, fake_garminconnect):
    client = GarminClient(str(tmp_path / "tokens"))
    assert client.recent_activities(2) == [{"activityId": 1}, {"activityId": 2}]
    assert client.recent_activities(1) == [{"activityId": 1}]
    assert _FakeGarmin.logins == 1  # one login shared across calls
    assert client._api.logged_in == str(tmp_path / "tokens")


def test_client_reset_forces_relogin(tmp_path, fake_garminconnect):
    client = GarminClient(str(tmp_path))
    client.recent_activities(1)
    client.reset()
    client.recent_activities(1)
    assert _FakeGarmin.logins == 2


def test_client_download_gpx_and_course_cache(tmp_path, fake_garminconnect):
    client = GarminClient(str(tmp_path))
    assert client.download_gpx(77) == b"GPXBYTES"
    assert client.activity(77) == {"activityId": 77}
    assert client.course_name(5) == "Loop"
    assert client.course_name(6) == ""  # empty name stored as-is
    assert client.course_name(99) == ""  # foreign id
