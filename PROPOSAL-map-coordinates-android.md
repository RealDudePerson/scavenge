# Proposal — scavenge: map, coordinate judging, Android companion

Status: **draft for owner review. No code written yet.**
Provenance: repo analysis by opencode (`ANALYSIS.md`, 678 lines, every claim tagged
`[V]`/`[I]`/`[?]`), plus independent re-verification by Hermes of the load-bearing
claims (see §2).

---

## 1. What the app is today

FastAPI + SQLAlchemy (async) + SQLite + Jinja2 + vendored Foundation/jQuery, ~2,100 lines.
Teams log in with a passphrase and submit photos against hunt items; an OpenAI-compatible
vision model judges each photo against per-item `required_properties`/`bonus_properties`;
points are awarded server-side.

Scoring, exactly (`app/main.py:706-714`):

```python
all_required_met = review["is_target"] and review["confidence"] >= 0.7 and not review["missed_required"]
all_bonus_met    = all_required_met and not review["missed_bonus"] and len(bonus) > 0
awarded_bonus    = item.bonus_points if all_bonus_met else 0
points_awarded   = item.points + awarded_bonus if all_required_met else 0
```

**There is no location concept anywhere.** `app/models.py` has no coordinate columns;
GPS is never read; `AGENTS.md` confirms the old lat/lon/radius schema was removed.

---

## 2. Verified findings that change the plan

### 2.1 The EXIF timestamp read is broken — reproduced independently

`get_photo_taken_at()` (`app/main.py:537-578`) is the **only** EXIF reader, and it is
load-bearing: it is the entire photo-age anti-cheat gate.

I built a camera-shaped JPEG by hand (`DateTime` in IFD0; `DateTimeOriginal` +
`OffsetTimeOriginal` in the Exif sub-IFD — what a phone actually writes) and ran the
repo's function verbatim against it:

| | |
|---|---|
| Photo actually taken | `2024:06:01 09:30:00 -07:00` → **2024-06-01 16:30 UTC** |
| File written (IFD0 `DateTime`) | `2024:01:01 00:00:00` |
| **What the repo returns** | **`2024-01-01 07:00 UTC`** |
| Error vs the true capture instant | **153 days, 14.5 hours** |

Two defects compound:

1. **It reads the wrong tag.** Pillow's `Exif.get()` only consults IFD0; `DateTimeOriginal`
   lives in the Exif sub-IFD. Confirmed directly: with a camera-shaped file,
   `exif.get(36867)` → `None` while `exif.get(306)` → the IFD0 value. The loop at
   `app/main.py:548` iterates `(36867, 36868, 306)` and silently falls through to `306` —
   the file-write time, a *different semantic value* from the capture time.
2. **It applies the wrong tag's offset.** `OffsetTimeOriginal` (the offset *of*
   `DateTimeOriginal`) is applied to whatever was read (`:560,574`), shifting the result by
   up to ±14 h.

**Consequences:** photos whose only timestamp is `DateTimeOriginal` are rejected outright as
"no EXIF timestamp"; the offset mix can flip the freshness decision either way; and because
the gate effectively reads the *file* timestamp, **re-saving an old photo refreshes it past
the gate** — so the anti-cheat is both semantically wrong and defeatable.

`README.md:136` and `AGENTS.md:65` describe the intended behaviour, not the actual behaviour.

### 2.2 Other verified findings

- **GPS is read nowhere** — no `GPSInfo`, no `34853`, no coordinate columns. GPS *is*
  physically present in stored originals; the server just never looks.
- **`/leaderboard` has no auth dependency** (`app/main.py:353-354`) although `AGENTS.md`
  lists it as team-authenticated. It exposes team names, scores and every approved photo.
  A live-position map must **not** follow this pattern.
- **Multi-hunt bleed** — the dashboard's item list and `max_points` come from
  `select(HuntItem)` with no hunt filter (`app/main.py:317,337`).
- **EXIF is stripped from derived images** (`:592,596`, `app/ai_review.py:56`), so
  `uploads/originals/` is the only possible GPS source.
- **Orientation is never applied** (no `exif_transpose`) — phone thumbnails can render sideways.
- **Adding columns requires a destructive reset** — `init_db` only calls `create_all`
  (`app/database.py:10-12`); new columns need `/admin/reset` + reload, which **drops all
  hunts, teams and submissions**.

