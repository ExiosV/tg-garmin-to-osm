import calendar
import html
import logging
import os
import threading
import time

from . import gpx as gpxmod
from .gpx import GpxError
from . import i18n
from . import osm as osmmod
from .config import TYPE_GROUPS, Config, VISIBILITIES, expand_types
from .garmin import GarminClient, course_id, is_auth_error, user_token_dir
from .garmin_api import GarminApiClient, GarminApiError, GarminFileError
from .garmin_api import is_auth_error as api_is_auth_error
from . import onboarding
from .osm import Osm, OsmError
from .redact import add_secret, redact
from .state import State
from .telegram import Telegram, UPDATES_OFFSET

log = logging.getLogger("tg_garmin_to_osm")

MAX_ATTEMPTS = 3  # permanent OSM-side upload rejections before a ride goes to 'failed'
TRACE_GONE_SECONDS = 2 * 60 * 60  # an uploaded trace missing on OSM after this long is gone

# Module-level work lock shared by Runner and API handlers
WORK_LOCK = threading.RLock()

# In-memory dialog for the /login out-of-band code (per Telegram user). Account linking itself
# happens on the web site (garmin-osm.exiosv.com), which delivers tokens to the ingest API.
PENDING: dict = {}


def pending_get(uid: int) -> dict | None:
    p = PENDING.get(uid)
    if p and time.time() - p["ts"] > i18n.PENDING_TTL:
        PENDING.pop(uid, None)
        return None
    return p


def activity_start_ts(a: dict) -> int:
    """Epoch seconds from the Garmin summary (startTimeGMT is UTC wall time); 0 when unparseable."""
    raw = a.get("startTimeGMT") or ""
    try:
        return calendar.timegm(time.strptime(raw, "%Y-%m-%d %H:%M:%S"))
    except (ValueError, TypeError):
        return 0


def type_key(a: dict) -> str:
    kind = a.get("activityType")
    return (kind.get("typeKey", "") if isinstance(kind, dict) else (kind or ""))


def is_ride(types: frozenset, a: dict) -> bool:
    return type_key(a).strip().lower() in types


def expand_types_user(user: dict, cfg: Config) -> frozenset:
    """The user's subscribed typeKeys: their setting (groups + raw keys) over the env default."""
    return expand_types(user.get("types") or cfg.default_types)


def user_vis(user: dict, cfg: Config) -> str:
    """Per-user visibility; the row default comes from the env at first contact."""
    return user.get("visibility") or cfg.visibility


def user_lang(user: dict) -> str:
    return user.get("lang") or ""


def telegram_link_site(cfg: Config, lang: str) -> str:
    """The site page with the Telegram sign-in widget (the site root offers only OSM sign-in)."""
    return f"{cfg.link_site}/ru/telegram" if lang == i18n.RU else f"{cfg.link_site}/telegram"


def summary_row(a: dict) -> dict:
    """An activities-table row shape from a raw Garmin summary (also used by /upload for unknown ids)."""
    return {
        "name": a.get("activityName") or "",
        "type": type_key(a),
        "start_ts": activity_start_ts(a),
        "distance_km": round((a.get("distance") or 0) / 1000, 2),
    }


def _detail_course(garmin: GarminClient, aid: str) -> str:
    """Course name for the trace description; only rides linked to one of the user's own courses
    resolve to a name (a foreign/deleted course id cannot be fetched)."""
    try:
        cid = course_id(garmin.activity(aid))
        return garmin.course_name(cid) if cid else ""
    except Exception as e:
        log.debug("course lookup failed for %s: %s", aid, redact(e))
        return ""


# -- scan ----------------------------------------------------------------------------------------------------------

def scan_activities(cfg: Config, garmin: GarminClient, state: State, user: dict) -> int:
    """Fetch the user's newest Garmin activities and remember the unseen ones.

    Activities matching the user's types become 'new' (uploaded once the UPLOAD_DELAY window
    passes), everything else 'ignored' (/upload can still push it). On the very first scan after
    linking, the whole backlog is marked 'backlog': nothing old is uploaded en masse, but
    /upload <id> works for it. Returns the number of new rows."""
    uid = user["user_id"]
    first = state.get_meta(f"init:{uid}") is None
    types = expand_types_user(user, cfg)
    added = 0
    for a in garmin.recent_activities(cfg.scan_page):
        aid = str(a.get("activityId") or "")
        if not aid or state.activity(uid, aid):
            continue
        row = summary_row(a)
        status = "backlog" if first else ("new" if is_ride(types, a) else "ignored")
        if state.insert_activity(uid, aid, row["name"], row["type"], row["start_ts"],
                                 row["distance_km"], status):
            added += 1
            log.info("user %s: seen activity %s (%s, %s) -> %s", uid, aid, row["type"], row["name"], status)
    state.set_meta(f"init:{uid}", "1")
    return added


