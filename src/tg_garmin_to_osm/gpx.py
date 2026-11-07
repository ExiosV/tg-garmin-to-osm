import time
import xml.etree.ElementTree as ET


class GpxError(ValueError):
    """The file cannot become a useful OSM trace."""


def _tag(el) -> str:
    return el.tag.rsplit("}", 1)[-1]  # namespace-agnostic


def validate_gpx(data: bytes) -> int:
    """Raise GpxError unless `data` is a GPX with track points; return the count of timestamped ones.

    OSM accepts only real <trkpt> tracks, and without <time> on the points a trace cannot confirm the
    survey date, which is the whole point of uploading rides.
    """
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise GpxError(f"not valid XML: {e}") from None
    if _tag(root).lower() != "gpx":
        raise GpxError(f"root element is <{_tag(root)}>, not <gpx>")
    points = [el for el in root.iter() if _tag(el).lower() == "trkpt"]
    if not points:
        raise GpxError("no <trkpt> track points in the file")
    timed = sum(1 for p in points if any(_tag(c).lower() == "time" for c in p))
    if not timed:
        raise GpxError("track points have no <time> stamps")
    return timed


def description(a: dict, lang: str = "en") -> str:
    """Public trace description from an activities row: "<name> (YYYY-MM-DD, 42.5 km[, course])".
    The Garmin course name is appended only when the ride actually followed one of the owner's own
    courses (a foreign/deleted course id cannot be resolved to a name)."""
    name = (a.get("name") or "Ride").strip() or "Ride"
    parts = [name]
    ts = a.get("start_ts")
    if ts:
        parts.append(time.strftime("%Y-%m-%d", time.gmtime(int(ts))))
    km = a.get("distance_km")
    if km:
        parts.append(f"{float(km):g} km")
    course = (a.get("course") or "").strip()
    if course:
        parts.append(f"по курсу «{course}»" if lang == "ru" else f'course "{course}"')
    if len(parts) == 1:
        return name
    return f"{parts[0]} ({', '.join(parts[1:])})"
