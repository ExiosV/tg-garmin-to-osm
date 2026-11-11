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
            "1. /link — откроете сайт привязки: войдите через Telegram и привяжите Garmin и OSM там "
            "(в чат ничего вводить не нужно)\n"
            "2. Дальше всё автоматом: /status — состояние, /sync — загрузить сейчас\n\n"
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
            "/link — сайт привязки Garmin и OSM (вход через Telegram)\n"
            "/garminkey КЛЮЧ — привязать Garmin Personal API ключом (без сайта)\n"
            "/login — запасной вход в OSM кодом из браузера\n"
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
        "login": "Запасной способ. 1. Откройте ссылку и авторизуйтесь:\n{url}\n"
                 "2. Скопируйте код со страницы и пришлите его следующим сообщением. "
                 "(Обычно проще: /link)",
        "login_no_client": "OSM-приложение не настроено на сервере.",
        "login_done": "✅ OSM связан: <b>{name}</b>. Теперь /sync или просто катайте.",
        "login_fail": "⚠️ Обмен кода не удался: {err}. /login — попробовать заново.",
        "link_site": "🔗 Откройте сайт привязки и войдите через Telegram:\n{url}\n"
                     "Там же привязываются Garmin и OSM; подтверждения придут сюда.",
        "onb_send_key": "🔑 Пришлите ключ следующим сообщением — оно будет удалено сразу, "
                        "я сохраню только ключ. /cancel — отмена.",
        "garminkey_usage": "🔑 Пришлите команду с вашим Personal API ключом одним сообщением: "
                           "/garminkey ВАШ_КЛЮЧ\n"
                           "Ключ создаётся в Garmin Connect: Settings → Personal API Token "
                           "(https://connect.garmin.com/app/settings/personalApiToken) — выберите "
                           "максимальный срок действия. Сообщение с ключом будет удалено сразу.",
        "garminkey_ok": "✅ Garmin связан через Personal API ({n} последних активностей видны). "
                        "Остался /login для OSM — или он уже сделан на сайте.",
        "garminkey_bad": "⚠️ Garmin не принял ключ (неверный, истёкший или отозванный). "
                         "Сгенерируйте новый: Settings → Personal API Token, и пришлите "
                         "/garminkey НОВЫЙ_КЛЮЧ.",
        "garmin_linked_ok": "✅ Garmin связан (через сайт). Остался /login для OSM — или он тоже "
                            "уже сделан на сайте.",
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
        "backfill_progress": "📦 Бэкфилл: {done} из {total} (загружено {uploaded})...",
        "backfill_stopped": "🛑 Бэкфилл остановлен: загружено {uploaded}. Остальные поездки вернулись "
                            "в очередь — «Залить всё» продолжит с того же места.",
        "backfill_stopping": "🛑 Останавливаю бэкфилл — недогруженные поездки вернутся в очередь.",
        "onb_backfill_started": "📦 Поехали: заливаю всю историю в фоне, прогресс пришлю сюда.",
        "onb_backfill_already": "⏳ Бэкфилл уже идёт — дождитесь завершения.",
    },
    EN: {
        "start": (
            "🚴 <b>Garmin → OpenStreetMap</b>\n"
            "This bot links your Garmin Connect with your OSM account and after every ride uploads "
            "the track to OSM as a GPS trace — then sends you a link to the trace and an "
            "\"edit the map in iD against this trace\" link, so you can improve roads and surfaces "
            "where you actually rode.\n\n"
            "<b>Getting started</b>\n"
            "1. /link — open the linking site: sign in with Telegram there, then link Garmin and "
            "OSM (nothing needs to be typed in this chat)\n"
            "2. Then it just works: /status for state, /sync to upload now\n\n"
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
            "/link — the linking site for Garmin and OSM (Telegram sign-in)\n"
            "/garminkey KEY — link Garmin with a Personal API key (no site needed)\n"
            "/login — fallback: link OSM with a pasted browser code\n"
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
        "login": "Fallback route. 1. Open the link and authorize:\n{url}\n"
                 "2. Copy the code from the page and send it as your next message. "
                 "(Usually easier: /link)",
        "login_no_client": "The OSM application is not configured on the server.",
        "login_done": "✅ OSM linked: <b>{name}</b>. Now /sync or just go ride.",
        "login_fail": "⚠️ Code exchange failed: {err}. /login to retry.",
        "link_site": "🔗 Open the linking site and sign in with Telegram:\n{url}\n"
                     "Link Garmin and OSM there; confirmations will arrive in this chat.",
        "onb_send_key": "🔑 Send the key as your next message — it will be deleted right away, "
                        "I only keep the key itself. /cancel to abort.",
        "garminkey_usage": "🔑 Send your Personal API key in one message: /garminkey YOUR_KEY\n"
                           "Create it in Garmin Connect: Settings → Personal API Token "
                           "(https://connect.garmin.com/app/settings/personalApiToken) — choose the "
                           "longest expiration. The message with the key is deleted right away.",
        "garminkey_ok": "✅ Garmin linked via Personal API ({n} latest activities visible). "
                        "/login for OSM remains — or it is already done on the site.",
        "garminkey_bad": "⚠️ Garmin rejected the key (wrong, expired, or revoked). Generate a new "
                         "one: Settings → Personal API Token, then send /garminkey NEW_KEY.",
        "garmin_linked_ok": "✅ Garmin linked (via the site). /login for OSM remains — or it is "
                            "already done on the site.",
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
        "backfill_progress": "📦 Backfill: {done} of {total} ({uploaded} uploaded)...",
        "backfill_stopped": "🛑 Backfill stopped: {uploaded} uploaded. The rest of the rides are back "
                            "in the queue — 'Upload everything' resumes from there.",
        "backfill_stopping": "🛑 Stopping the backfill — unprocessed rides go back to the backlog.",
        "onb_backfill_started": "📦 Off we go: uploading your full history in the background, "
                                "progress messages follow.",
        "onb_backfill_already": "⏳ A backfill is already running — wait for it to finish.",
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