# -- notification helper --------------------------------------------------------------------------------------------

def _notify(cfg: Config, tg: Telegram | None, state: State, user: dict, kind: str, data: dict,
            tg_text_key: str = "", tg_fmt: dict | None = None) -> None:
    """Write an event row; send Telegram only when chat_id is set, Telegram is on (token non-empty),
    tg is not None, and tg_text_key is non-empty."""
    state.add_event(user["user_id"], kind, data)
    chat_id = user.get("chat_id")
    if chat_id and cfg.token and tg is not None and tg_text_key:
        try:
            lang = user_lang(user)
            text = i18n.text(lang, tg_text_key, **(tg_fmt or {}))
            tg.send(chat_id, text, silent=True)
        except Exception as e:
            log.warning("notify telegram failed for user %s: %s", user["user_id"], redact(e))


# -- upload --------------------------------------------------------------------------------------------------------

def _fail_osm(state: State, user: dict, row: dict, e: OsmError) -> str:
    """One OSM-side failure: transient errors do not burn an attempt, permanent ones do."""
    transient = e.status in (0, 429) or e.status >= 500
    if transient:
        log.warning("transient OSM error for %s/%s (will retry): %s",
                    user["user_id"], row["activity_id"], redact(e))
        return "new"
    return state.record_failure(user["user_id"], row["activity_id"], redact(e), MAX_ATTEMPTS)


def upload_one(cfg: Config, garmin: GarminClient, osm: Osm, state: State, user: dict, row: dict,
               tg: Telegram | None, vis: str, notify: bool = True) -> int:
    """Download the GPX for one ride row and upload it; marks the row and notifies. Returns the trace id.
    `notify=False` (backfill) keeps the per-trace message quiet; error notices still go out."""
    uid = user["user_id"]
    aid = row["activity_id"]
    osm_name = user.get("osm_name") or ""
    lang = user_lang(user)
    try:
        data = garmin.download_gpx(aid)
        gpxmod.validate_gpx(data)
    except (GpxError, GarminFileError, ValueError) as e:
        # File problem (no track, corrupt archive): fail at once and retry never. The site shows the
        # reason; no Telegram message, because "N attempts" would be misleading here.
        state.record_failure(uid, aid, "no_track: " + redact(e), 1)
        _notify(cfg, tg, state, user, "upload_failed",
                {"activity_id": aid, "name": row["name"] or aid, "error": redact(e)})
        log.error("garmin file problem for %s/%s: %s", uid, aid, redact(e))
        return 0
    except Exception as e:  # Garmin-side problem: do not burn an attempt, retry on the next scan
        if is_auth_error(e) or api_is_auth_error(e):
            if hasattr(garmin, "reset"):
                garmin.reset()
            _notify_garmin_expired(cfg, tg, state, user)
        log.error("garmin download failed for %s/%s: %s", uid, aid, redact(e))
        return 0
    row = dict(row)
    row["course"] = _detail_course(garmin, aid)
    desc = gpxmod.description(row, lang)
    try:
        trace_id = osm.upload_gpx(data, desc, cfg.trace_tags, vis)
    except OsmError as e:
        status = _fail_osm(state, user, row, e)
        if e.status == 401:
            _notify(cfg, tg, state, user, "osm_unauthorized", {},
                    "osm_401", {"lang": lang})
        elif status == "failed":
            _notify(cfg, tg, state, user, "upload_failed",
                    {"activity_id": aid, "name": row["name"] or aid, "error": redact(e)},
                    "upload_failed_final", {"lang": lang, "name": html.escape(row["name"] or aid),
                                            "n": MAX_ATTEMPTS, "err": redact(e)})
        return 0
    state.set_status(uid, aid, "uploaded", trace_id)
    log.info("user %s: uploaded activity %s as OSM trace %s", uid, aid, trace_id)
    if notify:
        _notify(cfg, tg, state, user, "uploaded",
                {"activity_id": aid, "name": row["name"], "trace_id": trace_id,
                 "trace_url": osmmod.trace_link(cfg.osm_base, trace_id, osm_name)},
                "uploaded", {"lang": lang, "desc": html.escape(desc),
                             "url": html.escape(osmmod.trace_link(cfg.osm_base, trace_id, osm_name)),
                             "id": trace_id, "vis": vis})
    return trace_id


