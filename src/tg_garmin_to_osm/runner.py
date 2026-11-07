import calendar
import html
import logging
import time

from . import gpx as gpxmod
from . import i18n
from . import osm as osmmod
from .config import TYPE_GROUPS, Config, VISIBILITIES, expand_types
from .garmin import (GarminClient, begin_login, course_id, finish_mfa, is_auth_error,
                     save_tokens, user_token_dir)
from .osm import Osm, OsmError
from .redact import redact
from .state import State
from .telegram import Telegram, UPDATES_OFFSET

log = logging.getLogger("tg_garmin_to_osm")

MAX_ATTEMPTS = 3  # permanent OSM-side upload rejections before a ride goes to 'failed'

# In-memory linking dialogs (Garmin email/password/MFA, per Telegram user). Passwords live here for
# seconds and are never persisted; a restart drops the dialog, the user just starts over.
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
    /upload <id> works for it. Returns the number of new rows.
    """
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
               tg: Telegram, vis: str, notify: bool = True) -> int:
    """Download the GPX for one ride row and upload it; marks the row and notifies. Returns the trace id.
    `notify=False` (backfill) keeps the per-trace message quiet; error notices still go out."""
    uid = user["user_id"]
    aid = row["activity_id"]
    osm_name = user.get("osm_name") or ""
    lang = user_lang(user)
    try:
        data = garmin.download_gpx(aid)
        gpxmod.validate_gpx(data)
    except Exception as e:  # Garmin-side or file problem: do not burn an attempt, retry on the next scan
        if is_auth_error(e):
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
            tg.send(user["chat_id"], i18n.text(lang, "osm_401", lang), silent=True)
        elif status == "failed":
            tg.send(user["chat_id"], i18n.text(lang, "upload_failed_final", lang,
                                               name=html.escape(row["name"] or aid),
                                               n=MAX_ATTEMPTS, err=redact(e)), silent=True)
        return 0
    state.set_status(uid, aid, "uploaded", trace_id)
    log.info("user %s: uploaded activity %s as OSM trace %s", uid, aid, trace_id)
    if notify:
        tg.send(user["chat_id"], i18n.text(lang, "uploaded", lang, desc=html.escape(desc),
                                           url=html.escape(osmmod.trace_link(cfg.osm_base, trace_id, osm_name)),
                                           id=trace_id, vis=vis))
    return trace_id


def _notify_garmin_expired(cfg: Config, tg: Telegram, state: State, user: dict) -> None:
    """Tell the user their Garmin session died, once per day (a dead session re-fails every scan)."""
    uid = user["user_id"]
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if state.get_meta(f"garmin_fail:{uid}") == today:
        return
    state.set_meta(f"garmin_fail:{uid}", today)
    try:
        tg.send(user["chat_id"], i18n.text(user_lang(user), "garmin_expired"), silent=True)
    except Exception:
        pass


def upload_ready(cfg: Config, garmin: GarminClient, tg: Telegram, state: State, user: dict) -> int:
    """Upload every due 'new' ride of this user. Returns the number uploaded."""
    osm = Osm(cfg.osm_base, user["osm_access_token"])
    vis = user_vis(user, cfg)
    uploaded = 0
    for row in state.ready_to_upload(user["user_id"], cfg.upload_delay_minutes):
        uploaded += 1 if upload_one(cfg, garmin, osm, state, user, row, tg, vis) else 0
    return uploaded


# -- trace processing ----------------------------------------------------------------------------------------------

def check_pending(cfg: Config, tg: Telegram, state: State, user: dict) -> int:
    """Flip this user's 'uploaded' rows to 'done' once OSM has processed the trace; announce the edit link."""
    osm = Osm(cfg.osm_base, user["osm_access_token"])
    osm_name = user.get("osm_name") or ""
    lang = user_lang(user)
    done = 0
    for row in state.pending_traces(user["user_id"]):
        try:
            meta = osm.trace_meta(row["trace_id"])
        except OsmError as e:
            if e.status != 404:  # 404 just means OSM has not made the trace visible yet
                log.error("trace meta failed for %s: %s", row["trace_id"], redact(e))
            continue
        if meta["pending"]:
            continue
        state.set_status(user["user_id"], row["activity_id"], "done")
        done += 1
        link = osmmod.edit_link(cfg.osm_base, row["trace_id"], meta["lat"], meta["lon"], osm_name)
        log.info("trace %s processed (user %s)", row["trace_id"], user["user_id"])
        tg.send(user["chat_id"], i18n.text(lang, "processed", url=html.escape(link), id=row["trace_id"]))
    return done


def user_cycle(cfg: Config, tg: Telegram, state: State, user: dict) -> dict:
    """One full pass for one user: notice new activities, upload due rides, report processed traces."""
    garmin = GarminClient(user_token_dir(cfg.garmin_token_dir, user["user_id"]))
    seen = scan_activities(cfg, garmin, state, user)
    return {
        "seen": seen,
        "uploaded": upload_ready(cfg, garmin, tg, state, user),
        "processed": check_pending(cfg, tg, state, user),
    }


def cycle(cfg: Config, tg: Telegram, state: State) -> list:
    """Serve every user with both accounts linked (auto-upload on). Returns per-user summaries."""
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


