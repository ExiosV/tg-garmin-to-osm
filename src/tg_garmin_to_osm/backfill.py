"""One-off backfill (`python -m tg_garmin_to_osm backfill`): upload every ride since a date that is
not already on OSM. Rides whose date falls within a day of one of the user's existing OSM traces are
considered already uploaded (manual uploads from before this service existed) and are skipped.
Runs for all linked users, or one with --user <telegram_id>."""
import datetime
import logging
import time

from . import gpx as gpxmod
from . import i18n
from .config import Config
from .garmin import GarminClient, user_token_dir
from .osm import Osm, OsmError
from .redact import redact
from .runner import activity_start_ts, expand_types_user, is_ride, summary_row, upload_one, user_vis
from .state import State
from .telegram import Telegram

log = logging.getLogger("tg_garmin_to_osm")

PAGE = 100
MAX_PAGES = 10  # 1000 activities is far beyond any three-month window
UPLOAD_PAUSE_S = 4.0  # a one-off burst of ~dozens of uploads still stays polite toward OSM


def scan_history(cfg: Config, garmin: GarminClient, state: State, user: dict, since: str) -> tuple:
    """Remember every activity from `since` (YYYY-MM-DD, UTC) that the scans have not seen yet.

    Rides matching the user's types become 'new', everything else 'ignored'; backlog rides inside
    the window are flipped to 'new' (the first-scan backlog was never meant to hold rides forever).
    Returns (added, flipped)."""
    uid = user["user_id"]
    since_ts = int(datetime.datetime.strptime(since, "%Y-%m-%d")
                   .replace(tzinfo=datetime.timezone.utc).timestamp())
    types = expand_types_user(user, cfg)
    added = flipped = 0
    for page_no in range(MAX_PAGES):
        acts = garmin.activities_page(page_no * PAGE, PAGE)
        if not acts:
            break
        older = False
        for a in acts:
            ts = activity_start_ts(a)
            if ts and ts < since_ts:
                older = True  # newest-first list: everything after this is out of the window
                continue
            aid = str(a.get("activityId") or "")
            if not aid:
                continue
            known = state.activity(uid, aid)
            if known:
                if known["status"] == "backlog" and is_ride(types, a):
                    state.set_status(uid, aid, "new")
                    flipped += 1
                continue
            row = summary_row(a)
            status = "new" if is_ride(types, a) else "ignored"
            if state.insert_activity(uid, aid, row["name"], row["type"], row["start_ts"],
                                     row["distance_km"], status):
                added += 1
                log.info("backfill user %s: seen activity %s (%s) -> %s", uid, aid, row["type"], status)
        if older or len(acts) < PAGE:
            break
    return added, flipped


def _trace_dates(traces: list) -> set:
    out = set()
    for t in traces:
        raw = str(t.get("timestamp") or "")[:10]
        try:
            out.add(datetime.date.fromisoformat(raw))
        except ValueError:
            continue
    return out


def run_backfill_user(cfg: Config, tg: Telegram, state: State, user: dict, since: str,
                      dry_run: bool = False) -> None:
    uid = user["user_id"]
    lang = user.get("lang") or ""
    if not user.get("garmin_linked"):
        print(i18n.text(lang, "backfill_no_garmin"))
        return
    if not user.get("osm_access_token"):
        print(i18n.text(lang, "backfill_no_login"))
        return
    garmin = GarminClient(user_token_dir(cfg.garmin_token_dir, uid))
    osm = Osm(cfg.osm_base, user["osm_access_token"])
    added, flipped = scan_history(cfg, garmin, state, user, since)
    rows = state.rides_by_status(uid, "new")
    try:
        traces = osm.user_traces()
    except OsmError as e:
        print("cannot list existing OSM traces:", redact(e))
        traces = []
    dates = _trace_dates(traces)
    vis = user_vis(user, cfg)
    uploaded = skipped = failed = 0
    for row in rows:  # rides_by_status is ordered by start_ts, oldest first
        aid = row["activity_id"]
        ride_day = (datetime.datetime.fromtimestamp(row["start_ts"], datetime.timezone.utc).date()
                    if row["start_ts"] else None)
        near = ride_day is not None and any(abs((d - ride_day).days) <= 1 for d in dates)
        if near:
            if not dry_run:
                state.set_status(uid, aid, "skipped")
            skipped += 1
            print(f"SKIP {aid} {gpxmod.description(row, 'en')} (an OSM trace already exists around that date)")
            continue
        if dry_run:
            print(f"WOULD UPLOAD {aid} {gpxmod.description(row, 'en')}")
            continue
        if upload_one(cfg, garmin, osm, state, user, row, tg, vis, notify=False):
            uploaded += 1
        elif state.activity(uid, aid)["status"] == "failed":
            failed += 1
        time.sleep(UPLOAD_PAUSE_S)
    summary = i18n.text(lang, "backfill_summary", since=since, uploaded=uploaded,
                        skipped=skipped, failed=failed)
    print(summary)
    print(f"user {uid}: history +{added} rows, {flipped} backlog rides flipped to 'new'; "
          f"existing OSM traces: {len(traces)}")
    if not dry_run and (uploaded or failed):
        tg.send(user["chat_id"], summary)


def run_backfill(cfg: Config, tg: Telegram, state: State, since: str, dry_run: bool = False,
                 user_id: int | None = None) -> None:
    users = [u for u in state.garmin_osm_users()
             if user_id is not None and u["user_id"] == user_id
             or user_id is None and u["auto"]]  # auto-off users only go with an explicit --user
    for user in users:
        run_backfill_user(cfg, tg, state, user, since, dry_run)
    if not users:
        print("no linked users matched")