def _notify_garmin_expired(cfg: Config, tg: Telegram | None, state: State, user: dict) -> None:
    """Tell the user their Garmin session died, once per day (a dead session re-fails every scan)."""
    uid = user["user_id"]
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if state.get_meta(f"garmin_fail:{uid}") == today:
        return
    state.set_meta(f"garmin_fail:{uid}", today)
    _notify(cfg, tg, state, user, "garmin_expired", {},
            "garmin_expired", {"lang": user_lang(user)})


def upload_ready(cfg: Config, garmin: GarminClient, tg: Telegram | None, state: State, user: dict) -> int:
    """Upload every due 'new' ride of this user. Returns the number uploaded."""
    osm = Osm(cfg.osm_base, user["osm_access_token"])
    vis = user_vis(user, cfg)
    uploaded = 0
    for row in state.ready_to_upload(user["user_id"], cfg.upload_delay_minutes):
        uploaded += 1 if upload_one(cfg, garmin, osm, state, user, row, tg, vis) else 0
    return uploaded


# -- trace processing ----------------------------------------------------------------------------------------------

def check_pending(cfg: Config, tg: Telegram | None, state: State, user: dict) -> int:
    """Flip this user's 'uploaded' rows to 'done' once OSM has processed the trace; announce the edit link."""
    osm = Osm(cfg.osm_base, user["osm_access_token"])
    osm_name = user.get("osm_name") or ""
    lang = user_lang(user)
    done = 0
    for row in state.pending_traces(user["user_id"]):
        try:
            meta = osm.trace_meta(row["trace_id"])
        except OsmError as e:
            if e.status != 404:  # a 404 may just mean OSM has not made the trace visible yet
                log.error("trace meta failed for %s: %s", row["trace_id"], redact(e))
                continue
            age = time.time() - (row.get("uploaded_ts") or 0)
            if age > TRACE_GONE_SECONDS:
                # The trace never appeared: it is gone (deleted or rejected), so stop polling.
                error = "osm: trace not found on OSM after 2 hours"
                state.record_failure(user["user_id"], row["activity_id"], error, 1)
                _notify(cfg, tg, state, user, "upload_failed",
                        {"activity_id": row["activity_id"], "name": row["name"],
                         "trace_id": row["trace_id"], "error": error})
                log.error("trace %s of user %s is gone after 2 hours", row["trace_id"], user["user_id"])
            continue
        if meta["pending"]:
            continue
        state.set_status(user["user_id"], row["activity_id"], "done")
        done += 1
        link = osmmod.edit_link(cfg.osm_base, row["trace_id"], meta["lat"], meta["lon"], osm_name)
        log.info("trace %s processed (user %s)", row["trace_id"], user["user_id"])
        _notify(cfg, tg, state, user, "processed",
                {"activity_id": row["activity_id"], "name": row["name"],
                 "trace_id": row["trace_id"], "trace_url": osmmod.trace_link(cfg.osm_base, row["trace_id"], osm_name),
                 "edit_url": link},
                "processed", {"lang": lang, "url": html.escape(link), "id": row["trace_id"]})
    return done


def garmin_client_for(cfg: Config, user: dict):
    """The right Garmin client for the user's linkage kind: Personal API key (official) or the
    legacy garminconnect token store from the web-site handshake."""
    if user.get("garmin_api_key"):
        return GarminApiClient(user["garmin_api_key"])
    return GarminClient(user_token_dir(cfg.garmin_token_dir, user["user_id"]))


def user_cycle(cfg: Config, tg: Telegram | None, state: State, user: dict) -> dict:
    """One full pass for one user: notice new activities, upload due rides, report processed traces."""
    garmin = garmin_client_for(cfg, state.user(user["user_id"]) or user)
    seen = scan_activities(cfg, garmin, state, user)
    state.delete_meta(f"garmin_fail:{user['user_id']}")  # the scan worked: Garmin is not failing
    return {
        "seen": seen,
        "uploaded": upload_ready(cfg, garmin, tg, state, user),
        "processed": check_pending(cfg, tg, state, user),
    }


def backfill_osm_uids(cfg: Config, state: State) -> int:
    """Store osm_uid once for users with an OSM token and no osm_uid. Errors are skipped quietly:
    the next cycle retries. Returns the number of rows filled."""
    filled = 0
    for user in state.osm_users():
        if user.get("osm_uid") is not None:
            continue
        try:
            osm_uid, _ = Osm(cfg.osm_base, user["osm_access_token"]).user_details()
            if not osm_uid:
                continue
            other = state.find_by_osm_uid(osm_uid)
            if other and other["user_id"] != user["user_id"]:
                log.warning("osm_uid backfill: user %s has the OSM account of user %s, skipped",
                            user["user_id"], other["user_id"])
                continue
            state.set_osm_uid(user["user_id"], osm_uid)
            filled += 1
        except Exception as e:  # 401, network, unique race: retry next cycle
            log.info("osm_uid backfill skipped for user %s: %s", user["user_id"], redact(e))
    return filled