# -- Garmin linking dialog ------------------------------------------------------------------------------------------

def _pending_flow(cfg: Config, tg: Telegram, state: State, msg: dict, uid: int, text: str) -> bool:
    """Drive an in-progress linking dialog (Garmin email/password/MFA, OSM code). Returns True if consumed."""
    p = pending_get(uid)
    if not p:
        return False
    chat_id = (msg.get("chat") or {}).get("id")
    lang = (state.user(uid) or {}).get("lang") or ""
    if text.startswith("/cancel"):
        PENDING.pop(uid, None)
        tg.reply_msg(msg, i18n.text(lang, "cancel_done"))
        return True
    if p["stage"] == "osm_code":
        PENDING.pop(uid, None)
        _osm_link(cfg, tg, state, msg, uid, text)
        return True
    if p["stage"] == "email":
        PENDING[uid] = {**p, "stage": "password", "email": text, "ts": time.time()}
        tg.reply_msg(msg, i18n.text(lang, "garmin_ask_password"))
        return True
    if p["stage"] == "password":
        email = p["email"]
        PENDING.pop(uid, None)
        # the credentials message disappears at once; nothing is logged or stored
        tg.delete(chat_id, msg.get("message_id"))
        try:
            status, api, client_state = begin_login(email, text)
        except Exception as e:  # never surface the exception: it can carry account details
            log.error("garmin login failed for user %s: %s", uid, type(e).__name__)
            tg.reply_msg(msg, i18n.text(lang, "garmin_fail"))
            return True
        if status == "needs_mfa":
            PENDING[uid] = {"stage": "mfa", "api": api, "client_state": client_state, "ts": time.time()}
            tg.reply_msg(msg, i18n.text(lang, "garmin_ask_mfa"))
        else:
            save_tokens(api, user_token_dir(cfg.garmin_token_dir, uid))
            state.set_garmin_linked(uid, True)
            tg.reply_msg(msg, i18n.text(lang, "garmin_linked_ok"))
        return True
    if p["stage"] == "mfa":
        PENDING.pop(uid, None)
        try:
            finish_mfa(p["api"], p["client_state"], text, user_token_dir(cfg.garmin_token_dir, uid))
        except Exception as e:
            log.error("garmin mfa failed for user %s: %s", uid, type(e).__name__)
            tg.reply_msg(msg, i18n.text(lang, "garmin_mfa_fail"))
            return True
        state.set_garmin_linked(uid, True)
        tg.reply_msg(msg, i18n.text(lang, "garmin_linked_ok"))
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
    try:
        name = Osm(cfg.osm_base, token_data["access_token"]).user_name()
    except OsmError as e:
        log.warning("osm user name lookup failed: %s", redact(e))
    state.save_osm_user(uid, (msg.get("chat") or {}).get("id") or uid, token_data["access_token"], name)
    tg.reply_msg(msg, i18n.text(lang, "login_done", name=html.escape(name)))


def _status_text(cfg: Config, state: State, user: dict, T) -> str:
    counts = ", ".join(f"{k}: {v}" for k, v in sorted(state.counts(user["user_id"]).items())) or "—"
    last_scan = state.get_meta("last_scan_ts")
    scan = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(int(last_scan))) if last_scan else "—"
    return T("status", garmin="✅" if user["garmin_linked"] else "— /garmin",
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
        tg.reply_msg(msg, T("start"))
    elif cmd == "/help":
        tg.reply_msg(msg, T("help"))
    elif cmd == "/garmin":
        PENDING[uid] = {"stage": "email", "ts": time.time()}
        tg.reply_msg(msg, T("garmin_ask_email"))
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
                raw = GarminClient(user_token_dir(cfg.garmin_token_dir, uid)).activity(arg)
                row = summary_row(raw)
                state.insert_activity(uid, arg, row["name"], row["type"], row["start_ts"],
                                      row["distance_km"], "new")
            else:
                state.set_status(uid, arg, "new")
            garmin = GarminClient(user_token_dir(cfg.garmin_token_dir, uid))
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
    handled = 0
    for update in updates:
        offset = max(offset, int(update.get("update_id") or 0)) + 1
        msg = update.get("message") or {}
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


def run_forever(cfg: Config, tg: Telegram, state: State) -> None:
    fails = 0
    last_scan = 0.0
    last_check = 0.0
    if not cfg.operator_chat:
        log.warning("TELEGRAM_CHAT_ID is not set: service warnings have nowhere to go")
    while True:
        try:
            handle_updates(cfg, tg, state)
            now = time.time()
            if now - last_scan >= cfg.garmin_poll_seconds:
                last_scan = now
                cycle(cfg, tg, state)
            if now - last_check >= cfg.trace_check_seconds:
                last_check = now
                for user in state.osm_users():
                    check_pending(cfg, tg, state, user)
            fails = 0
        except Exception as e:  # keep the loop alive; warn the operator once after 10 consecutive failures
            fails += 1
            log.error("cycle failed (%d): %s", fails, redact(e))
            if fails == 10 and cfg.operator_chat:
                try:
                    tg.send(cfg.operator_chat, i18n.text("", "cycle_warn", err=redact(e)), silent=True)
                except Exception:
                    pass
        time.sleep(1)
