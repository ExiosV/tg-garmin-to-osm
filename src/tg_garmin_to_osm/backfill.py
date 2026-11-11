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
from .garmin import GarminClient
from .osm import Osm, OsmError
from .redact import redact
from .runner import (
    activity_start_ts, expand_types_user, garmin_client_for, is_ride, summary_row,
    upload_one, user_vis,
)
from .state import State
from .telegram import Telegram

log = logging.getLogger("tg_garmin_to_osm")

PAGE = 100
MAX_PAGES = 10  # 1000 activities is far beyond any three-month window
FULL_HISTORY_PAGES = 50  # the user-triggered "upload everything" goes up to 5000 activities
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
                # the user may have widened their types after the first run: backlog and
                # previously-ignored activities of a now-matching type join the upload queue
                if known["status"] in ("backlog", "ignored") and is_ride(types, a):
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


def run_backfill_user(cfg: Config, tg: Telegram | None, state: State, user: dict, since: str,
                      dry_run: bool = False) -> None:
    uid = user["user_id"]
    lang = user.get("lang") or ""
    if not user.get("garmin_linked"):
        print(i18n.text(lang, "backfill_no_garmin"))
        return
    if not user.get("osm_access_token"):
        print(i18n.text(lang, "backfill_no_login"))
        return
    from .runner import garmin_client_for
    garmin = garmin_client_for(cfg, user)
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
    if not dry_run and (uploaded or failed) and tg and user.get("chat_id"):
        tg.send(user["chat_id"], summary)


def run_backfill(cfg: Config, tg: Telegram | None, state: State, since: str, dry_run: bool = False,
                 user_id: int | None = None) -> None:
    users = [u for u in state.garmin_osm_users()
             if user_id is not None and u["user_id"] == user_id
             or user_id is None and u["auto"]]  # auto-off users only go with an explicit --user
    for user in users:
        run_backfill_user(cfg, tg, state, user, since, dry_run)
    if not users:
        print("no linked users matched")


# -- user-triggered "upload everything" (button in the dialog / dashboard) ---------------------------

import threading

_BACKFILL_THREADS: dict[int, threading.Thread] = {}
_BACKFILL_LOCK = threading.Lock()


def backfill_status(state: State, uid: int) -> dict:
    running = _BACKFILL_THREADS.get(uid) is not None and _BACKFILL_THREADS[uid].is_alive()
    return {
        "running": running,
        "done": state.get_meta(f"backfill_done:{uid}") or 0,
        "total": state.get_meta(f"backfill_total:{uid}") or 0,
        "uploaded": state.get_meta(f"backfill_uploaded:{uid}") or 0,
        "skipped": state.get_meta(f"backfill_skipped:{uid}") or 0,
        "failed": state.get_meta(f"backfill_failed:{uid}") or 0,
        "error": state.get_meta(f"backfill_error:{uid}") or "",
    }


def start_user_backfill(cfg: Config, tg: Telegram | None, state: State, user: dict) -> dict:
    """Start "upload ALL history, then keep auto-uploading new rides" in a background thread.
    The main cycle skips the user while the thread is alive (work-lock discipline, no double uploads)."""
    uid = user["user_id"]
    with _BACKFILL_LOCK:
        if backfill_status(state, uid)["running"]:
            return {"ok": False, "error": "already_running", "status": backfill_status(state, uid)}
        thread = threading.Thread(target=_thread_target, daemon=True,
                                  name=f"backfill-{uid}", kwargs=dict(cfg=cfg, tg=tg, state=state, user=user))
        _BACKFILL_THREADS[uid] = thread
        state.set_meta(f"backfill_running:{uid}", "1")
        thread.start()
    return {"ok": True, "status": backfill_status(state, uid)}


