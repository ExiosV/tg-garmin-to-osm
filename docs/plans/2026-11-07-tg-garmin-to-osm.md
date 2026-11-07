# tg-garmin-to-osm: велотреки Garmin → OSM под управлением через Telegram

- **Статус:** реализован v1 (2026-11-07), до деплоя.
- **Дата:** 2026-11-07.
- **Область:** репо `tg-garmin-to-osm`, манифесты `exiosv-lab-infra/k8s/tg-garmin-to-osm/`.
- **Связано:** `tg-garmin-livetrack` (шаблон архитектуры), `health-sync` (паттерны Garmin-auth, `RIDE_TYPES`), `tg-botkit` (транспорт Telegram).

## 1. Задача

Владелец катается на веле, треки пишутся в Garmin/Strava и вручную загружаются в OSM, чтобы по свежему треку проверять/обновлять карту (дороги, покрытия). Автоматизировать загрузку: сервис-сиблинг при появлении в Garmin новой велопоездки сам загружает GPX в OSM и присылает в Telegram ссылку на трек и ссылку «править карту в iD по треку». Пока один пользователь; схема — с заделом на мультиюзера.

Ограничения и допущения: один Garmin-аккаунт и один OSM-аккаунт; сервис ходит в Garmin напрямую (файлы треков health-sync не отдаёт); всё конфигурируется env/командами, приватность — решение владельца.

## 2. Короткий вывод (TL;DR)

1. Новый репо `tg-garmin-to-osm` по шаблону `tg-garmin-livetrack`: src-layout, `python -m tg_garmin_to_osm run|once|check`, deps через tarball-теги (`tg-botkit` v0.3.0, `garminconnect==0.3.20`), SQLite на PVC, Dockerfile 7 строк, образ `exiosv/tg-garmin-to-osm:k8s` через reusable `k8s-deploy-app.yml`.
2. Garmin: `garminconnect` с собственным токен-стором на своём PVC (`GARMINTOKENS=/data/garmin`, не делить с health-sync); паттерны auth из `health_sync/connectors/garmin.py` (MFA-файл, ticket-login, reset при auth-ошибке). Поллинг лёгкий: страница 10 активностей раз в 10 мин.
3. OSM: API 0.6. Загрузка `POST /api/0.6/gpx` (multipart, `description` обязателен, ответ — id трека); OAuth 2 confidential-приложение, скоупы `read_prefs read_gpx write_gpx`, вход прямо в чате через redirect `urn:ietf:wg:oauth:2.0:oob` (код копируется из браузера). Статус обработки — `GET /api/0.6/gpx/<id>.json` (`pending` → готово), после чего бот присылает edit-ссылку `openstreetmap.org/edit?editor=id&gpx=<id>#map=17/<lat>/<lon>` (iD открывает трек слоем).
4. Приватность: с 2026-08 API принимает только `identifiable`/`trackable` (`public`/`private` — ошибка). По умолчанию `identifiable` (максимум пользы для OSM), переключается `/visibility` и env `VISIBILITY`; `UPLOAD_DELAY_MINUTES` даёт окно на `/skip`. Владельцу сообщено: трек в OSM никогда не полностью приватен, старт/финиш обычно виден.
5. Автозагрузка — только велотипы (`UPLOAD_TYPES` = `RIDE_TYPES` из health-sync); остальное хранится как `ignored` и грузится по `/upload <id>`. Первый скан после деплоя помечает весь бэклог `backlog` — массовой загрузки истории нет.
6. Отказоустойчивость: mark-before-notify (статус в БД до отправки в Telegram → дубликатов загрузок нет); Garmin-ошибки и OSM 5xx/429 не тратят попытки, постоянные OSM 4xx после 3 попыток → `failed` + уведомление; OSM 401 → просит `/login`.
7. Telegram: команды только из чата владельца (`TELEGRAM_CHAT_ID`), чужие чаты молча игнорируются: `/login`, `/status`, `/sync`, `/pending`, `/skip`, `/upload`, `/visibility`, `/help`.

## 3. Почему не альтернативы