---

## 3. Proposed workstreams

Four parts, in dependency order. A and B are prerequisites for a meaningful C/D.

### A. Server foundations (prerequisite)
1. **Fix the EXIF timestamp read** — read `DateTimeOriginal` via
   `exif.get_ifd(ExifTags.IFD.Exif)`, fall back to `DateTimeDigitized`, then IFD0
   `DateTime`, and apply each tag's *own* offset. Without this, any coordinate feature
   inherits a broken clock.
2. **Add `ImageOps.exif_transpose`** so phone photos stop rendering sideways.
3. **Harden the filename** — the upload filename is used verbatim in the path
   (`:631-632`); strip to a safe basename.
4. **Filter the dashboard by the active hunt** to stop multi-hunt bleed.

### B. Coordinate judging
5. Schema: per-item target + radius; per-submission captured coordinate + verdict.
6. Parse GPS **server-side from the stored original** (`get_ifd(GPSInfo)`) — never from a
   multipart field.
7. Haversine distance (~10 lines, no new dependency).
8. Proximity blended in as **additive points by default**, with an explicit
   `location_status` persisted and surfaced so a missing GPS is visible, never silent.

### C. Interactive map (beakon pattern, different stack)
9. Vendored Leaflet, one grouped JSON endpoint, 10 s poll, rebuild-per-poll,
   `visibilitychange` pause, fail-closed on error.
10. Plot item targets as radius circles; team submission pins; optional trails.
11. **Behind team/admin auth** — explicitly not on the public `/leaderboard`.

### D. Android companion
12. Native Kotlin/Compose app replicating the hunter surface.
13. **`ACCESS_MEDIA_LOCATION`** + `MediaStore.setRequireOriginal()` for gallery picks;
    CameraX in-app capture with the location attached as the preferred path.
14. Uploads original bytes unmodified; server does the trusting.

---

## 4. Data model changes (proposed)

`HuntItem` gains: `target_lat`, `target_lon`, `radius_meters`, `proximity_points`,
`proximity_mode` (`off|bonus|gate`), `near_multiplier`.

`Submission` gains: `gps_lat`, `gps_lon`, `gps_accuracy_m`, `distance_m`,
`location_status` (`hit|near|far|missing|no_target`), `proximity_points_awarded`,
and `captured_at` (the EXIF instant, currently computed then discarded).

`hunts/*.yaml` items gain the same keys, loaded beside the existing fields.

> **Destructive:** adding columns needs `/admin/reset` + hunt reload. Existing
> submissions do not survive. See decision 9.

---

## 5. Proximity scoring (proposed)

`ai_points` stays exactly as it is. Proximity is added alongside it:

| Distance from target | Result |
|---|---|
| `d <= radius_meters` | full `proximity_points`, status `hit` |
| `radius_meters < d <= radius_meters × near_multiplier` | half, status `near` |
| beyond | `0`, status `far` |
| no GPS in the photo | `0`, status `missing` — **photo still judged, message says why** |
| item has no target | `0`, status `no_target` |

`points_awarded = ai_points + proximity_points_awarded`.

In `gate` mode (opt-in per item) proximity also gates approval — for items where *being
there* is the challenge. Default is `bonus` so a missing or forged coordinate can never
silently deny a legitimate photo.

**Honest limitation:** client-supplied coordinates are forgeable — EXIF can be edited and
multipart fields spoofed. This is a **game mechanic**, not a security control. It stops
casual cheating; it is not tamper-proof. What we can do cheaply: parse GPS server-side only,
validate ranges, cross-check the photo timestamp against the clock, and log every
coordinate with team/item.

---

## 6. Map design (proposed)

Borrowed from beakon: single grouped endpoint, per-entity colour (golden-angle index),
legend with show/hide toggles, 10 s poll, full rebuild each poll so disappeared entities
vanish, `visibilitychange` pause, empty payload + message on error (fail closed).

- `GET /api/map` — team auth. Items (target + radius + points) and team pins.
- `GET /admin/map` — admin auth. Same, plus unapproved submissions.

Server sends `age_seconds` so the client can dim stale pins without parsing strings.

