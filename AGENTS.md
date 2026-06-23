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

- **AI is the sole gate** — no GPS/radius check. Old schema had `lat`/`lon`/`radius_meters`/`location`; removed. If you see those in old YAMLs, they're stale.
- **Schema changes require `/admin/reset`** — adding a column to `models.py` doesn't migrate; it requires drop+recreate. Then `/admin/load-hunts` and `/admin/load-teams` to repopulate.
- **Hunt timer is global state in `admin_config.yaml`** — `hunt_ends_at` (ISO timestamp or empty) + `hunt_duration_minutes` (default 160). When empty, hunt is closed and `/submit` rejects. Admin dashboard has Start/Extend (+5min)/Stop buttons.
- **Submission flow gates** (in order in `app/main.py:636`): hunt state → empty file → locked check (10 attempts max) → freshness check (EXIF timestamp vs server time) → AI review → DB insert.
- **EXIF timestamp is interpreted as UTC** if `OffsetTimeOriginal` (EXIF 2.31, tag 0x9010) is present, else treated as server local time. Photos with no EXIF timestamp are rejected.
- **NGINX upload limit** — if serving behind NGINX, set `client_max_body_size 20M+` in the `http {}` block or you'll get `413 Request Entity Too Large` for phone photos.
- **Three StaticFiles mounts** for uploads: `/uploads/originals`, `/uploads/thumb`, `/uploads/display`. Don't change to a single mount — the dirs map directly to URL paths.
- **`admin_config.yaml` is gitignored but lives on disk with the real OpenAI key** (key takes precedence over `OPENAI_API_KEY` env var). If you copy this repo to share, scrub the key or rotate it with the provider.

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

**Public**: `/`, `/admin/login`, `/team/login`, `/logout`, `/result`, `/uploads/{originals,thumb,display}/...`, `/static` (none)

**Team (auth: `team_id` in session)**: `/hunt/dashboard`, `/gallery`, `/leaderboard`

**Admin (auth: `is_admin=True` in session)**: `/admin/dashboard`, `/admin/hints`, `/admin/all-gallery`, all `POST /admin/*` endpoints

**Always public, no auth**: `POST /submit` (gated by hunt state only, since the form posts `team_id` directly)

## Tuning Knobs (in `app/main.py:36-37`)

- `MAX_ATTEMPTS_PER_ITEM = 10` — hard cap per (team, item) before item is locked
- `CONFIDENCE_THRESHOLD = 0.7` — AI `confidence` must be ≥ this to approve

Change with care; both are baked into the `/submit` decision logic.

## Logging

All routes log to `logging.getLogger("uvicorn.error")` — they print to the uvicorn terminal under standard format. Every `/submit` path (success, no-GPS, no-EXIF, too-old, missing-required, missing-bonus, outside-radius, max-attempts, locked, invalid-format, server-error) emits a distinct log line including team name, item name, and reason. Watch these for debugging.
