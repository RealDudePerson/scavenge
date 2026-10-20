# Analysis brief — scavenge: map + coordinate judging + Android companion

You are analysing an existing application. **This is an analysis task only: do not
implement, refactor, or modify anything except writing the single output file
described at the end.**

## The repository

`~/projects/scavenge` — FastAPI + SQLAlchemy (async) + SQLite + Jinja2 + Foundation CSS.
A team-based scavenger hunt: teams log in with a passphrase, see a list of items, and
submit photos. An AI vision model judges each photo against per-item required/bonus
properties and points are awarded automatically.

Key files: `app/main.py` (~776 lines, all routes + submission logic), `app/models.py`,
`app/ai_review.py`, `app/config.py`, `app/database.py`, `templates/*.html`,
`hunts/*.yaml`, `teams/teams.yaml`, `AGENTS.md`.

## The reference project

`~/projects/beakon` — a separate location-sharing server of mine (Flask + SQLite,
Leaflet map). Relevant prior art: it renders an interactive map at `/map` from a
single grouped endpoint `GET /api/locations/shared`, with per-person colour, a legend
with show/hide toggles, and a 10s refresh. **Read it for the map pattern only** — it is
a different stack; do not propose copying its code.

## What the owner wants to build (context for your analysis)

1. Combine scavenge with a **beakon-style interactive map**.
2. Add **location-coordinate judging to the point system** — items should be findable
   at/near a target coordinate, and proximity should affect points.
3. Build an **Android companion app** that submits photos **with GPS EXIF intact**.
   The owner's blocker: *"photo uploads to the web on android silently strip the
   location exif data."* The app must request
   `<uses-permission android:name="android.permission.ACCESS_MEDIA_LOCATION" />`.
   It should **largely replicate the interface of the web version**.

## Deliverable — analyse and report

Write `ANALYSIS.md` in the repo root. Use file:line references throughout and quote
code where it matters. Cover:

### 1. Architecture map
- The `/submit` flow end to end: request → validation → EXIF read → thumbnail
  generation → AI review → scoring → DB write → response. Cite the lines.
- The scoring formula exactly as implemented, and every place points are computed or
  summed (including leaderboard and dashboard totals).
- The auth/session model (team passphrase, admin password, how identity is carried).
- The image pipeline: upload dirs, thumbnail sizes, what is stored where, cleanup.
- The config surface in `app/config.py` and `admin_config.yaml`.

### 2. EXIF handling — be precise
- Exactly which EXIF fields are read today, by which function, and what is done with
  them. Quote the code.
- Confirm whether **GPS** is read anywhere. If not, say so plainly.
- What is discarded, and at what point.
- Note anything that would already be broken by a photo lacking EXIF.

### 3. Anti-cheat, as it exists
- The photo-age gate (`get_max_photo_age_hours`) — how it works and what it depends on.
- The attempt counter, `/submit` gating, and any other integrity control.
- State clearly what stops cheating today and what does not.

### 4. Integration design (design only, no code)
- **(a) Coordinate judging.** How to add per-item target coordinates + radius, and how
  to blend proximity into the point system without breaking the existing
  required/bonus model. Consider: exact hit vs near vs out of range; whether
  proximity is a gate, a multiplier, or a separate bonus; what happens when a photo
  has no GPS at all (must not silently fail). Propose the schema change.
- **(b) The map.** What a Leaflet view needs from the server: endpoints, payload shape,
  what is plotted (item targets, team positions, trails), refresh strategy, and how it
  respects the existing auth model.
- **(c) The Android companion.** What the app must do to get intact GPS EXIF.

### 5. Android EXIF mechanics — be specific and correct
Explain precisely:
- Why Android strips location EXIF from photos a web page receives, and at what layer.
- What `ACCESS_MEDIA_LOCATION` grants and what `MediaStore.setRequireOriginal(uri)`
  does, and when each is required.
- The difference between an in-app camera capture (CameraX / `ACTION_IMAGE_CAPTURE`)
  and picking from the gallery — which path preserves GPS and which needs
  `setRequireOriginal`.
- How to read GPS with `ExifInterface` and what happens if the field is absent.
- What the app must send to the server so the coordinate judging has trustworthy data,
  and **how the server should treat client-supplied coordinates as untrusted input**.
- Which parts of the web UI must be replicated for parity, based on your reading of
  `templates/`.

### 6. Risks and unknowns
- Anything that would break, regress, or need migration.
- What you could not determine from the repo alone.
- Explicitly separate **what you verified by reading code** from **what you are
  inferring**. Do not present inference as fact.

## Rules
- **Analysis only.** The only file you may create or modify is `ANALYSIS.md`.
- Do not run the server, install packages, or change dependencies.
- Every claim about the existing code must be traceable to a file:line you actually
  read. If you are unsure, say you are unsure.