- **Детект новых поездок через `GET /activities` health-sync** (есть кэш и уже скейлится на livetrack) — эндпоинт отдаёт только сводные JSON, файлов треков нет и лимит 30; всё равно нужен свой garminconnect-клиент для скачивания. Вывод: не использовать, сканируем Garmin сами, но лёгким поллингом.
- **OAuth 1.0a OSM** — deprecated, платформа перешла на OAuth 2. Вывод: не использовать.
- **Загрузка FIT/оригинала** — OSM принимает GPX (трекпойнты с таймстампами); FIT — zip, требовал бы конверсии. Вывод: GPX напрямую из garminconnect, с валидацией `<trkpt>`/`<time>` перед отправкой.
- **Видимость `public`/`private`** — deprecated для API-загрузок (2026-08), сервер вернёт ошибку. Вывод: только `identifiable`/`trackable`, по умолчанию `identifiable`, меняется командой.
- **PKCE/public client** — можно, но сервис — конфиденциальный клиент с секретом в Bitwarden; PKCE для oob-потока всё равно не проверить сервером. Вывод: confidential + oob, state-таблица не нужна (в oob браузер не возвращает state).
- **Общий PVC/токен-стор с health-sync** — RWO, параллельные логины одним стором не тестировались, гонки при refresh. Вывод: свой PVC и свой `GARMINTOKENS`.
- **Webhook Telegram** — дому уже выбран long-polling через локальный Bot API server (`tg-botkit`), webhook требует Ingress и ломает несколько ботов на один токен. Вывод: getUpdates.

## 4. Решения и фазы

1. **Скелет и клиенты** — `config.py` (env-only, толерантные парсеры, секреты регистрируются в `redact`), `state.py` (SQLite: `users`, `activities(user_id, activity_id, ..., status, trace_id)`, `meta`; bootstrap fail-fast), `osm.py` (token exchange, upload, trace meta, edit/trace links), `garmin.py` (singleton с блокировкой, MFA-файл, ticket-login, `download_gpx`), `gpx.py` (валидация, description).
2. **Runner и Telegram** — `runner.py`: `scan_activities` (backlog/new/ignored) → `upload_ready` (окно задержки) → `check_pending` (pending→done, edit-ссылка); команды; `run_forever` (getUpdates wait=25 + интервалы). `telegram.py` — транспорт над `tg_botkit.Bot`.
3. **Упаковка** — Dockerfile, `.env.example`, README (операционный контракт), `deploy.yml`, манифесты k8s + `secrets-spec/tg-garmin-to-osm.spec` (все ключи `:ext`).
4. **Ввод в строй (владелец)** — создать GitHub-репо, бот у BotFather, OAuth-приложение на osm.org, ключи в Bitwarden, `gen-externalsecrets.sh`, деплой, `/login`, `/sync`.

## 5. Обновления 2026-11-07 (v2, живой ввод в строй)

1. **Двуязычие**: сообщения ru/en; русский — только для «RF-связанных» пользователей (правило владельца). Бот не видит телефон/страну (телефон только через явно переданный контакт), поэтому прокси — язык клиента Telegram (`language_code`), контакт с номером +7 пинует русский, `/lang` — ручное перекрытие; по умолчанию английский.
2. **Курс Garmin в описании трека**: `metadataDTO.associatedCourseId` из деталей активности; имя берётся из списка СВОИХ курсов (`get_courses`); чужой/удалённый id (проверено живьём: `get_course` → 404) — суффикс не добавляется.
3. **Бэкфилл** (`backfill [--since] [--dry-run]`): страница истории 100 активностей, дедупликация по дате (±1 день) против списка существующих треков пользователя (`GET /api/0.6/user/gpx_files`) — вручную загруженные ранее поездки не дублируются; пауза 4 с между загрузками; запуск Job-ом при Deployment в 0 реплик (общий RWO PVC).
4. **URL треков OSM (2026)**: страницы треков живут на `/user/<name>/traces/<id>`, старый `/traces/<id>` — 404 .
5. **bw-push.sh**: текущий `bws` требует `BWS_ACCESS_TOKEN` (экспорт из `BW_ACCESS_TOKEN`), без фиксa — «Missing access token».
6. **Garmin-пароль в Bitwarden протух** (401 от Garmin); работает копия токен-файла health-sync (тот же формат garminconnect 0.3.20). MFA/ticket-процедуры — README + `scripts/garmin-first-login.sh`.

## 6. Известные ограничения

- Трейк, который OSM отвергает уже после успешного upload-вызова, остаётся `uploaded` навсегда (в `/pending` виден, удаляется вручную на osm.org).
- Один Garmin/OSM аккаунт; мультиюзер — будущая фаза (`user_id` в ключе уже есть, вход в чате уже готов).
- Новые typeKey Garmin (например, новые e-bike-типы) попадают в `ignored`, пока не добавлены в `UPLOAD_TYPES`.