**Tiles are the open question:** Leaflet JS/CSS can be vendored for offline, but base map
tiles come from a tile server over the network. For a desert hunt that may not exist.
Options in decision 5.

---

## 7. Android companion (proposed)

### Why the web upload loses GPS
The stripping happens in the **OS media provider**, not the browser. Since Android 10,
`MediaProvider` redacts location metadata for any process without `ACCESS_MEDIA_LOCATION`,
and a web page cannot hold it. A `<input type="file">` picker returns a redacted stream, so
the bytes reaching JavaScript already lack GPS. (The GPS tags arrive as **zeros** with a
zero denominator — `NaN` — which is why it reads as "silently stripped" while
`DateTimeOriginal` survives. Tracked upstream as Chromium 1498162.)

### The two paths that work
- **In-app capture (preferred)** — CameraX writing to an app-owned file, with the location
  attached at capture time. The app owns the media, so nothing is redacted and no special
  permission is needed to read it back. CameraX does **not** attach GPS automatically.
- **Gallery pick** — resolve the `content://media/...` URI through
  `MediaStore.setRequireOriginal(uri)` under `ACCESS_MEDIA_LOCATION`, then read with
  `androidx.exifinterface.media.ExifInterface`.

Manifest needs `<uses-permission android:name="android.permission.ACCESS_MEDIA_LOCATION" />`
(API 29+) **and** a runtime request — declaring it alone does nothing. Read GPS with
`ExifInterface.getLatLong()`, which returns `false` when absent; if absent, upload anyway and
let the server record `missing` rather than blocking the hunter.

### Location sources
The app can send two different things, and they mean different things:
- **EXIF GPS** — where the *photo* was taken.
- **Live device fix** — where the *phone* is *now* (`ACCESS_FINE_LOCATION`).

I recommend sending both and letting the server score off EXIF while cross-checking the live
fix (they should agree within a tolerance for a genuinely on-site photo). This also gives the
map a live position without a second tracking service. Decision 2.

---

## 8. Phasing

| Phase | Content | Depends on |
|---|---|---|
| 1 | Server foundations (A) + coordinate judging (B), web only | — |
| 2 | Map (C), web only | 1 |
| 3 | Android companion (D) | 1 (map optional) |

Each phase is separately reviewable and separately deployable. Nothing in phase 1 breaks the
existing web flow.

---

## 9. Decisions required

1. **Proximity model** — additive bonus / hard gate / per-item choice. *(Recommend: per-item,
   default bonus.)*
2. **Which location scores** — photo EXIF / live device fix / both with cross-check.
   *(Recommend: EXIF authoritative, live fix cross-checked.)*
3. **What the map shows** — item targets only / targets + submission pins / targets + **live
   team tracking**. *(Recommend: targets + pins; live tracking is a bigger lift and a real
   privacy question.)*
4. **Who sees whom on the map** — admin sees all, teams see everyone / teams see only their own.
   *(Recommend: admin all; teams see items + their own team.)*
5. **Map tiles offline** — pre-cache an area / rely on connectivity / vendored Leaflet with a
   blank-canvas fallback. *(Recommend: fallback mode.)*
6. **Android approach** — native Compose / WebView shell loading the real web UI with a native
   capture bridge. *(Recommend: native — reliable EXIF control, offline assets. The WebView
   shell gives exact UI parity for far less work if that matters more.)*
7. **Android surface scope** — login + items + upload only / + my gallery / + leaderboard.
   *(Recommend: full hunter parity, matching "largely replicate the interface".)*
8. **Pre-existing fixes** — the timestamp bug is a prerequisite for B, so it ships in phase 1.
   The rest (public `/leaderboard`, plaintext passphrases, no login rate limiting, filename
   traversal) are **not** required by this feature. *(Recommend: separate pass, not folded in.)*
9. **Is there live data to preserve?** Adding columns requires a destructive
   `/admin/reset` + reload. *(If yes, I'll write a migration instead.)*

---

## 10. Non-goals

- Admin surfaces in the Android app (hunter-facing only).
- Making coordinates tamper-proof — they can't be, and claiming otherwise would be dishonest.
- Replacing the AI judge; proximity is added beside it, never instead of it.
- Fixing the app's unrelated security debt in this feature (see decision 8).
