# Scavenge

A team-based scavenger hunt site where players take photos of items on a list and an AI judges whether the photos match. Built for casual events — birthday parties, family reunions, corporate team-building, campus activities.

Players log in with a team passphrase, see the list of items, and submit photos from their phone. Each photo is sent to an AI vision model that decides whether the photo shows what was asked for. Points are awarded automatically.

## Quick start

```bash
# 1. Create the venv and install dependencies
python3 -m venv venv
./venv/bin/pip install fastapi uvicorn[standard] sqlalchemy[asyncio] aiosqlite jinja2 \
    python-multipart pillow pillow-heif openai pyyaml itsdangerous
# (or use the venv you already have)

# 2. Create admin_config.yaml with your admin password and AI settings
#    see "Configuration" below

# 3. Start the server
./run_server.sh
```

The server binds to `0.0.0.0:8000`, so it's reachable on your local network at `http://<your-ip>:8000/`.

## Configuration

All secrets and runtime settings live in `admin_config.yaml` at the project root. This file is gitignored — never commit it.

```yaml
admin_password: "your_admin_password_here"
openai_api_key: "sk-..."              # your AI provider key
openai_model: "gpt-4o-mini"           # any vision-capable chat model
openai_base_url: "https://api.openai.com/v1"  # or any OpenAI-compatible endpoint
max_photo_age_hours: 8                # max age of EXIF timestamp on a submission

hunt_ends_at: ""                      # ISO timestamp; empty = hunt closed
hunt_duration_minutes: 160            # default duration for "Start Hunt" button

map_tile_url: ""                      # base-map tiles; empty = OSM default
```

The `OPENAI_API_KEY` environment variable takes precedence over the YAML key if both are set. The `MAP_TILE_URL` environment variable likewise overrides `map_tile_url`.

Set the `SESSION_SECRET` environment variable in production. If it is unset, the server generates a random key at startup and warns — which means every restart (including `--reload` code changes) logs all teams out.

## Setting up a hunt

Create a YAML file in `hunts/`. The filename doesn't matter — every `.yaml` file in that directory is loaded.

```yaml
name: "Fall Campus Hunt"
description: "Find hidden items around campus"
theme:                                 # optional; defaults to spring palette
  primary: "#FF6B9D"
  secondary: "#FEC868"
  accent: "#7AC74F"
  background: "#FFF8F0"
  surface: "#FFFFFF"
  text: "#2D3142"
  text_muted: "#6C757D"
  success: "#06A77D"
  warning: "#E63946"
  info: "#4A90E2"
items:
  - name: "Library Clock"
    description: "The big clock in the main library"
    points: 100
    bonus_points: 50
    bonus_hint: "A clear, front-on shot of the clock face is the most reliable way to earn the bonus."
    required_properties:
      - "shows a clock"
      - "shows the main library building"
    bonus_properties:
      - "clock face is clearly visible"
```

### Field reference

| Field | Required | Notes |
|---|---|---|
| `name` | yes | Shown to players. Short and scannable. |
| `description` | yes | One-line explanation players see. |
| `theme` | no | 10 hex colors; omit to use the default spring palette. |
| `items[].name` | yes | Shown to players and used in the AI prompt. |
| `items[].description` | yes | Shown to players and used in the AI prompt. |
| `items[].points` | yes | Awarded on successful submission. |
| `items[].bonus_points` | no | Awarded on top of `points` if all `bonus_properties` match. |
| `items[].bonus_hint` | no | Public hint shown to all teams. |
| `items[].required_properties` | **yes** | AI checks all of these. Missing → item is silently skipped on load. |
| `items[].bonus_properties` | no | AI checks all of these. All match → bonus awarded. |
| `items[].target_lat` / `target_lon` | no | True location used for proximity judging. Absent → `location_status = "no_target"`. |
| `items[].radius_meters` | no | "Hit" ring radius in metres. Default `100`. |
| `items[].near_multiplier` | no | Outer "near" ring = `radius_meters × near_multiplier`. Default `2.0`. |
| `items[].proximity_points` | no | Points for a full hit (half for near). Default `0`. |
| `items[].proximity_mode` | no | `off` \| `bonus` (default) \| `gate`. `gate` also requires a hit/near to approve. |
| `items[].zone_lat` / `zone_lon` / `zone_radius_meters` | no | Centre/radius of the area drawn on the map (phase 2). May be offset from the true target. |

`required_properties` is mandatory. If a hunt file is missing this field on an item, that item is dropped when the file is loaded — the rest of the hunt still loads.

**Proximity judging.** GPS is read server-side from the stored original's EXIF (never from a form field). `points_awarded = AI points + proximity points`. A photo with no GPS still earns its AI points; its `location_status` is recorded as `missing` and the result message says so. In `gate` mode only, a missing or far coordinate also blocks approval.