def cycle(cfg: Config, tg: Telegram | None, state: State) -> list:
    """Serve every user with both accounts linked (auto-upload on). Returns per-user summaries."""
    backfill_osm_uids(cfg, state)
    results = []
    for user in state.garmin_osm_users():
        if not user["auto"]:
            continue
        try:
            res = user_cycle(cfg, tg, state, user)
            res["user_id"] = user["user_id"]
            state.set_meta("last_scan_ts", str(int(time.time())))
            results.append(res)
        except Exception as e:  # one broken user must not stall the others
            log.error("cycle failed for user %s: %s", user["user_id"], redact(e))
        time.sleep(1)  # pace multi-user scans a little toward Garmin
    return results


# -- linking ---------------------------------------------------------------------------------------------------------

def apply_garmin_key_pure(cfg: Config, state: State, uid: int, key: str) -> tuple[bool, str | None, str | None, int]:
    """Verify a Personal API key with recent_activities(3); only a working key is stored, with
    garmin_owner_id/garmin_name from ownerId/ownerDisplayName of the first activity (NULL when absent).
    A failed check leaves the stored key and link alone (a typo or an outage must not unlink).
    Returns (ok, garmin_name_or_none, error_code_or_none, activity_count).
    error_code: 'bad_key' for 401/403 from token exchange, 'garmin_unavailable' for network/5xx."""
    add_secret(key)
    try:
        acts = GarminApiClient(key).recent_activities(3) or []
    except GarminApiError as e:
        log.warning("garmin key check failed: %s", redact(e))
        return False, None, ("bad_key" if e.status in (401, 403) else "garmin_unavailable"), 0
    except Exception as e:
        log.error("garmin key check failed: %s", redact(e))
        return False, None, "garmin_unavailable", 0
    first = acts[0] if acts else {}
    owner_id = str(first.get("ownerId") or "") or None
    garmin_name = str(first.get("ownerDisplayName") or "") or None
    state.set_garmin_key(uid, key)
    state.set_garmin_owner(uid, owner_id, garmin_name)  # a new key may be another Garmin account
    state.delete_meta(f"garmin_fail:{uid}")  # a fresh, working key
    return True, garmin_name, None, len(acts)


def apply_garmin_key(cfg: Config, tg: Telegram, state: State, uid: int, key: str) -> None:
    """Store a Personal API key (from the dialog or /garminkey), verify it live, reply via Telegram."""
    ok, garmin_name, err, n = apply_garmin_key_pure(cfg, state, uid, key)
    user = state.user(uid)
    lang = user_lang(user or {})
    if ok:
        tg.send(user["chat_id"], i18n.text(lang, "garminkey_ok", n=n))
        _notify(cfg, None, state, user, "garmin_linked", {"name": garmin_name})  # event only: replied above
    else:
        tg.send(user["chat_id"], i18n.text(lang, "garminkey_bad"))


def _send_guide(cfg: Config, tg: Telegram, chat_id: int, lang: str) -> None:
    """The picture guide: real screenshots from assets/ when present, otherwise the text steps."""
    import os
    assets = os.path.join(os.path.dirname(__file__), "assets")
    try:
        files = sorted(f for f in os.listdir(assets) if f.lower().endswith((".png", ".jpg")))
    except OSError:
        files = []
    caption = onboarding.garmin_guide_caption(lang)
    sent = 0
    for f in files:
        try:
            with open(os.path.join(assets, f), "rb") as fh:
                tg.photo(chat_id, caption if sent == 0 else "", fh.read(), f)
            sent += 1
        except Exception as e:
            log.warning("guide photo %s failed: %s", f, redact(e))
    if not sent:  # no assets shipped: the text steps carry the guide
        tg.send(chat_id, caption)


