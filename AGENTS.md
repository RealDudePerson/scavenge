# OpenCode Instructions for Scavenge

A team-based scavenger hunt site. FastAPI + SQLite + Jinja2 + Foundation CSS, with **AI-powered photo review** (any OpenAI-compatible endpoint).

## Quick Start

- **Run server**: `./run_server.sh` (binds `0.0.0.0:8000`; uses `./venv/bin/uvicorn` with `--reload`)
- **Activate venv manually** for tooling: `./venv/bin/python` / `./venv/bin/pip`
- **Install a new dep**: `./venv/bin/pip install <pkg>`, then add to `package.json` `dependencies` (it tracks deps but isn't actually used as a manifest — `venv/` is gitignored)

## Architecture

- `app/main.py` — all routes, AI submission logic, hunt timer gates, gallery URL building
- `app/models.py` — SQLAlchemy: `Hunt` (with `theme_json` column — JSON of 10 hex colors), `HuntItem` (with `required_properties`/`bonus_properties`/`bonus_hint` JSON strings), `Team`, `Submission` (with AI review fields, attempt counter)
- `app/ai_review.py` — `review_image()` sends JPEG bytes to OpenAI-compatible endpoint, parses structured response
- `app/config.py` — YAML + env config loaders: `get_admin_password`, `get_openai_config`, `get_max_photo_age_hours`, `get_hunt_state`, `set_hunt_ends_at`
- `app/database.py` — async engine + sessionmaker
- `templates/` — Jinja2 templates (all extend `base.html`; theme CSS vars injected from `theme` context)
- `hunts/*.yaml` — hunt definitions (see schema below)
- `teams/teams.yaml` — team passphrases
- `admin_config.yaml` — **gitignored**, contains live secrets (see below)
- `uploads/{originals,thumb,display}/` — gitignored; `originals/` are full-size, `thumb/` 200px, `display/` 1200px

## YAML Schemas

### `hunts/*.yaml`

```yaml
name: "Hunt Name"
description: "..."
theme:                        # optional; 10 hex colors with defaults
  primary: "#FF6B9D"
  # ... 9 more
items:
  - name: "Item Name"
    description: "..."
    points: 100
    bonus_points: 50
    bonus_hint: "Public hint shown to hunters"  # optional
    required_properties:    # AI checks all of these
      - "shows a real X"
    bonus_properties:       # AI checks all of these for bonus
      - "taken during daytime"
```

`required_properties` is **mandatory** — items without it are silently skipped by `load_hunts`. The `theme` block is optional (defaults are spring-palette). `bonus_hint` is optional.

### `teams/teams.yaml`

```yaml
teams:
  - name: "Team Name"
    description: "..."
    passphrase: "shared-secret"   # given to the team to log in
```

## Key Gotchas

- **AI + coordinates** — the AI review is the approval gate; proximity is a separate, additive score (default mode `bonus`). Items may set `target_lat`/`target_lon`, `radius_meters` (default 100), `near_multiplier` (default 2.0), `proximity_points` (default 0), `proximity_mode` (`off|bonus|gate`, default `bonus`), plus `zone_lat`/`zone_lon`/`zone_radius_meters` for the (phase 2) map. GPS is read **server-side from the stored original** via `get_ifd(GPSInfo)` — never from a multipart field. `location_status` (`hit|near|far|missing|no_target|off`) is always persisted; missing GPS still earns AI points and is stated in the `/result` message, never silent. `gate` mode is the only mode where proximity can deny an otherwise-valid photo.
- **The team map never leaks the true target (phase 2 §1)** — `GET /api/map` sends only the display zone (`zone_lat`/`zone_lon`/`zone_radius_meters`) and omits any item with no zone; `target_lat`/`target_lon`/`radius_meters` must never appear in its payload (a player would read them out of the JS and walk to the spot). `GET /admin/map` may include the true target. Enforced by `tests/test_map.py::test_team_map_leaks_no_true_target` (deep scan of every key and numeric value in the serialised payload).
- **Map positions are display-only (phase 2 §2–4)** — `POST /api/position` stores the latest position per team (prior rows pruned on insert, so the table holds ≤1 row/team), is range-validated (NaN/Inf/non-numeric → 400) and rate-limited 60/min/IP, and is **never scored from**. Positions older than 60 s are marked `stale` (`_STALE_THRESHOLD_SECONDS`). Tiles come from `get_tile_url()` (`map_tile_url` in `admin_config.yaml`, `MAP_TILE_URL` env, OSM default); if tiles fail the page shows a non-blocking notice and keeps rendering markers/zones (vendored Leaflet 1.9.4). The admin map *page* is `/admin/map-view`; its API is `/admin/map`.
- **`get_photo_taken_at` reads the Exif sub-IFD** — `DateTimeOriginal` → `DateTimeDigitized` → IFD0 `DateTime`, each with its **own** offset (`OffsetTimeOriginal` 0x9010 / `OffsetTimeDigitized` 0x9011). Pillow's `Exif.get()` only sees IFD0, so the sub-IFD read is mandatory. The result is persisted as `Submission.captured_at`.
- **Team passphrases are hashed** (PBKDF2-HMAC-SHA256, `app/security.py`) — `load_teams` hashes on load and `/team/login` compares with `hmac.compare_digest`; the DB never stores plaintext. `POST /team/login` is rate-limited in-memory per client IP (10 / 5 min).
- **Schema changes require `/admin/reset`** — adding a column to `models.py` doesn't migrate; it requires drop+recreate. Then `/admin/load-hunts` and `/admin/load-teams` to repopulate.
- **Hunt timer is global state in `admin_config.yaml`** — `hunt_ends_at` (ISO timestamp or empty) + `hunt_duration_minutes` (default 160). When empty, hunt is closed and `/submit` rejects. Admin dashboard has Start/Extend (+5min)/Stop buttons.
- **Submission flow gates** (in order in `app/main.py`): team session match → hunt state → empty file → locked check (10 attempts max) → freshness check (EXIF timestamp vs server time) → GPS parse (EXIF only) → AI review → proximity scoring → DB insert. The locked/attempt check is repeated inside the insert transaction because the AI call opens a race window.
- **AI review runs in a threadpool** (`run_in_threadpool`) — it is a blocking network call and must never run directly on the event loop, or one team's review stalls every other request. Same for any sync DB work: query the theme cache, don't hit the DB synchronously per request.
- **Set `SESSION_SECRET` in production** — unset falls back to a random per-process key, so every restart invalidates all sessions.
- **EXIF timestamp is interpreted as UTC** if `OffsetTimeOriginal` (EXIF 2.31, tag 0x9010) is present, else treated as server local time. Photos with no EXIF timestamp are rejected.
- **NGINX upload limit** — if serving behind NGINX, set `client_max_body_size 20M+` in the `http {}` block or you'll get `413 Request Entity Too Large` for phone photos.
- **Two StaticFiles mounts** for uploads: `/uploads/thumb` and `/uploads/display`. Both are re-encoded (no EXIF) and map directly to URL paths — don't merge them into a single mount. `/uploads/originals` is deliberately **not** mounted: originals are the as-uploaded bytes and carry GPS EXIF, so they must stay off HTTP (see `tests/test_uploads_security.py`).
- **`admin_config.yaml` is gitignored but lives on disk with the real OpenAI key** (`OPENAI_API_KEY` env var takes precedence over the YAML key). If you copy this repo to share, scrub the key or rotate it with the provider.
- **Vendored front-end assets** live in `static/vendor/` (Foundation CSS/JS, jQuery) and are served from `/static` so the UI works with no cell signal. Google Fonts still loads from CDN and degrades to `system-ui` offline.

## Admin Workflows

- `/admin/login` — password from `admin_config.yaml` → `admin_password`
- `/admin/load-hunts` — read all `hunts/*.yaml` into DB; skips items missing `required_properties`
- `/admin/load-teams` — read `teams/teams.yaml` into DB
- `/admin/review-submission/{id}` — re-run AI review on one submission (post with no body)
- `/admin/cleanup-orphans` — delete upload files not referenced in `Submission` table
- `/admin/reset` — confirm dialog; drops tables, wipes uploads; **destructive**
- `/admin/hunt/{start,extend,stop}` — control the global hunt timer
- `/admin/hints` — view all bonus hints + (private) AI criteria
- `/admin/all-gallery` — every submission with team label + AI reasoning + Re-review button

## Routing Map

**Public**: `/`, `/admin/login`, `/team/login`, `/api/login` (JSON; needs the right passphrase), `/logout`, `/result`, `/uploads/{thumb,display}/...` (`originals` is deliberately not mounted), `/static` (none)

**Team (auth: `team_id` in session)**: `/hunt/dashboard`, `/map`, `/gallery`, `/leaderboard`

**Admin (auth: `is_admin=True` in session)**: `/admin/dashboard`, `/admin/map-view`, `/admin/hints`, `/admin/all-gallery`, all `POST /admin/*` endpoints

**Team (auth: `team_id` in session)**: `POST /submit` — requires a logged-in team and the posted `team_id` must match the session; gated by hunt state too

**Team JSON (auth: `team_id` in session)**: `GET /api/map` (display zone only — never the true target), `GET /api/items` (same leak rule), `POST /api/submit` (shares `/submit`'s pipeline), `GET /api/gallery`, `GET /api/leaderboard`, `POST /api/position` (display-only live position, rate-limited; JSON **or** form body)

The HTML routes and their JSON twins share one implementation (`_load_items_and_state`,
`_load_gallery_entries`, `_load_leaderboard`, `_process_submission`, `_authenticate_team`,
`_read_position_payload`/`_coerce_position`) — a divergence between them is a bug (§2 of phase 3a).

**Admin JSON (auth: `is_admin=True`)**: `GET /admin/map` (includes every team, true targets, and unapproved submissions)

## Tuning Knobs (in `app/main.py:36-37`)

- `MAX_ATTEMPTS_PER_ITEM = 10` — hard cap per (team, item) before item is locked
- `CONFIDENCE_THRESHOLD = 0.7` — AI `confidence` must be ≥ this to approve

Change with care; both are baked into the `/submit` decision logic.

## Logging

All routes log to `logging.getLogger("uvicorn.error")` — they print to the uvicorn terminal under standard format. Every `/submit` path (success, no-GPS, no-EXIF, too-old, missing-required, missing-bonus, outside-radius, max-attempts, locked, invalid-format, server-error) emits a distinct log line including team name, item name, and reason. Watch these for debugging.
