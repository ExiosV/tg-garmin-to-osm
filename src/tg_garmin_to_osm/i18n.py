"""Two-language message catalog. Telegram bots cannot see the user's phone or country (a phone
number arrives only via an explicitly shared contact), so the client UI language (`language_code`)
is the proxy per the owner's rule: Russian only for RF-linked users (language_code "ru*" or a +7
contact), everyone else gets English. /lang pins an override."""
import logging

log = logging.getLogger("tg_garmin_to_osm")

RU, EN = "ru", "en"

MESSAGES = {
    RU: {
        "start": (
            "🚴 <b>Garmin → OpenStreetMap</b>\n"
            "Бот связывает ваш Garmin Connect с аккаунтом OSM и после каждой поездки сам загружает "
            "трек на OSM как GPS-трек — а затем присылает ссылку на трек и ссылку «править карту в "
            "iD по треку», чтобы вы могли обновлять дороги и покрытия там, где проехали.\n\n"
            "<b>Как начать</b>\n"
            "1. /garmin — связать Garmin (понадобятся логин и пароль Garmin; если включена 2FA — код "
            "пришлёте сюда же. Сообщения с паролем удаляются сразу, хранятся только токены)\n"
            "2. /login — связать OSM (откройте ссылку, войдите, пришлите код со страницы)\n"
            "3. Дальше всё автоматом: /status — состояние, /sync — загрузить сейчас\n\n"
            "<b>Настройки</b>\n"
            "/types — какие активности загружать (по умолчанию только вело)\n"
            "/auto on|off — включить/выключить автозагрузку\n"
            "/visibility identifiable|trackable — видимость треков на OSM\n"
            "/lang ru|en — язык сообщений\n\n"
            "<b>Приватность</b>\n"
            "Треки на OSM публичны — в этом смысл: они улучшают карту. По умолчанию "
            "identifiable (трек виден под вашим ником, с датами). Полностью скрытый трек невозможен: "
            "координаты любого трека доступны через API OSM. Чувствительную поездку можно /skip "
            "сразу после её окончания или вообще не включать автозагрузку (/auto off)."
        ),
        "help": (
            "Garmin → OSM: автозагрузка велотреков (и не только) на карту.\n"
            "/garmin — связать Garmin (email → пароль → код 2FA, всё в чате)\n"
            "/login — связать OSM (ссылка → код из браузера)\n"
            "/status — состояние и настройки\n"
            "/sync — сканировать Garmin и загрузить сейчас\n"
            "/pending — треки в обработке и ошибки\n"
            "/skip <id> — не загружать поездку\n"
            "/upload <id> — загрузить поездку вручную\n"
            "/types [список] — какие активности загружать: группы bike run walk water winter fitness "
            "или ключи Garmin\n"
            "/auto on|off — автозагрузка\n"
            "/visibility [identifiable|trackable] — видимость новых треков\n"
            "/unlink garmin|osm — отвязать аккаунт\n"
            "/lang [ru|en] — язык сообщений\n"
            "/cancel — прервать текущий диалог (привязку)\n"
            "/help — это сообщение"
        ),
        "login": "1. Откройте ссылку и авторизуйтесь:\n{url}\n"
                 "2. Скопируйте код со страницы и пришлите его следующим сообщением.",
        "login_no_client": "OSM-приложение не настроено на сервере.",
        "login_done": "✅ OSM связан: <b>{name}</b>. Теперь /sync или просто катайте.",
        "login_fail": "⚠️ Обмен кода не удался: {err}. /login — попробовать заново.",
        "garmin_ask_email": "Пришлите email вашего аккаунта Garmin Connect (следующим сообщением). "
                            "/cancel — отменить.",
        "garmin_ask_password": "Теперь пришлите пароль Garmin. Сообщение будет удалено сразу, пароль "
                               "нигде не сохраняется (только токены). /cancel — отменить.",
        "garmin_ask_mfa": "Garmin запросил код 2FA — пришлите его следующим сообщением. /cancel — отменить.",
        "garmin_linked_ok": "✅ Garmin связан. Токены сохранены; пароль больше не нужен. "
                            "Остался /login для OSM — и всё поедет.",
        "garmin_fail": "⚠️ Garmin не принял вход (неверный email/пароль, блокировка или временная "
                       "ошибка). Попробуйте ещё раз: /garmin.",
        "garmin_mfa_fail": "⚠️ Код 2FA не принят. /garmin — начать заново.",
        "garmin_timeout": "⏳ Диалог привязки прерван по таймауту. /garmin — начать заново.",
        "cancel_done": "Ок, прервано.",
        "status": "🚴 tg-garmin-to-osm\n"
                  "Garmin: {garmin} · OSM: {osm}\n"
                  "Автозагрузка: {auto} · типы: {types}\n"
                  "Видимость: {vis} · язык: {lang}\n"
                  "Активности: {counts}\n"
                  "Скан Garmin: {scan} (каждые {poll} с)",
        "sync_running": "🔄 Сканирую Garmin...",
        "sync_done": "Готово: новых {seen}, загружено {uploaded}, обработано {processed}.",
        "sync_no_garmin": "Сначала свяжите Garmin: /garmin.",
        "sync_no_osm": "Сначала свяжите OSM: /login.",
        "pending_empty": "Нет треков в обработке и ошибок.",
        "pending_link": " · <a href=\"{url}\">трек {id}</a>",
        "pending_err": "\n  ошибка: {err}",
        "skip_missing": "Активность {id} не найдена (она должна попасть в скан).",
        "skip_uploaded": "Уже загружена в OSM — трек можно удалить на странице трека.",
        "skip_done": "⏭ {id} пропущена — загружена не будет.",
        "upload_already": "Уже загружена: <a href=\"{url}\">трек {id}</a>",
        "upload_fail_cmd": "Загрузить не удалось — подробности в журнале.",
        "visibility_set": "Видимость новых треков: {vis}.",
        "visibility_show": "Текущая видимость: {vis}. Варианты: {opts}.",
        "auto_set": "Автозагрузка: {auto}.",
        "auto_show": "Автозагрузка: {auto} (/auto on|off).",
        "types_show": "Группы: {groups}.\nЗагружаются: {current}.\n"
                      "Настройка: /types bike,walk (или ключи Garmin, например swimming).",
        "types_set": "Буду загружать: {types}.",
        "lang_set": "Язык сообщений: {lang}.",
        "lang_show": "Язык сообщений: {lang} (/lang ru|en).",
        "unlink_done": "✅ {what} отвязан(а).",
        "unlink_usage": "Уточните: /unlink garmin или /unlink osm.",
        "unknown": "Не понял команду. /help — список команд.",
        "uploaded": "✅ <b>Трек загружен в OSM</b>\n"
                    "{desc}\n"
                    "🗺 <a href=\"{url}\">Трек {id}</a> · видимость: {vis}\n"
                    "⏳ OSM обрабатывает трек — пришлю ссылку на правку карты.",
        "processed": "🗺 <b>Трек обработан OSM</b> — можно править карту:\n"
                     "✏️ <a href=\"{url}\">Правка в iD по треку {id}</a>",
        "osm_401": "🔑 OSM отклонил токен (401). Отправьте /login заново.",
        "upload_failed_final": "❌ Не смог загрузить «{name}» в OSM ({n} попыток): {err}",
        "garmin_expired": "⚠️ Сессия Garmin истекла. Свяжите заново: /garmin.",
        "cycle_warn": "⚠️ tg-garmin-to-osm: 10 ошибок подряд, последняя: {err}",
        "err_reply": "⚠️ Ошибка: {err}",
        "backfill_summary": "📦 Бэкфилл с {since}: загружено {uploaded}, "
                            "уже были на OSM (пропущены) {skipped}, ошибок {failed}.",
        "backfill_no_login": "Нет OSM-логина — бэкфилл невозможен.",
        "backfill_no_garmin": "Нет Garmin-логина — бэкфилл невозможен.",
    },
    EN: {
        "start": (
            "🚴 <b>Garmin → OpenStreetMap</b>\n"
            "This bot links your Garmin Connect with your OSM account and after every ride uploads "
            "the track to OSM as a GPS trace — then sends you a link to the trace and an "
            "\"edit the map in iD against this trace\" link, so you can improve roads and surfaces "
            "where you actually rode.\n\n"
            "<b>Getting started</b>\n"
            "1. /garmin — link Garmin (you will send your Garmin email and password here; if 2FA is "
            "on you will also send the code. The messages are deleted immediately, only tokens are "
            "stored)\n"
            "2. /login — link OSM (open the link, sign in, paste the code from the page)\n"
            "3. Then it just works: /status for state, /sync to upload now\n\n"
            "<b>Settings</b>\n"
            "/types — which activities to upload (bike rides only by default)\n"
            "/auto on|off — turn auto-upload on/off\n"
            "/visibility identifiable|trackable — how traces appear on OSM\n"
            "/lang ru|en — message language\n\n"
            "<b>Privacy</b>\n"
            "Traces on OSM are public — that is the point: they improve the map. The default is "
            "identifiable (the trace shows under your OSM name, with timestamps). A fully private "
            "trace is impossible: coordinates of any trace are readable via the OSM API. For a "
            "sensitive ride use /skip right after it ends, or keep auto-upload off (/auto off)."
        ),
        "help": (
            "Garmin → OSM: auto-uploads your rides (and more) to the map.\n"
            "/garmin — link Garmin (email → password → 2FA code, all in this chat)\n"
            "/login — link OSM (link → code from the browser)\n"
            "/status — state and settings\n"
            "/sync — scan Garmin and upload now\n"
            "/pending — traces processing and errors\n"
            "/skip <id> — do not upload a ride\n"
            "/upload <id> — upload a ride manually\n"
            "/types [list] — which activities to upload: groups bike run walk water winter fitness "
            "or raw Garmin typeKeys\n"
            "/auto on|off — auto-upload\n"
            "/visibility [identifiable|trackable] — visibility of new traces\n"
            "/unlink garmin|osm — unlink an account\n"
            "/lang [ru|en] — message language\n"
            "/cancel — abort the current dialog (linking)\n"
            "/help — this message"
        ),
        "login": "1. Open the link and authorize:\n{url}\n"
                 "2. Copy the code from the page and send it as your next message.",
        "login_no_client": "The OSM application is not configured on the server.",
        "login_done": "✅ OSM linked: <b>{name}</b>. Now /sync or just go ride.",
        "login_fail": "⚠️ Code exchange failed: {err}. /login to retry.",
        "garmin_ask_email": "Send the email of your Garmin Connect account (as the next message). "
                            "/cancel to abort.",
        "garmin_ask_password": "Now send your Garmin password. The message will be deleted "
                               "immediately and the password is never stored (only tokens). "
                               "/cancel to abort.",
        "garmin_ask_mfa": "Garmin asked for a 2FA code — send it as the next message. /cancel to abort.",
        "garmin_linked_ok": "✅ Garmin linked. Tokens are saved; the password is not needed anymore. "
                            "/login for OSM is the last step.",
        "garmin_fail": "⚠️ Garmin rejected the login (wrong email/password, a block, or a temporary "
                       "error). Try again: /garmin.",
        "garmin_mfa_fail": "⚠️ The 2FA code was not accepted. /garmin to start over.",
        "garmin_timeout": "⏳ The linking dialog timed out. /garmin to start again.",
        "cancel_done": "Done, aborted.",
        "status": "🚴 tg-garmin-to-osm\n"
                  "Garmin: {garmin} · OSM: {osm}\n"
                  "Auto-upload: {auto} · types: {types}\n"
                  "Visibility: {vis} · language: {lang}\n"
                  "Activities: {counts}\n"
                  "Garmin scan: {scan} (every {poll} s)",
        "sync_running": "🔄 Scanning Garmin...",
        "sync_done": "Done: {seen} new, {uploaded} uploaded, {processed} processed.",
        "sync_no_garmin": "Link Garmin first: /garmin.",
        "sync_no_osm": "Link OSM first: /login.",
        "pending_empty": "No traces processing and no failures.",
        "pending_link": " · <a href=\"{url}\">trace {id}</a>",
        "pending_err": "\n  error: {err}",
        "skip_missing": "Activity {id} not found (it must appear in a scan first).",
        "skip_uploaded": "Already uploaded to OSM — the trace can be deleted on its page.",
        "skip_done": "⏭ {id} skipped — it will not be uploaded.",
        "upload_already": "Already uploaded: <a href=\"{url}\">trace {id}</a>",
        "upload_fail_cmd": "Upload failed — details in the log.",
        "visibility_set": "Visibility for new traces: {vis}.",
        "visibility_show": "Current visibility: {vis}. Options: {opts}.",
        "auto_set": "Auto-upload: {auto}.",
        "auto_show": "Auto-upload: {auto} (/auto on|off).",
        "types_show": "Groups: {groups}.\nUploading: {current}.\n"
                      "Set with /types bike,walk (or raw Garmin keys, e.g. swimming).",
        "types_set": "Will upload: {types}.",
        "lang_set": "Message language: {lang}.",
        "lang_show": "Message language: {lang} (/lang ru|en).",
        "unlink_done": "✅ {what} unlinked.",
        "unlink_usage": "Specify: /unlink garmin or /unlink osm.",
        "unknown": "Unknown command. /help lists them.",
        "uploaded": "✅ <b>Trace uploaded to OSM</b>\n"
                    "{desc}\n"
                    "🗺 <a href=\"{url}\">Trace {id}</a> · visibility: {vis}\n"
                    "⏳ OSM is processing the trace — I will send the map-edit link.",
        "processed": "🗺 <b>Trace processed by OSM</b> — the map can be edited:\n"
                     "✏️ <a href=\"{url}\">Edit in iD against trace {id}</a>",
        "osm_401": "🔑 OSM rejected the token (401). Send /login again.",
        "upload_failed_final": "❌ Failed to upload \"{name}\" to OSM ({n} attempts): {err}",
        "garmin_expired": "⚠️ The Garmin session expired. Link again: /garmin.",
        "cycle_warn": "⚠️ tg-garmin-to-osm: 10 failures in a row, last: {err}",
        "err_reply": "⚠️ Error: {err}",
        "backfill_summary": "📦 Backfill since {since}: uploaded {uploaded}, "
                            "already on OSM (skipped) {skipped}, failed {failed}.",
        "backfill_no_login": "No OSM login — backfill impossible.",
        "backfill_no_garmin": "No Garmin login — backfill impossible.",
    },
}

PENDING_TTL = 15 * 60  # a linking dialog older than this is dropped


def normalize(code) -> str:
    """Client UI language -> message language; Russian only for "ru*" clients, English otherwise."""
    return RU if str(code or "").lower().startswith("ru") else EN


def phone_lang(phone) -> str | None:
    """A shared contact with an RF-prefix number (+7) pins Russian; other phones decide nothing."""
    p = str(phone or "").lstrip("+").strip()
    return RU if p.startswith("7") else None


def text(state_user_lang: str, key: str, code=None, **kw) -> str:
    """Format one message: the user's stored language wins, then this message's language_code,
    then English."""
    lang = state_user_lang if state_user_lang in (RU, EN) else normalize(code)
    tpl = MESSAGES.get(lang, MESSAGES[EN]).get(key) or MESSAGES[EN].get(key, key)
    return tpl.format(**kw) if kw else tpl
