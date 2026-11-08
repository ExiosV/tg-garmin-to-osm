import json
import sys
import types

import pytest

from tg_garmin_to_osm import garmin_api
from tg_garmin_to_osm.garmin_api import GarminApiClient, GarminApiError, fit_to_gpx, is_auth_error


class _Resp:
    def __init__(self, status=200, text="", content=None):
        self.status_code, self._text, self.content = status, text, content or text.encode()

    @property
    def text(self):
        return self._text


SUMMARY = [{
    "activityId": 501, "activityName": "Утро", "activityType": {"typeKey": "gravel_cycling"},
    "startTimeGMT": "2026-11-08 06:12:34", "distance": 25250.0,
}]


def make_client(monkeypatch):
    calls = []
    token_exchanges = []

    def fake_post(url, headers=None, timeout=None):
        token_exchanges.append(headers)
        return _Resp(text="BEARERTOKEN1")

    def fake_get(url, headers=None, timeout=None):
        calls.append((url, headers))
        if "search/activities" in url:
            return _Resp(text=json.dumps(SUMMARY))
        if "/download-service/export/gpx/" in url:
            return _Resp(status=404, text="no gpx")
        if "/download-service/files/" in url:
            return _Resp(content=b"\x0eFITDATA")
        return _Resp(text=json.dumps({"activityId": 501}))

    monkeypatch.setattr(garmin_api.requests, "post", fake_post)
    monkeypatch.setattr(garmin_api.requests, "get", fake_get)
    monkeypatch.setattr(garmin_api, "fit_to_gpx", lambda fit: b"<gpx>converted</gpx>")
    return GarminApiClient("KEY1"), calls, token_exchanges


def test_token_exchange_uses_apikey_header(monkeypatch):
    client, _, exchanges = make_client(monkeypatch)
    client.recent_activities(1)
    assert exchanges[0]["Authorization"] == "ApiKey KEY1"
    assert client._token() == "BEARERTOKEN1"


def test_list_maps_official_shape(monkeypatch):
    client, calls, _ = make_client(monkeypatch)
    acts = client.recent_activities(10)
    assert acts[0]["activityId"] == 501 and acts[0]["activityType"]["typeKey"] == "gravel_cycling"
    assert calls[0][0].endswith("/activitylist-service/activities/search/activities?start=0&limit=10")
    assert calls[0][1]["Authorization"] == "Bearer BEARERTOKEN1"


def test_download_prefers_gpx_falls_back_to_fit(monkeypatch):
    client, calls, _ = make_client(monkeypatch)
    data = client.download_gpx(501)
    assert data == b"<gpx>converted</gpx>"  # GPX 404 -> FIT -> converted
    assert any("/download-service/files/activity/501" in u for u, _ in calls)


def test_401_reexchanges_once(monkeypatch):
    client, calls, exchanges = make_client(monkeypatch)
    state = {"n": 0}

    def fake_get(url, headers=None, timeout=None):
        state["n"] += 1
        if state["n"] == 1:
            return _Resp(status=401, text="expired")
        return _Resp(text=json.dumps(SUMMARY))

    monkeypatch.setattr(garmin_api.requests, "get", fake_get)
    assert client.recent_activities(1)[0]["activityId"] == 501
    assert len(exchanges) == 2  # one clean re-exchange after the 401


def test_is_auth_error_scope():
    assert is_auth_error(GarminApiError("x", 401))
    assert not is_auth_error(GarminApiError("x", 500))


def test_fit_to_gpx_conversion(monkeypatch):
    import fitdecode

    class Frame:
        frame_type = fitdecode.FIT_FRAME_DATA

        def __init__(self, values):
            self.values = values

        def get_value(self, name):
            return self.values.get(name)

    semilat = int(55.75 * (1 << 31) / 180)  # semicircles = degrees * 2^31 / 180
    frames = [
        Frame({"position_lat": semilat, "position_long": int(37.61 * (1 << 31) / 180), "timestamp": __import__("datetime").datetime(2026, 11, 8, 6, 12, 34)}),
        Frame({"position_lat": None}),  # skipped
    ]
    class FakeReader:
        def __init__(self, data):
            pass

        def __iter__(self):
            return iter(frames)

    monkeypatch.setattr(fitdecode, "FitReader", FakeReader)
    gpx = garmin_api.fit_to_gpx(b"raw")
    assert 'lat="55.750000"' in gpx.decode() and "lon=" in gpx.decode()
    assert "<time>2026-11-08T06:12:34Z</time>" in gpx.decode()
    assert gpx.decode().count("<trkpt") == 1
