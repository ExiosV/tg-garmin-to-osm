import logging
import os
from dataclasses import dataclass, field

from . import redact

log = logging.getLogger("tg_garmin_to_osm")

# Garmin activity type groups (typeKey families as they appear in Garmin Connect summaries).
# A user subscribes to whole groups (not individual types); unknown raw keys in a custom setting are
# treated as literal typeKeys, so anything Garmin adds later can be used without a code change.
TYPE_GROUPS = {
    "bike": {"cycling", "road_biking", "gravel_cycling", "mountain_biking", "cyclocross",
             "e_bike_fitness", "e_bike_mountain", "virtual_ride", "indoor_cycling", "bike"},
    "run": {"running", "trail_running", "treadmill_running", "track_running", "virtual_run",
            "indoor_running", "ultra_run"},
    "walk": {"walking", "hiking", "trail_hiking", "virtual_walk"},
    "water": {"swimming", "pool_swimming", "open_water_swimming", "swim_run", "surfing",
              "stand_up_paddleboarding", "paddleboarding", "rowing", "rowing_erg", "kayaking",
              "canoeing", "sailing", "windsurfing", "kitesurfing", "diving", "snorkeling"},
    "winter": {"snow_skiing", "resort_skiing_snowboarding", "backcountry_skiing_snowboarding",
               "cross_country_skiing", "classic_skiing", "ski_skating", "snowboarding",
               "ice_skating", "snow_shoe"},
    "fitness": {"strength_training", "indoor_cardio", "elliptical_training", "stair_stepper",
                "yoga", "pilates", "hiit", "cardio_training", "aerobic_training", "core_training"},
}
DEFAULT_TYPES = "bike"

VISIBILITIES = ("identifiable", "trackable")


def expand_types(setting: str) -> frozenset:
    """A comma-separated user setting ("bike", "bike,walk", or raw Garmin typeKeys) -> a set of
    typeKeys; group names expand to their whole family, unknown tokens pass through literally."""
    out: set = set()
    for token in (setting or "").lower().split(","):
        token = token.strip()
        if not token:
            continue
        out |= TYPE_GROUPS.get(token, {token})
    return frozenset(out) or frozenset(TYPE_GROUPS[DEFAULT_TYPES])  # empty setting -> the default group


def parse_types_setting(value: str) -> str:
    """Tolerant normalization of the default setting: known groups kept, garbage dropped;
    empty -> DEFAULT_TYPES."""
    tokens = [t.strip().lower() for t in (value or "").split(",") if t.strip()]
    kept = [t for t in tokens if t in TYPE_GROUPS] or \
           [t for t in tokens if any(t in g for g in TYPE_GROUPS.values())]
    return ",".join(kept) or DEFAULT_TYPES


def parse_visibility(value: str) -> str:
    """Tolerant VISIBILITY: an unknown value is a warning and the safer default, never a crash."""
    v = (value or "").strip().lower()
    if v in VISIBILITIES:
        return v
    if v:
        log.warning("VISIBILITY=%r is not one of %s; using 'identifiable'", value, "/".join(VISIBILITIES))
    return "identifiable"


@dataclass
class Config:
    token: str = ""                # TELEGRAM_BOT_TOKEN
    operator_chat: str = ""        # TELEGRAM_CHAT_ID: service warnings go here (the operator)
    api_base: str = "https://api.telegram.org"
    osm_base: str = "https://www.openstreetmap.org"
    osm_client_id: str = ""
    osm_client_secret: str = ""
    garmin_email: str = ""         # legacy single-account bootstrap; users link their own via /garmin
    garmin_password: str = ""
    garmin_token_dir: str = "~/.garminconnect"
    data_dir: str = "./data"
    garmin_poll_seconds: int = 600   # scan cadence per user: one page of SCAN_PAGE, deliberately light
    trace_check_seconds: int = 300   # OSM trace processing check (pending -> done)
    visibility: str = "identifiable"  # default for new users
    upload_delay_minutes: int = 0    # window between a ride being noticed and uploaded, time to /skip it
    trace_tags: str = "bike"
    scan_page: int = 10              # activities fetched per user per scan (newest first)
    default_types: str = DEFAULT_TYPES
    ingest_token: str = ""           # bearer for the cluster-internal ingest API (the web site relays links here)
    ingest_port: int = 8080
    link_site: str = "https://garmin-osm.exiosv.com"  # where users link their accounts

    @classmethod
    def from_env(cls) -> "Config":
        e = os.environ.get
        cfg = cls(
            token=e("TELEGRAM_BOT_TOKEN", ""),
            operator_chat=e("TELEGRAM_CHAT_ID", ""),
            api_base=e("TELEGRAM_BOT_API_BASE_URL", "https://api.telegram.org").rstrip("/"),
            osm_base=e("OSM_BASE_URL", "https://www.openstreetmap.org").rstrip("/"),
            osm_client_id=e("OSM_CLIENT_ID", ""),
            osm_client_secret=e("OSM_CLIENT_SECRET", ""),
            garmin_email=e("GARMIN_EMAIL", ""),
            garmin_password=e("GARMIN_PASSWORD", ""),
            garmin_token_dir=e("GARMINTOKENS", "~/.garminconnect"),
            data_dir=e("DATA_DIR", "./data"),
            garmin_poll_seconds=int(e("GARMIN_POLL_SECONDS", "600")),
            trace_check_seconds=int(e("TRACE_CHECK_SECONDS", "300")),
            visibility=parse_visibility(e("VISIBILITY", "identifiable")),
            upload_delay_minutes=int(e("UPLOAD_DELAY_MINUTES", "0")),
            trace_tags=e("TRACE_TAGS", "bike"),
            scan_page=int(e("SCAN_PAGE", "10")),
            default_types=parse_types_setting(e("DEFAULT_TYPES", DEFAULT_TYPES)),
            ingest_token=e("INGEST_TOKEN", ""),
            ingest_port=int(e("INGEST_PORT", "8080")),
            link_site=e("LINK_SITE", "https://garmin-osm.exiosv.com").rstrip("/"),
        )
        # Exception text can carry any of these; register them before anything logs.
        redact.add_secret(cfg.token)
        redact.add_secret(cfg.osm_client_secret)
        redact.add_secret(cfg.garmin_password)
        redact.add_secret(cfg.ingest_token)
        return cfg