### Theme colors

The theme is rendered as CSS variables on every page. The defaults work fine; override if you want a different vibe (Halloween, winter, a corporate brand color, etc.). All 10 are required if you provide a `theme:` block.

## Setting up teams

Edit `teams/teams.yaml`:

```yaml
teams:
  - name: "Team Alpha"
    passphrase: "alpha-secret"   # given to the team to log in
  - name: "Team Beta"
    passphrase: "beta-secret"
```

Passphrases are shared with each team ahead of time — they're how teams log in. There's no concept of per-team accounts beyond this. Use something each team will remember but that isn't guessable.

The plaintext passphrase lives only in this YAML file. `/admin/load-teams` stores a salted PBKDF2 hash in the database, and login compares against that hash — the database never holds the plaintext. `POST /team/login` is rate-limited per client IP.

## Loading data and running a hunt

Log in at `/admin/login` with the password from `admin_config.yaml`.

### One-time setup (per hunt)

1. **Load hunts** — visit `/admin/load-hunts`. This reads every YAML file in `hunts/` into the database. Click the button; it should report how many hunts/items were loaded. Items missing `required_properties` are silently skipped — re-check your YAML if a count looks low.
2. **Load teams** — visit `/admin/load-teams`. Same idea; reads `teams/teams.yaml` into the database.

### During the hunt

3. **Start the hunt** — on `/admin/dashboard`, click **Start Hunt**. By default this opens submissions for 160 minutes (configurable via `hunt_duration_minutes`).
4. **Extend** — adds 5 minutes to the current end time. Use it if teams need more time.
5. **Stop** — closes the hunt immediately. Submissions are rejected after this until you Start again.

The timer is global, not per-hunt. If you have multiple hunt YAMLs, only the timer gates submissions — which hunt is "active" is up to you to manage.

### After the hunt

6. Visit `/leaderboard` to see results. Once the hunt is closed, the leaderboard shows a shared gallery of all approved submissions across all teams. Each photo has a "Why this score?" button that reveals the AI's reasoning.
7. `/admin/all-gallery` shows every submission (approved or not) with team labels, the AI's reasoning, and a re-review button.

## How the AI review works

When a team submits a photo:

1. The server reads the EXIF `DateTimeOriginal` timestamp.
   - If `OffsetTimeOriginal` (EXIF 2.31) is present, the timestamp is interpreted as UTC.
   - Otherwise it's treated as server local time.
