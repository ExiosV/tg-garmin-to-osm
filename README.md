# tg-garmin-to-osm

A Telegram bot that links your Garmin Connect account with your OpenStreetMap account and, after
every ride (or any activity you enable), uploads the track to OSM as a GPS trace — then sends you a
link to the trace and an **“edit the map in iD against this trace”** link, so you can improve roads,
tracks and surfaces where you actually rode.

Talk to the bot: **[@garmin_to_osm_bot](https://t.me/garmin_to_osm_bot)**.
Linking site: **https://garmin-osm.exiosv.com** (repo: `garmin-to-osm-web`).

```
pip install -e .[dev] && pytest
python -m tg_garmin_to_osm check    # bot + linked accounts diagnostics
python -m tg_garmin_to_osm once     # a single cycle (updates + scan + trace check)
python -m tg_garmin_to_osm run      # the loop
python -m tg_garmin_to_osm backfill --since 2026-08-22 [--dry-run]  # one-off history upload
```
Config: see `.env.example`.

## Getting started

1. **/link** in the bot chat — open **https://garmin-osm.exiosv.com** and sign in with Telegram
   (Login Widget; this is how the site knows which chat the accounts belong to).
2. **Link Garmin** on the site — enter your Garmin Connect email and password there (over HTTPS;
   the password is used once and never stored, only the resulting session tokens; a 2FA code page
   follows if Garmin asks). Why a site and not the chat: Garmin has no public OAuth for third-party
   apps (official access needs a partner account), so the handshake runs on our HTTPS backend and
   the password never passes through Telegram.
3. **Link OSM** on the site — a real OAuth 2 redirect flow: you confirm access on
   openstreetmap.org itself, the site receives the token automatically. Only
   `read_prefs read_gpx write_gpx` are requested.
4. That's it — new rides upload automatically and you get the edit link once OSM processes the
   trace. Fallback: `/login` pastes an OSM out-of-band code right in the chat.

All commands work for any user in a private chat:

| Command | What it does |
|---|---|
| `/status` | your links, settings, activity counters |
| `/sync` | scan Garmin and upload now |
| `/pending` | traces waiting for OSM processing, and failures |
| `/skip <id>` | do not upload a ride (useful right after it ends) |
| `/upload <id>` | upload a ride manually, even an old or non-auto one |
| `/types` | which activities auto-upload: groups `bike run walk water winter fitness` or raw Garmin typeKeys (`bike,walk`, `swimming`, …). Default: `bike` |
| `/auto on\|off` | turn auto-upload on/off |
| `/visibility identifiable\|trackable` | how new traces appear on OSM (default `identifiable`) |
| `/unlink garmin\|osm` | remove a link |
| `/lang ru\|en` | message language |
| `/cancel` | abort a linking dialog |

## How it behaves

- **Scan**: one light page (newest 10 activities) per user every `GARMIN_POLL_SECONDS` (600) — easy
  on Garmin. Activities matching the user's types become upload candidates; the very first scan
  after linking marks the existing history as backlog (nothing old uploads en masse — use
  `/upload <id>` or the operator `backfill` command).
- **Upload**: the activity GPX is downloaded from Garmin and validated (must contain `<trkpt>`
  with `<time>` — a trace without timestamps cannot confirm the survey date), then
  `POST /api/0.6/gpx` (OSM API 0.6). The response trace id is stored with the row. Trace
  description: `<name> (YYYY-MM-DD, 42.5 km)` plus `, по курсу «Name»` / `, course "Name"` when the
  ride followed one of the user's own Garmin courses.
- **Processing**: OSM processes traces asynchronously; the bot polls `GET /api/0.6/gpx/<id>` and
  announces `https://www.openstreetmap.org/edit?editor=id&gpx=<id>#map=17/<lat>/<lon>` — iD opens
  with the track as a pink GPX layer. Trace pages live at `/user/<name>/traces/<id>` (the 2026 URL
  shape).
- **Failure model**: mark-before-notify (the DB row is set before the Telegram message — no
  duplicate uploads). Garmin-side errors and OSM 5xx/429 do not burn attempts; permanent OSM 4xx
  after 3 attempts mark the row `failed` and notify. OSM 401 → re-link with `/login`; a dead Garmin
  session → `/garmin` again (notified once per day).
- **Languages**: Russian only for RF-linked users (per the operator's rule) — Telegram bots cannot
  see phone/country, so the client UI language (`language_code`) is the proxy; a shared contact with
  a +7 number pins Russian; `/lang` overrides; everyone else gets English.
- **Backfill** (operator, one-off): `backfill --since YYYY-MM-DD [--dry-run] [--user <tg_id>]`
  pages deep history, then uploads every candidate ride whose date is **not** within ±1 day of an
  existing trace in the user's OSM trace list (`GET /api/0.6/user/gpx_files`) — manually uploaded
  rides are not duplicated. 4 s pause between uploads.

## Privacy

- Traces on OSM are public — that is the point: they improve the map. `identifiable` (the default)
  shows the trace under your OSM name with timestamps; `trackable` hides the user linkage in the
  API but timestamps stay readable. **No trace is ever fully private**: coordinates of any trace are
  readable via the OSM API. Skip sensitive rides with `/skip` (there is an `UPLOAD_DELAY_MINUTES`
  window between noticing and uploading) or keep `/auto off`.
- The bot stores: your Telegram id/chat, the OSM token, the Garmin token file, your settings, and
  basic activity bookkeeping. It never stores your Garmin password (deleted from the chat at once).
- The trace description and tags (`TRACE_TAGS`) are public on the trace page.

## Self-hosting

- Python ≥ 3.12; deps: `tg-botkit` (pinned tarball), `garminconnect==0.3.20`, `requests`.
- State: SQLite (`DATA_DIR/state.db`) + per-user Garmin token directories
  (`GARMINTOKENS/<telegram_uid>/garmin_tokens.json`) — put both on a persistent volume.
- Register an OSM OAuth 2 application (confidential, redirect `urn:ietf:wg:oauth:2.0:oob`, scopes
  `read_prefs read_gpx write_gpx`) and fill `OSM_CLIENT_ID`/`OSM_CLIENT_SECRET`.
- `TELEGRAM_CHAT_ID` is the **operator** chat: service-level warnings ("10 failures in a row") go
  there; users interact with the bot in their own private chats.
- Optional env: `DEFAULT_TYPES` (default `bike`), `VISIBILITY`, `UPLOAD_DELAY_MINUTES`,
  `SCAN_PAGE`, `TRACE_TAGS`, `GARMIN_POLL_SECONDS`, `TRACE_CHECK_SECONDS`. `GARMIN_EMAIL`/
  `GARMIN_PASSWORD` are a legacy single-account bootstrap and are not needed for the chat linking
  flow.
- Docker: `python:3.12-slim`, `ENTRYPOINT ["python", "-m", "tg_garmin_to_osm"]`, `CMD ["run"]`.

## Known limits

- One Garmin account and one OSM account per Telegram user; a shared cluster deployment serves
  everyone (per-user token stores and settings, isolated activity rows).
- A trace that OSM rejects asynchronously stays `uploaded` forever (visible in `/pending`).
- Foreign/deleted Garmin course ids cannot be resolved to a name — the course suffix is simply
  omitted.
- The bot must own `getUpdates` (no webhook, no other consumer for the token).

## Disclaimer

Not affiliated with Garmin or OpenStreetMap. Uses the unofficial `garminconnect` library — the
Garmin side can break when Garmin changes its API. Licensed under Apache-2.0.
