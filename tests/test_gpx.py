import pytest

from tg_garmin_to_osm.gpx import GpxError, description, validate_gpx

GPX = """<gpx version="1.1" creator="Garmin" xmlns="http://www.topografix.com/GPX/1/1">
<trk><trkseg>
<trkpt lat="55.7" lon="37.6"><ele>150</ele><time>2026-11-07T06:12:34Z</time></trkpt>
<trkpt lat="55.71" lon="37.61"><time>2026-11-07T06:13:00Z</time></trkpt>
</trkseg></trk></gpx>"""


def test_valid_gpx():
    assert validate_gpx(GPX.encode()) == 2


def test_rejects_waypoints_only():
    wp = "<gpx><wpt lat='1' lon='2'><time>2026-11-07T06:00:00Z</time></wpt></gpx>"
    with pytest.raises(GpxError, match="trkpt"):
        validate_gpx(wp.encode())


def test_rejects_points_without_time():
    with pytest.raises(GpxError, match="time"):
        validate_gpx(b"<gpx><trk><trkpt lat='1' lon='2'/></trk></gpx>")


def test_rejects_invalid_xml_and_non_gpx_root():
    with pytest.raises(GpxError, match="XML"):
        validate_gpx(b"<gpx><trk></gpx>")
    with pytest.raises(GpxError, match="root element"):
        validate_gpx(b"<kml><Document/></kml>")


def test_description_full_and_course():
    row = {"name": "Утренний круг", "start_ts": 1772991154, "distance_km": 42.5}
    assert description(row, "ru").startswith("Утренний круг (2026-")
    assert "42.5 km)" in description(row, "ru")
    course = {"name": "Поездка", "start_ts": 0, "distance_km": 0, "course": "Lefkara Classic"}
    assert "по курсу «Lefkara Classic»" in description(course, "ru")
    assert 'course "Lefkara Classic"' in description(course, "en")
    assert description({"name": "X", "course": ""}, "en") == "X"  # no course -> no suffix