def _handle_callback(cfg: Config, tg: Telegram, state: State, cb: dict) -> bool:
    """Inline-button presses from the onboarding dialog. Always answers the callback."""
    cid = cb.get("id")
    data = cb.get("data") or ""
    msg = cb.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    uid = (cb.get("from") or {}).get("id") or 0
    if cid:
        tg.answer_callback(cid)
    if not uid or not chat_id:
        return False
    user = state.touch_user(uid, chat_id, (cb.get("from") or {}).get("language_code") or "")
    lang = user_lang(user)
    linked_garmin = bool(user["garmin_linked"])
    linked_osm = bool(user["osm_access_token"])

    def T(key, **kw):
        return i18n.text(lang, key, **kw)

    def menu(kind):
        text, rows = (onboarding.garmin_menu_rows(lang, linked_garmin) if kind == "garmin"
                      else onboarding.osm_menu_rows(lang, linked_osm))
        tg.buttons(chat_id, text, rows)

    if data == onboarding.CB_LINK_MENU:
        menu("garmin")
    elif data == onboarding.CB_GARMIN_START:
        menu("garmin")
    elif data == onboarding.CB_GARMIN_HAVE_KEY:
        PENDING[uid] = {"stage": onboarding.STAGE_GARMIN_KEY, "ts": time.time()}
        tg.send(chat_id, T("onb_send_key"))
    elif data == onboarding.CB_GARMIN_GUIDE:
        _send_guide(cfg, tg, chat_id, lang)
    elif data == onboarding.CB_OSM_MENU:
        menu("osm")
    elif data == onboarding.CB_OSM_SITE:
        tg.send(chat_id, T("link_site", url=telegram_link_site(cfg, lang)))
    elif data == onboarding.CB_OSM_CODE:
        if not cfg.osm_client_id:
            tg.send(chat_id, T("login_no_client"))
            return True
        PENDING[uid] = {"stage": onboarding.STAGE_OSM_CODE, "ts": time.time()}
        tg.send(chat_id, T("login", url=osmmod.authorize_url(cfg.osm_base, cfg.osm_client_id)))
    elif data == onboarding.CB_STATUS:
        tg.send(chat_id, _status_text(cfg, state, state.user(uid) or user,
                                      lambda key, **kw: i18n.text(lang, key, **kw)))
    elif data == onboarding.CB_BACK:
        text, rows = onboarding.main_menu_rows(lang)
        tg.buttons(chat_id, text, rows)
    elif data == "onb:garmin:unlink":
        state.unlink_garmin(uid)
        tg.send(chat_id, T("unlink_done", what="garmin"))
        menu("garmin")
    elif data == "onb:osm:unlink":
        state.unlink_osm(uid)
        tg.send(chat_id, T("unlink_done", what="osm"))
        menu("osm")
    else:
        log.debug("unknown callback data: %s", data)
    return True


def ingest_garmin_link(cfg: Config, tg: Telegram | None, state: State, uid: int, tokens: str) -> None:
    """Ingest callback: the web site completed a Garmin login and sends the token store over."""
    d = user_token_dir(cfg.garmin_token_dir, uid)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "garmin_tokens.json"), "w") as f:
        f.write(tokens)
    os.chmod(os.path.join(d, "garmin_tokens.json"), 0o600)
    state.set_garmin_linked(uid, True)
    user = state.user(uid)
    if user and user["chat_id"] and tg:
        try:
            tg.send(user["chat_id"], i18n.text(user_lang(user), "garmin_linked_ok"))
        except Exception as e:
            log.warning("ingest notify failed: %s", redact(e))
    log.info("user %s: Garmin tokens ingested from the web site", uid)


def ingest_osm_link(cfg: Config, tg: Telegram | None, state: State, uid: int, access_token: str,
                    name: str, osm_uid: int | None = None) -> None:
    """Ingest callback: the web site completed the OSM OAuth redirect flow."""
    user = state.user(uid)
    chat_id = user["chat_id"] if user else uid  # private chats: chat id == user id
    state.save_osm_user(uid, chat_id, access_token, name, osm_uid)
    if chat_id and tg:
        try:
            tg.send(chat_id, i18n.text(user_lang(user or {}), "login_done", name=html.escape(name)))
        except Exception as e:
            log.warning("ingest notify failed: %s", redact(e))
    log.info("user %s: OSM token ingested from the web site", uid)


def _pending_flow(cfg: Config, tg: Telegram, state: State, msg: dict, uid: int, text: str) -> bool:
    """Drive an in-progress dialog (OSM code, Garmin key). Returns True if consumed.
    Any /command other than /cancel abandons the dialog — commands always work."""
    p = pending_get(uid)
    if not p:
        return False
    lang = (state.user(uid) or {}).get("lang") or ""
    if text.startswith("/cancel"):
        PENDING.pop(uid, None)
        tg.reply_msg(msg, i18n.text(lang, "cancel_done"))
        return True
    if text.startswith("/"):
        PENDING.pop(uid, None)  # a command wins over a stale dialog
        return False
    if p["stage"] == "osm_code":
        PENDING.pop(uid, None)
        _osm_link(cfg, tg, state, msg, uid, text)
        return True
    if p["stage"] == onboarding.STAGE_GARMIN_KEY:
        PENDING.pop(uid, None)
        chat_id = (msg.get("chat") or {}).get("id")
        tg.delete(chat_id, msg.get("message_id"))  # the key leaves the chat at once
        apply_garmin_key(cfg, tg, state, uid, text)
        return True
    return False


# -- commands ------------------------------------------------------------------------------------------------------

