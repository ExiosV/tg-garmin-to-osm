import argparse
import logging
import sys

from .backfill import run_backfill
from .config import Config
from .ingest import serve as serve_ingest
from .garmin import GarminClient, user_token_dir
from .osm import Osm, OsmError
from .redact import redact
from .runner import cycle, handle_updates, ingest_garmin_link, ingest_osm_link, run_forever
from .state import State
from .telegram import Telegram

log = logging.getLogger("tg_garmin_to_osm")


def _check_garmin(cfg: Config, state: State) -> None:
    for user in state.garmin_users():
        print("garmin token dir:", user_token_dir(cfg.garmin_token_dir, user["user_id"]))
        try:
            acts = GarminClient(user_token_dir(cfg.garmin_token_dir, user["user_id"])).recent_activities(3)
            latest = "; ".join(f"{a.get('activityName')} ({a.get('startTimeGMT', '?')})" for a in acts[:3])
            print(f"garmin (user {user['user_id']}): OK,", len(acts), "activities | latest:", latest or "-")
        except Exception as e:
            print(f"garmin (user {user['user_id']}): error:", redact(e))


def _check_osm(cfg: Config, state: State) -> None:
    if not cfg.osm_client_id:
        print("osm app: OSM_CLIENT_ID/OSM_CLIENT_SECRET are not set")
    else:
        print("osm app: client_id set, base", cfg.osm_base)
    for user in state.osm_users():
        try:
            print(f"osm (user {user['user_id']}): {user['osm_name'] or '(name unknown)'} | token works:",
                  bool(Osm(cfg.osm_base, user["osm_access_token"]).user_name()))
        except OsmError as e:
            print(f"osm (user {user['user_id']}): token error:", redact(e))


def _check(cfg: Config, tg: Telegram, state: State) -> None:
    try:
        me = tg.whoami()
        print("bot:", me.get("username"))
    except Exception as e:
        print("bot: error:", redact(e))
    if cfg.operator_chat:
        try:
            chat = tg.chat_info(cfg.operator_chat)
            print("operator chat:", chat.get("title") or chat.get("first_name"), "| type:", chat.get("type"))
        except Exception as e:
            print("operator chat: error:", redact(e))
    else:
        print("operator chat: TELEGRAM_CHAT_ID is not set (service warnings disabled)")
    try:
        url = tg.webhook_info().get("url") or ""
        print("webhook:", url or "(none)")
        if url:
            print("warning: a webhook is set; getUpdates will not receive updates until it is removed")
    except Exception as e:
        print("webhook: error:", redact(e))
    print("users:", state.counts_users())
    _check_garmin(cfg, state)
    _check_osm(cfg, state)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="tg_garmin_to_osm")
    ap.add_argument("cmd", choices=["run", "once", "check", "backfill"])
    ap.add_argument("--since", default="2026-08-22", help="backfill window start, YYYY-MM-DD UTC")
    ap.add_argument("--dry-run", action="store_true", help="backfill: list what would happen, change nothing")
    ap.add_argument("--user", type=int, default=None, help="backfill: one telegram user id instead of all")
    args = ap.parse_args()
    cmd = args.cmd
    cfg = Config.from_env()
    tg = Telegram(cfg.token, cfg.api_base)
    try:
        state = State(cfg.data_dir)
        if cmd == "check":
            _check(cfg, tg, state)
            return
        if cmd == "once":
            handle_updates(cfg, tg, state, wait=0)
            print("cycle:", cycle(cfg, tg, state))
            return
        if cmd == "backfill":
            run_backfill(cfg, tg, state, args.since, args.dry_run, args.user)
            return
        if cfg.ingest_token:
            serve_ingest(cfg.ingest_port, cfg.ingest_token,
                         lambda uid, tokens: ingest_garmin_link(cfg, tg, state, uid, tokens),
                         lambda uid, token, name: ingest_osm_link(cfg, tg, state, uid, token, name))
            log.info("ingest API listening on :%s", cfg.ingest_port)
        run_forever(cfg, tg, state)
    except Exception as e:  # no raw traceback: exception text can contain a token or a password
        log.error("fatal: %s", redact(e))
        sys.exit(1)


if __name__ == "__main__":
    sys.exit(main())
