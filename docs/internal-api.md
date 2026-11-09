# Internal Account API (garmin-to-osm-web -> tg-garmin-to-osm)

Cluster-internal JSON API served by the bot process on `INGEST_PORT` (default 8080).
The web site (garmin-osm.exiosv.com) drives sign-up and all settings through this API.
The bot stays the single writer of its SQLite database and PVC.

## Transport

- Same server as the existing ingest endpoints (`/ingest/garmin`, `/ingest/osm`).
- Every route except `GET /ping` requires `Authorization: Bearer <INGEST_TOKEN>`.
- JSON request/response. Errors: `{"error": "<code>"}` with HTTP:
  - 400 bad input
  - 401 unauthorized
  - 403 forbidden (e.g. `signup_closed`)
  - 404 not found
  - 409 conflict (e.g. `osm_taken`, `not_ready`)
  - 503 busy (work lock timeout)
  - 500 internal
- Never log or return tokens/keys.

## Identity Model

- **Telegram users**: `user_id = tg_uid` (positive), `chat_id = tg_uid`, `channel = 'telegram'`.
- **Web users**: `user_id = -osm_uid` (negative), `chat_id = NULL`, `channel = 'web'`.
- `osm_uid` column with unique partial index (`WHERE osm_uid IS NOT NULL`).
- Sign-in by OSM finds existing user by `osm_uid` first (Telegram user linking same OSM account sees their existing account).
- `MAX_WEB_USERS` env (default 100) caps `channel='web'` creation only.

## Routes

### Account Creation

```
POST /api/accounts/osm
  {"osm_uid": int, "osm_name": str, "access_token": str, "lang": "en"|"ru"}
  200 {"uid": int, "created": bool}     # created=false if existing user updated
  403 {"error": "signup_closed"}

POST /api/accounts/telegram
  {"tg_uid": int, "lang": "en"|"ru"}
  200 {"uid": int}                      # ensures row exists, chat_id = tg_uid
```

### User State

```
GET /api/users/{uid}
  200 {
    "uid": int,
    "channel": "web"|"telegram",
    "lang": "en"|"ru",
    "osm":    {"linked": bool, "name": str|null},
    "garmin": {"linked": bool, "name": str|null, "failing": bool},
    "settings": {"auto": bool, "types": str, "visibility": "identifiable"|"trackable"},
    "type_options": [str, ...],
    "delay_minutes": int,
    "counts": {"<status>": int, ...},
    "activities": [                      # newest first, max 30
      {"activity_id": str, "name": str, "type": str, "start_ts": int,
       "distance_km": float|null, "status": "backlog"|"new"|"ignored"|"skipped"|"uploaded"|"done"|"failed",
       "trace_id": int|null, "trace_url": str|null, "edit_url": str|null,
       "last_error": str|null, "actions": ["upload"|"skip", ...]}
    ],
    "events": [                           # newest first, max 30
      {"ts": int, "kind": str, "data": {...}}
    ]
  }
  404 {"error": "not_found"}
```

### Garmin Key

```
POST /api/users/{uid}/garmin-key
  {"key": str}
  200 {"ok": true, "garmin_name": str|null}
  200 {"ok": false, "error": "bad_key"|"garmin_unavailable"}
```
Pure verification function `apply_garmin_key_pure` stores key, calls `recent_activities(3)`,
extracts `ownerId`/`ownerDisplayName` from first activity, unlinks on failure.

### Settings

```
POST /api/users/{uid}/settings
  {"auto"?: bool, "types"?: str, "visibility"?: str, "lang"?: "en"|"ru"}
  200 {"ok": true} | 400 {"error": "bad_<field>"}
```

### Sync (one scan+upload+check for this user)

```
POST /api/users/{uid}/sync
  200 {"ok": true, "new": int, "uploaded": int}
  409 {"error": "not_ready"}    # Garmin or OSM not linked
```

### Activity Actions