def _private_only(msg: dict) -> bool:
    """The service works in private chats only; group messages are ignored."""
    return ((msg.get("chat") or {}).get("type")) == "private"


def _osm_link(cfg: Config, tg: Telegram, state: State, msg: dict, uid: int, code: str) -> None:
    user = state.touch_user(uid, (msg.get("chat") or {}).get("id") or uid)
    lang = user_lang(user)
    try:
        token_data = osmmod.exchange_code(cfg.osm_base, cfg.osm_client_id, cfg.osm_client_secret, code)
    except (OsmError, ValueError) as e:
        log.error("OSM login failed: %s", redact(e))
        tg.reply_msg(msg, i18n.text(lang, "login_fail", err=redact(e)))
        return
    name = ""
    osm_uid = 0
    try:
        osm_uid, name = Osm(cfg.osm_base, token_data["access_token"]).user_details()
    except OsmError as e:
        log.warning("osm user details lookup failed: %s", redact(e))
    state.save_osm_user(uid, (msg.get("chat") or {}).get("id") or uid, token_data["access_token"], name, osm_uid or None)
    tg.reply_msg(msg, i18n.text(lang, "login_done", name=html.escape(name)))


def _status_text(cfg: Config, state: State, user: dict, T) -> str:
    counts = ", ".join(f"{k}: {v}" for k, v in sorted(state.counts(user["user_id"]).items())) or "—"
    last_scan = state.get_meta("last_scan_ts")
    scan = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(int(last_scan))) if last_scan else "—"
    return T("status", garmin="✅" if user["garmin_linked"] else "— /link",
             osm=html.escape(user["osm_name"]) if user["osm_name"] else "— /login",
             auto="on" if user["auto"] else "off",
             types=user.get("types") or cfg.default_types,
             vis=user_vis(user, cfg), lang=user.get("lang") or "en",
             counts=counts, scan=scan, poll=cfg.garmin_poll_seconds)