def _thread_target(cfg: Config, tg: Telegram | None, state: State, user: dict) -> None:
    uid = user["user_id"]
    lang = user.get("lang") or ""
    from .garmin_api import GarminApiError
    from .redact import add_secret as _noop  # keep namespace warm; no secrets flow here
    try:
        from .garmin import GarminClient, user_token_dir
        garmin = garmin_client_for(cfg, state.user(uid) or user)
        osm = Osm(cfg.osm_base, user["osm_access_token"])
        # every page of history, deduped against the user's OSM traces
        added, flipped = _scan_all(cfg, garmin, state, user)
        rows = state.rides_by_status(uid, "new")
        try:
            traces = osm.user_traces()
        except OsmError as e:
            traces = []
            log.error("backfill user %s: user_traces failed: %s", uid, redact(e))
        dates = _trace_dates(traces)
        vis = user_vis(user, cfg)
        total = len(rows)
        uploaded = skipped = failed = 0
        state.set_meta(f"backfill_total:{uid}", str(total))
        state.set_meta(f"backfill_uploaded:{uid}", "0")
        state.set_meta(f"backfill_skipped:{uid}", "0")
        state.set_meta(f"backfill_failed:{uid}", "0")
        state.delete_meta(f"backfill_error:{uid}")
        last_progress = time.time()
        for row in rows:  # oldest first
            aid = row["activity_id"]
            ride_day = (datetime.datetime.fromtimestamp(row["start_ts"], datetime.timezone.utc).date()
                        if row["start_ts"] else None)
            if ride_day is not None and any(abs((d - ride_day).days) <= 1 for d in dates):
                state.set_status(uid, aid, "skipped")
                skipped += 1
            else:
                try:
                    if upload_one(cfg, garmin, osm, state, user, row, tg, vis, notify=False):
                        uploaded += 1
                    elif state.activity(uid, aid)["status"] == "failed":
                        failed += 1
                except GarminApiError as e:
                    failed += 1
                    log.error("backfill upload %s failed: %s", aid, redact(e))
                time.sleep(UPLOAD_PAUSE_S)
            state.set_meta(f"backfill_uploaded:{uid}", str(uploaded))
            state.set_meta(f"backfill_skipped:{uid}", str(skipped))
            state.set_meta(f"backfill_failed:{uid}", str(failed))
            if tg is not None and user.get("chat_id") and time.time() - last_progress >= 120:
                last_progress = time.time()
                try:
                    tg.send(user["chat_id"], i18n.text(lang, "backfill_progress",
                            done=uploaded + skipped + failed, total=total, uploaded=uploaded), silent=True)
                except Exception:
                    pass
        summary = i18n.text(lang, "backfill_summary", since="full history", uploaded=uploaded,
                            skipped=skipped, failed=failed)
        if tg is not None and user.get("chat_id"):
            try:
                tg.send(user["chat_id"], summary)
            except Exception:
                pass
        state.delete_meta(f"backfill_uploaded:{uid}")
        state.delete_meta(f"backfill_skipped:{uid}")
        state.delete_meta(f"backfill_failed:{uid}")
        state.delete_meta(f"backfill_total:{uid}")
        state.delete_meta(f"backfill_error:{uid}")
        log.info("backfill user %s finished: uploaded=%d skipped=%d failed=%d", uid, uploaded, skipped, failed)
    except Exception as e:
        log.error("backfill user %s crashed: %s", uid, redact(e))
        state.set_meta(f"backfill_error:{uid}", redact(e)[:200])
    finally:
        with _BACKFILL_LOCK:
            _BACKFILL_THREADS.pop(uid, None)
        state.delete_meta(f"backfill_running:{uid}")


def _scan_all(cfg: Config, garmin, state: State, user: dict) -> tuple:
    """Every activity the user ever has: pages until exhausted (50 pages = 5000)."""
    uid = user["user_id"]
    types = expand_types_user(user, cfg)
    added = flipped = 0
    since_ts = 0  # no window: the button means ALL history
    for page_no in range(FULL_HISTORY_PAGES):
        acts = garmin.activities_page(page_no * PAGE, PAGE)
        if not acts:
            break
        for a in acts:
            aid = str(a.get("activityId") or "")
            if not aid:
                continue
            known = state.activity(uid, aid)
            if known:
                # the user may have widened their types after the first run: backlog and
                # previously-ignored activities of a now-matching type join the upload queue
                if known["status"] in ("backlog", "ignored") and is_ride(types, a):
                    state.set_status(uid, aid, "new")
                    flipped += 1
                continue
            row = summary_row(a)
            status = "new" if is_ride(types, a) else "ignored"
            if state.insert_activity(uid, aid, row["name"], row["type"], row["start_ts"],
                                     row["distance_km"], status):
                added += 1
        if len(acts) < PAGE:
            break
    return added, flipped