2. The photo must be newer than `max_photo_age_hours`. Photos older than that are rejected (you can't submit a photo from yesterday).
3. The image is converted to JPEG, base64-encoded, and sent to your OpenAI-compatible endpoint with the item's `name`, `description`, `required_properties`, and `bonus_properties` in the prompt.
4. The model returns a structured JSON response. The server parses `is_target`, `confidence`, `matched_required`, `missed_required`, `matched_bonus`, `missed_bonus`, and `reason`.
5. Decision:
   - `is_target == true` **and** `confidence >= 0.7` → approved, points awarded
   - All `required_properties` matched **and** all `bonus_properties` matched → bonus awarded
   - Otherwise → rejected, attempt counted

Photos with no EXIF timestamp at all are rejected — the server has no way to know if they're fresh.

Each team gets up to **10 attempts** per item. After 10 rejections, the item is locked for that team. The first approved submission locks the item too.

### Tuning the AI

Two knobs are hardcoded in `app/main.py`:

- `MAX_ATTEMPTS_PER_ITEM = 10` — per (team, item) cap
- `CONFIDENCE_THRESHOLD = 0.7` — minimum AI confidence for approval

Edit and restart if you need different values.

The photo sent to the AI is downscaled to at most 1200px, so review cost and latency stay flat regardless of phone camera resolution. There is no built-in spend cap — point `openai_base_url` at a provider whose budget you control.

## Map and live team positions

The web map (phase 2) shows item geofences, the team's own live position, and submission pins. Admin sees every team and unapproved pins.

- **Team page** `/map`; **admin page** `/admin/map-view` (the admin *API* is `/admin/map`). Both are behind auth.
- **Team API** `GET /api/map` — one grouped payload (`items`, `me`, `submissions`). **Admin API** `GET /admin/map` — the same plus `teams`, each item's true target, and unapproved submissions.
- **Never leaks the target.** The zone drawn on the map comes from `zone_lat`/`zone_lon`/`zone_radius_meters`, which may be offset from the true `target_lat`/`target_lon`. The team payload contains only the display zone; `target_lat`, `target_lon` and `radius_meters` are never sent to a team. An item with no `zone_*` is omitted from the team payload. This is enforced by `tests/test_map.py::test_team_map_leaks_no_true_target`.
- **Live position** `POST /api/position` (form fields `lat`, `lon`, optional `accuracy_m`) records the team's current position. It is **display-only and never scored from**, is range-validated (NaN/Inf/non-numeric → 400), and is rate-limited to 60 requests/minute per IP. Only the latest row per team is kept (prior rows are pruned on insert), so the table never grows unbounded.
- **Staleness.** Positions older than **60 seconds** are flagged `stale: true` (and dimmed in the UI). `age_seconds` is sent numerically so the client never parses timestamps.
- **Map page behaviour.** Vendored **Leaflet 1.9.4** (`static/vendor/leaflet/`, no CDN at runtime), 10-second poll with a full rebuild each time, polling paused while the tab is hidden, and a fail-closed error state (markers cleared + visible message) on a failed poll.
- **Offline tiles.** If tiles cannot load, the page still renders zones, markers and the legend on a plain background with a visible, non-blocking notice. Tiles come from `map_tile_url` / `MAP_TILE_URL` (default OSM). The fallback is verified in a real headless-Chrome test (`test_map_still_works_when_tiles_fail`); skip it with no browser present.

## Deployment notes

### Behind NGINX

Phone photos can easily be 5–10 MB. Set a large upload limit in your NGINX config:

```nginx
http {
    client_max_body_size 20M;
    # ...
}
```

Otherwise you'll get `413 Request Entity Too Large` for big phone photos and the player will see a generic upload error.

### HEIC photos

iPhone photos in HEIC format are supported via `pillow-heif`. The dependency is registered at module load in `app/ai_review.py`. If a player submits a HEIC, it's converted to JPEG before being sent to the AI.

### Files on disk

Uploaded photos land in `uploads/originals/`. Thumbnails (200px) and display-size versions (1200px) land in `uploads/thumb/` and `uploads/display/` respectively. These directories are gitignored. `/admin/cleanup-orphans` deletes files that are no longer referenced in the database — useful after a `/admin/reset`.

### Schema changes

There are no migrations. If you change a model in `app/models.py`, the database needs to be dropped and recreated:

1. Visit `/admin/reset` (confirmation dialog). This drops the tables and wipes `uploads/`.
2. Restart the server.
3. Visit `/admin/load-hunts` and `/admin/load-teams` to repopulate.

## Admin endpoints reference

| Endpoint | Purpose |
|---|---|
| `/admin/login` | Password login (cookie-based session) |
| `/admin/dashboard` | Hunt timer controls, tool links |
| `/admin/map-view` | Interactive map: all teams, true targets, unapproved pins |
| `/admin/map` | GET; admin map JSON payload (behind admin auth) |
| `/admin/load-hunts` | Read all `hunts/*.yaml` into the DB |
| `/admin/load-teams` | Read `teams/teams.yaml` into the DB |
| `/admin/hints` | View all bonus hints + private AI criteria |
| `/admin/all-gallery` | Every submission with team label and AI reasoning |
| `/admin/review-submission/{id}` | POST; re-run AI review on one submission |
| `/admin/cleanup-orphans` | Delete upload files not in the DB |
| `/admin/reset` | Drop tables, wipe uploads — destructive |
| `/admin/hunt/start` | POST; start the timer for `hunt_duration_minutes` |
| `/admin/hunt/extend` | POST; add 5 minutes to the current end time |
| `/admin/hunt/stop` | POST; close the hunt immediately |

## Troubleshooting

**"Hunt closed" banner on the dashboard.**
The hunt timer is empty or in the past. Go to `/admin/dashboard` and click Start.

**Submissions always rejected with "photo too old".**
The EXIF timestamp is older than `max_photo_age_hours`. Either increase that value or check that phones have correct date/time set. If the photo is fresh but the server still rejects it, the EXIF tag may be missing or the timezone offset wrong.

**Submissions rejected with "AI service not configured".**
The OpenAI key isn't set. Add `openai_api_key` to `admin_config.yaml` (or export `OPENAI_API_KEY`) and restart the server.

**Items missing from a hunt.**
Check that every item in the YAML has `required_properties`. Items without it are silently dropped on load.

**413 errors on upload.**
NGINX `client_max_body_size` is too small. Set to `20M` or higher.

**"Invalid response" in the AI log.**
The model returned something the server couldn't parse as JSON. Check the AI provider's logs and consider switching to a more reliable model.

**Logs.**
All routes log to `uvicorn.error`. Every `/submit` rejection path emits a distinct log line with the team name, item name, and reason. Tail them while running a hunt to debug player issues.