def _handle_command(cfg: Config, tg: Telegram, state: State, msg: dict, cmd: str, arg: str) -> None:
    uid = (msg.get("from") or {}).get("id") or 0
    user = state.touch_user(uid, (msg.get("chat") or {}).get("id") or uid,
                            (msg.get("from") or {}).get("language_code") or "")
    lang = user_lang(user)
    code = (msg.get("from") or {}).get("language_code")

    def T(key, **kw):
        return i18n.text(lang, key, code, **kw)

    if cmd in ("/start",):
        text, rows = onboarding.main_menu_rows(lang)
        tg.buttons(chat_id=(msg.get("chat") or {}).get("id"), text=T("start") + "\n\n" + text,
                   rows=rows)
    elif cmd == "/help":
        tg.reply_msg(msg, T("help"))
    elif cmd == "/link":
        tg.reply_msg(msg, T("link_site", url=telegram_link_site(cfg, lang)))
    elif cmd == "/garminkey":
        tg.delete((msg.get("chat") or {}).get("id"), msg.get("message_id"))  # the key leaves the chat
        if not arg:
            tg.reply_msg(msg, T("garminkey_usage"))
            return
        apply_garmin_key(cfg, tg, state, uid, arg.strip())
    elif cmd == "/login":
        if not cfg.osm_client_id:
            tg.reply_msg(msg, T("login_no_client"))
        else:
            PENDING[uid] = {"stage": "osm_code", "ts": time.time()}
            tg.reply_msg(msg, T("login", url=osmmod.authorize_url(cfg.osm_base, cfg.osm_client_id)))
    elif cmd == "/cancel":
        PENDING.pop(uid, None)
        tg.reply_msg(msg, T("cancel_done"))
    elif cmd == "/status":
        tg.reply_msg(msg, _status_text(cfg, state, user, T))
    elif cmd == "/sync":
        if not user["garmin_linked"]:
            tg.reply_msg(msg, T("sync_no_garmin"))
            return
        if not user["osm_access_token"]:
            tg.reply_msg(msg, T("sync_no_osm"))
            return
        tg.reply_msg(msg, T("sync_running"))
        res = user_cycle(cfg, tg, state, state.user(uid))
        tg.reply_msg(msg, T("sync_done", **res))
    elif cmd == "/pending":
        osm_name = user.get("osm_name") or ""
        rows = state.pending_traces(uid) + state.failed_activities(uid)
        if not rows:
            tg.reply_msg(msg, T("pending_empty"))
        for r in rows:
            mark = "⏳" if r["status"] == "uploaded" else "❌"
            link = osmmod.trace_link(cfg.osm_base, r["trace_id"], osm_name) if r["trace_id"] else ""
            extra = T("pending_link", url=html.escape(link), id=r["trace_id"]) if link else ""
            err = T("pending_err", err=html.escape(r["last_error"] or "")) if r["status"] == "failed" else ""
            tg.reply_msg(msg, f"{mark} {html.escape(gpxmod.description(r, lang))}{extra}{err}")
    elif cmd == "/skip" and arg:
        row = state.activity(uid, arg)
        if not row:
            tg.reply_msg(msg, T("skip_missing", id=html.escape(arg)))
        elif row["status"] in ("uploaded", "done"):
            tg.reply_msg(msg, T("skip_uploaded"))
        else:
            state.set_status(uid, arg, "skipped")
            tg.reply_msg(msg, T("skip_done", id=html.escape(arg)))
    elif cmd == "/upload" and arg:
        row = state.activity(uid, arg)
        if row and row["status"] in ("uploaded", "done"):
            link = osmmod.trace_link(cfg.osm_base, row["trace_id"], user.get("osm_name") or "")
            tg.reply_msg(msg, T("upload_already", url=html.escape(link), id=row["trace_id"]))
        else:
            if not user["garmin_linked"] or not user["osm_access_token"]:
                tg.reply_msg(msg, T("sync_no_garmin") if not user["garmin_linked"] else T("sync_no_osm"))
                return
            if not row:
                raw = garmin_client_for(cfg, state.user(uid) or user).activity(arg)
                row = summary_row(raw)
                state.insert_activity(uid, arg, row["name"], row["type"], row["start_ts"],
                                      row["distance_km"], "new")
            else:
                state.set_status(uid, arg, "new")
            garmin = garmin_client_for(cfg, state.user(uid))
            osm = Osm(cfg.osm_base, user["osm_access_token"])
            row = state.activity(uid, arg)
            trace_id = upload_one(cfg, garmin, osm, state, user, row, tg, user_vis(user, cfg))
            if not trace_id:
                tg.reply_msg(msg, T("upload_fail_cmd"))
    elif cmd == "/visibility":
        if arg in VISIBILITIES:
            state.set_setting(uid, "visibility", arg)
            tg.reply_msg(msg, T("visibility_set", vis=arg))
        else:
            tg.reply_msg(msg, T("visibility_show", vis=user_vis(user, cfg), opts=" / ".join(VISIBILITIES)))
    elif cmd == "/auto":
        if arg in ("on", "off"):
            state.set_setting(uid, "auto", "1" if arg == "on" else "0")
            tg.reply_msg(msg, T("auto_set", auto=arg))
        else:
            tg.reply_msg(msg, T("auto_show", auto="on" if user["auto"] else "off"))
    elif cmd == "/types":
        if arg:
            state.set_setting(uid, "types", expand_types(arg) and arg.strip().lower())
            tg.reply_msg(msg, T("types_set", types=arg.strip().lower()))
        else:
            tg.reply_msg(msg, T("types_show", groups=", ".join(TYPE_GROUPS),
                                current=user.get("types") or cfg.default_types))
    elif cmd == "/lang":
        if arg in (i18n.RU, i18n.EN):
            state.set_setting(uid, "lang", arg)
            tg.reply_msg(msg, T("lang_set", lang=arg))
        else:
            tg.reply_msg(msg, T("lang_show", lang=lang or "en"))
    elif cmd == "/unlink":
        if arg in ("garmin", "osm"):
            if arg == "garmin":
                state.unlink_garmin(uid)
            else:
                state.unlink_osm(uid)
            tg.reply_msg(msg, T("unlink_done", what=arg))
        else:
            tg.reply_msg(msg, T("unlink_usage"))
    else:
        tg.reply_msg(msg, T("unknown"))


def _handle_message(cfg: Config, tg: Telegram, state: State, msg: dict) -> bool:
    """One Telegram message from a private chat: a command, a linking-dialog step, or an OSM code.
    Returns True if answered."""
    if not _private_only(msg):
        return False
    uid = (msg.get("from") or {}).get("id") or 0
    if not uid:
        return False
    text = (msg.get("text") or "").strip()
    if not text:
        return False
    if pending_get(uid):
        return _pending_flow(cfg, tg, state, msg, uid, text)
    if not text.startswith("/"):  # a bare code is only expected right after /login
        return False
    # A shared contact with an RF-prefix number is the one hard country signal Telegram gives a bot.
    pin = i18n.phone_lang((msg.get("contact") or {}).get("phone_number"))
    parts = text.split(maxsplit=1)
    cmd = parts[0].lower().split("@")[0]  # "/sync@MyBot" -> "/sync"
    arg = parts[1].strip() if len(parts) > 1 else ""
    if pin:
        state.set_setting(uid, "lang", pin)
    _handle_command(cfg, tg, state, msg, cmd, arg)
    return True