```
POST /api/users/{uid}/activities/{activity_id}/upload
  200 {"ok": true, "status": str} | 400 | 404

POST /api/users/{uid}/activities/{activity_id}/skip
  200 {"ok": true} | 400 | 404
```
- "upload" offered for statuses: `new`, `backlog`, `ignored`, `skipped`, `failed`.
- "skip" offered for same statuses.
- Unknown activity ID: fetches from Garmin (legacy `GarminClient` for token-file users,
  Personal API client for key users), inserts as `new`, then uploads.

### Unlink

```
POST /api/users/{uid}/unlink
  {"what": "garmin"|"osm"}
  200 {"ok": true}
```

### Delete Account

```
DELETE /api/users/{uid}
  200 {"ok": true}
```
Removes: users row, activities, events, per-user meta keys (`init:<uid>`, `garmin_fail:<uid>`, etc.),
and legacy `GARMINTOKENS/<uid>` directory.

## Events (Per-User Outbox)

Table: `events(id INTEGER PRIMARY KEY, user_id INTEGER, ts INTEGER, kind TEXT, data TEXT)`.

Written at every user notification point; Telegram still sent when `chat_id` is set and token non-empty.
Kept at most 200 per user (oldest dropped).

| kind | data |
|------|------|
| `uploaded` | `activity_id`, `name`, `trace_id`, `trace_url` |
| `processed` | `activity_id`, `name`, `trace_id`, `trace_url`, `edit_url` |
| `upload_failed` | `activity_id`, `name`, `error` |
| `garmin_expired` | `{}` |
| `osm_unauthorized` | `{}` |
| `garmin_linked` | `name` |
| `osm_linked` | `name` |

Unknown kinds render as neutral line on web.

## Concurrency & Locking

- **State thread-safety**: Single SQLite connection with `check_same_thread=False`, guarded by internal `threading.RLock` on every method.
- **Work lock**: module-level `runner.WORK_LOCK` (`threading.RLock`), shared by the main loop,
  Telegram command handling and the API. Held during:
  - `cycle()` (scan all users) and the trace checks
  - the processing of each Telegram update (one lock per update)
  - **never** during the `getUpdates` long poll.
- When the lock is busy, the loop does not move its scan/check timers: the work runs on the next pass.
- **API handlers** acquire work lock with `acquire(timeout=20)` for mutating routes; return 503 `{"error": "busy"}` on timeout.
- Read-only routes (`GET /api/users/{uid}`) do not acquire the work lock.

## Backfill & Migration

- `osm_uid` is stored on every OSM link (web, `/ingest/osm` with `osm_uid`, bot `/login` via
  `user/details.json`). `save_osm_user` keeps the old `osm_uid` and `chat_id` when the new value is NULL.
- Backfill: each cycle fills `osm_uid` for users with a token and no `osm_uid`; errors are skipped
  quietly (retried next cycle); an id that another row already has is logged and skipped.
- `garmin.failing` is true while meta `garmin_fail:<uid>` is set; a successful scan or a verified new
  key clears it.
- Activity `actions`: `upload` for new/backlog/ignored/skipped/failed; `skip` for new/failed only.
- `edit_url` (only for `done`): from the newest `processed` event, else `{base}/edit?editor=id&gpx=<id>`.
- `/api/accounts/osm` for an existing user updates the token and name only (the language stays).
- Migration via existing `_migrate_users` pattern: additive `ALTER TABLE ADD COLUMN` only, never drop/rewrite.
- New columns: `channel TEXT DEFAULT 'telegram'`, `osm_uid INTEGER`, `garmin_owner_id TEXT`, `garmin_name TEXT`.
- New table: `events` with index `idx_events_user_ts`.
- Unique partial index on `osm_uid`.

## Running Without Telegram

- `TELEGRAM_BOT_TOKEN` empty → bot runs without Telegram entirely.
- `getUpdates` errors are caught in their own `try` (logged redacted, 5 s back-off); the Garmin/OSM
  work of the same loop pass still runs.
- Operator alerts (10 consecutive failures) are skipped when Telegram is off.
- `tg` may be `None` everywhere; `_notify` always writes the event and sends Telegram only when
  `tg`, a message key and the user's `chat_id` are set. API calls pass the bot's `tg`, so a Telegram
  user who acts on the web still gets the Telegram message.