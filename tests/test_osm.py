import pytest

from tg_garmin_to_osm import osm as osmmod
from tg_garmin_to_osm.osm import Osm, OsmError, authorize_url, edit_link, exchange_code, trace_link


class _Resp:
    def __init__(self, status=200, text="", json=None, content=None):
        self.status_code, self._text, self._json, self.content = status, text, json or {}, content

    @property
    def text(self):
        return self._text

    def json(self):
        return self._json


GPX = (b"<gpx><trk><trkpt lat='1' lon='2'>"
       b"<time>2026-11-07T06:00:00Z</time></trkpt></trk></gpx>")


def test_authorize_url():
    url = authorize_url("https://www.openstreetmap.org", "cid123")
    assert url.startswith("https://www.openstreetmap.org/oauth2/authorize?")
    assert "client_id=cid123" in url
    assert "redirect_uri=urn%3Aietf%3Awg%3Aoauth%3A2.0%3Aoob" in url
    assert "scope=read_prefs+read_gpx+write_gpx" in url


def test_exchange_code_success(monkeypatch):
    seen = {}

    def fake_post(url, data=None, timeout=None):
        seen.update(url=url, data=data)
        return _Resp(json={"access_token": "TKN", "token_type": "Bearer"})

    monkeypatch.setattr(osmmod.requests, "post", fake_post)
    assert exchange_code("https://www.openstreetmap.org", "cid", "sec", "CODE")["access_token"] == "TKN"
    assert seen["data"]["grant_type"] == "authorization_code"
    assert seen["data"]["code"] == "CODE" and seen["data"]["client_secret"] == "sec"


def test_exchange_code_failure(monkeypatch):
    monkeypatch.setattr(osmmod.requests, "post", lambda *a, **k: _Resp(status=400, text="bad code"))
    with pytest.raises(OsmError):
        exchange_code("https://www.openstreetmap.org", "cid", "sec", "BAD")


def test_upload_gpx_success(monkeypatch):
    seen = {}

    def fake_request(method, url, headers=None, timeout=None, files=None, data=None):
        seen.update(method=method, url=url, headers=headers, files=files, data=data)
        return _Resp(text=" 777\n")

    monkeypatch.setattr(osmmod.requests, "request", fake_request)
    assert Osm("https://www.openstreetmap.org/", "TKN").upload_gpx(
        GPX, "Ride (2026-11-07)", "bike", "identifiable") == 777
    assert seen["method"] == "POST" and seen["url"].endswith("/api/0.6/gpx")
    assert seen["headers"]["Authorization"] == "Bearer TKN"
    assert seen["files"]["file"][0] == "track.gpx"
    assert seen["data"]["visibility"] == "identifiable"
    assert seen["data"]["description"] == "Ride (2026-11-07)"


def test_upload_gpx_local_validation(monkeypatch):
    called = []
    monkeypatch.setattr(osmmod.requests, "request", lambda *a, **k: called.append(1))
    osm = Osm("https://www.openstreetmap.org", "TKN")
    with pytest.raises(OsmError):
        osm.upload_gpx(GPX, "   ")  # empty description
    with pytest.raises(OsmError):
        osm.upload_gpx(GPX, "d", visibility="public")  # deprecated for API uploads
    with pytest.raises(OsmError):
        osm.upload_gpx(GPX, "d", visibility="private")
    assert not called  # nothing was sent


def test_upload_gpx_truncates_description(monkeypatch):
    seen = {}
    monkeypatch.setattr(osmmod.requests, "request",
                        lambda m, u, headers=None, timeout=None, files=None, data=None:
                        seen.update(data=data) or _Resp(text="1"))
    Osm("https://www.openstreetmap.org", "T").upload_gpx(GPX, "x" * 300)
    assert len(seen["data"]["description"]) == 255


def test_upload_gpx_http_and_transport_errors(monkeypatch):
    monkeypatch.setattr(osmmod.requests, "request",
                        lambda *a, **k: _Resp(status=400, text="description empty"))
    with pytest.raises(OsmError) as e:
        Osm("https://www.openstreetmap.org", "T").upload_gpx(GPX, "d")
    assert e.value.status == 400

    def boom(*a, **k):
        raise osmmod.requests.ConnectionError("no route")

    monkeypatch.setattr(osmmod.requests, "request", boom)
    with pytest.raises(OsmError) as e:
        Osm("https://www.openstreetmap.org", "T").upload_gpx(GPX, "d")
    assert e.value.status == 0


def test_trace_meta(monkeypatch):
    payload = {"gpx_file": {"id": 777, "pending": False, "lat": 55.751, "lon": 37.618,
                            "description": "Ride"}}
    monkeypatch.setattr(osmmod.requests, "request", lambda *a, **k: _Resp(json=payload))
    meta = Osm("https://www.openstreetmap.org", "T").trace_meta(777)
    assert meta == {"id": 777, "pending": False, "lat": 55.751, "lon": 37.618, "description": "Ride"}


def test_links():
    base = "https://www.openstreetmap.org"
    # OSM (2026) serves trace pages at /user/<name>/traces/<id>; the bare /traces/<id> is a dead 404
    assert trace_link(base, 5, "mapper") == f"{base}/user/mapper/traces/5"
    assert trace_link(base, 5) == f"{base}/traces/5"  # legacy shape when the name is unknown
    assert edit_link(base, 5, 55.751234, 37.618787) == f"{base}/edit?editor=id&gpx=5#map=17/55.75123/37.61879"
    assert edit_link(base, 5, None, None, "mapper") == f"{base}/user/mapper/traces/5"


def test_user_traces_parses_xml(monkeypatch):
    xml = (b"<osm generator=\"OpenStreetMap server\">"
           b"<gpx_file id=\"12501706\" name=\"track.gpx\" lat=\"34.6\" lon=\"33.0\" user=\"someone\" "
           b"visibility=\"identifiable\" pending=\"false\" timestamp=\"2026-11-07T02:53:24Z\">"
           b"<description>Ride (2026-11-04, 20.82 km)</description><tag>bike</tag>"
           b"</gpx_file>"
           b"<gpx_file id=\"999\" name=\"old.gpx\" user=\"someone\" visibility=\"public\" "
           b"pending=\"true\" timestamp=\"2026-09-16T12:00:00+01:00\"/>"
           b"</osm>")
    monkeypatch.setattr(osmmod.requests, "request",
                        lambda *a, **k: _Resp(status=200, content=xml))
    traces = Osm("https://www.openstreetmap.org", "T").user_traces()
    assert len(traces) == 2
    assert traces[0]["id"] == "12501706" and traces[0]["pending"] == "false"
    assert traces[0]["description"].startswith("Ride")
    assert traces[1]["id"] == "999" and traces[1]["description"] is None