def handle_updates(cfg: Config, tg: Telegram, state: State, wait: int = 25) -> int:
    """Long-poll Telegram and dispatch user messages. Never breaks the cycle on one bad update."""
    offset = int(state.get_meta(UPDATES_OFFSET, "0") or 0)
    updates = tg.updates(offset, wait=wait)
    ids = [int(u.get("update_id") or 0) for u in updates]
    if ids and max(ids) + 1 <= offset:
        # the server ignored our offset: it is a different bot/chat than the one this DB offset came
        # from (e.g. a token swap on an existing database) — resync, or every poll re-processes
        log.warning("stored updates_offset %s is beyond the server queue (max %s): resyncing",
                    offset, max(ids))
        offset = max(ids) - 1  # the per-update loop re-adds the +1
    handled = 0
    for update in updates:
        offset = max(offset, int(update.get("update_id") or 0)) + 1
        if update.get("callback_query"):  # onboarding dialog button press
            with WORK_LOCK:  # never during the long poll above
                try:
                    handled += 1 if _handle_callback(cfg, tg, state, update["callback_query"]) else 0
                except Exception as e:  # one bad button must not stall the service
                    log.error("callback handling failed: %s", redact(e))
            continue
        msg = update.get("message") or {}
        with WORK_LOCK:  # API calls and the cycle wait for this command
            try:
                handled += 1 if _handle_message(cfg, tg, state, msg) else 0
            except Exception as e:  # one bad message must not stall the service
                log.error("update handling failed: %s", redact(e))
                try:
                    lang = (state.user((msg.get("from") or {}).get("id") or 0) or {}).get("lang") or ""
                    tg.reply_msg(msg, i18n.text(lang, "err_reply", err=redact(e)))
                except Exception:
                    pass
    if updates:
        state.set_meta(UPDATES_OFFSET, str(offset))
    return handled


# -- runner main loop ----------------------------------------------------------------------------------------------

class Runner:
    """Main loop holder: the optional Telegram client and the shared work lock."""

    def __init__(self, cfg: Config, tg: Telegram | None, state: State):
        self.cfg = cfg
        self.tg = tg
        self.state = state
        self.work_lock = WORK_LOCK
        self._stop = threading.Event()
        self.fails = 0
        self.last_scan = 0.0
        self.last_check = 0.0
        self.lock_timeout = 5.0  # seconds to wait for a busy work lock before the next loop pass

    def poll_telegram(self) -> None:
        """One getUpdates batch. Telegram errors never reach the Garmin/OSM work."""
        if not self.tg:
            return
        try:
            handle_updates(self.cfg, self.tg, self.state)
        except Exception as e:
            log.error("telegram updates failed: %s", redact(e))
            time.sleep(5)  # back off, then go on with the cycle checks

    def work(self) -> None:
        """Due scans and trace checks under the work lock. A busy lock leaves the timers alone,
        so the work runs on the next loop pass."""
        now = time.time()
        if now - self.last_scan >= self.cfg.garmin_poll_seconds and WORK_LOCK.acquire(timeout=self.lock_timeout):
            try:
                self.last_scan = now
                cycle(self.cfg, self.tg, self.state)
            finally:
                WORK_LOCK.release()
        if now - self.last_check >= self.cfg.trace_check_seconds and WORK_LOCK.acquire(timeout=self.lock_timeout):
            try:
                self.last_check = now
                for user in self.state.osm_users():
                    check_pending(self.cfg, self.tg, self.state, user)
            finally:
                WORK_LOCK.release()

    def step(self) -> None:
        """One loop iteration."""
        self.poll_telegram()
        try:
            self.work()
            self.fails = 0
        except Exception as e:  # keep the loop alive; warn the operator once after 10 consecutive failures
            self.fails += 1
            log.error("cycle failed (%d): %s", self.fails, redact(e))
            if self.fails == 10 and self.cfg.operator_chat and self.tg:
                try:
                    self.tg.send(self.cfg.operator_chat, i18n.text("", "cycle_warn", err=redact(e)), silent=True)
                except Exception:
                    pass

    def run_forever(self) -> None:
        """Main loop. Without TELEGRAM_BOT_TOKEN (tg is None) only the Garmin/OSM work runs."""
        if self.tg and not self.cfg.operator_chat:
            log.warning("TELEGRAM_CHAT_ID is not set: service warnings have nowhere to go")
        while not self._stop.is_set():
            self.step()
            time.sleep(1)

    def stop(self) -> None:
        self._stop.set()


def run_forever(cfg: Config, tg: Telegram | None, state: State) -> None:
    """Backward-compatible wrapper."""
    Runner(cfg, tg, state).run_forever()
