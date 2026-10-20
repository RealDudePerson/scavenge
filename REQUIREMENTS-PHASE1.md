# REQUIREMENTS — Phase 1: foundations, coordinate judging, security fixes

Repo: `~/projects/scavenge` (FastAPI + SQLAlchemy async + SQLite + Jinja2).
Approved by owner. Decisions taken: see `PROPOSAL-map-coordinates-android.md` §9.

**Scope: server-side only.** The map UI is phase 2; the Android app is phase 3. Do not
build either here.

**There is no data to preserve.** The owner confirmed a destructive reset is acceptable,
so schema changes need no migration — change the models and reset.

---

## 1. Foundations

### 1.1 Fix the EXIF capture-time read (the load-bearing bug)

`get_photo_taken_at()` (`app/main.py:537-578`) is broken and is the entire photo-age
anti-cheat gate. Reproduced: a photo taken `2024:06:01 09:30:00 -07:00` is read as
`2024-01-01 07:00 UTC` — **153 days off**.

Root cause: Pillow's `Exif.get()` consults **IFD0 only**. `DateTimeOriginal` (36867) and
`DateTimeDigitized` (36868) live in the **Exif sub-IFD**, so `exif.get(36867)` returns
`None`. The loop silently falls through to IFD0 `DateTime` (306) — the *file-write* time —
and then applies `OffsetTimeOriginal`, which belongs to `DateTimeOriginal`, to it.

Required behaviour:
- Read the capture time in priority order **`DateTimeOriginal` → `DateTimeDigitized` →
  `DateTime`**, sourcing the first two from `exif.get_ifd(ExifTags.IFD.Exif)`.
- Apply **each tag's own** offset: `OffsetTimeOriginal` (0x9010) only with
  `DateTimeOriginal`, `OffsetTimeDigitized` (0x9011) only with `DateTimeDigitized`. Do not
  apply an offset that belongs to a different tag.
- Return `None` only when no timestamp tag exists at all.
- **Store the result** on the submission as `captured_at` (currently computed then thrown
  away). The freshness gate keeps using it.

### 1.2 Apply EXIF orientation
Call `ImageOps.exif_transpose` before generating thumbnails and the display copy, so phone
photos stop rendering sideways. `make_thumbnails` (`app/main.py:581-596`).

### 1.3 Sanitise the upload filename
`app/main.py:631-632` interpolates the client-supplied filename straight into the path.
Reduce it to a safe basename: strip any directory component, allow only
`[A-Za-z0-9._-]`, cap the length, and never let it escape `ORIGINALS_DIR`. The `uuid4`
prefix must remain.

### 1.4 Scope the dashboard to the active hunt
`select(HuntItem)` at `app/main.py:317` (and the `max_points` sum at `:337`) has no hunt
filter, so a multi-hunt install mixes items and inflates the maximum. Filter by the active
hunt.

---

## 2. Coordinate judging

### 2.1 Schema

`HuntItem` — add:
- `target_lat`, `target_lon` (Float, nullable) — the true target, used for judging.
- `radius_meters` (Float, default 100) — the judging tolerance ("hit" ring).
- `near_multiplier` (Float, default 2.0) — outer "near" ring = `radius_meters × near_multiplier`.
- `proximity_points` (Integer, default 0) — points for a full hit.
- `proximity_mode` (String, default `"bonus"`) — `off | bonus | gate`.
- `zone_lat`, `zone_lon` (Float, nullable) — centre of the geofence area **drawn on the map**
  (phase 2). May differ from the true target.
- `zone_radius_meters` (Float, nullable) — radius of that drawn area.

`Submission` — add:
- `captured_at` (DateTime, nullable) — from §1.1.
- `gps_lat`, `gps_lon`, `gps_accuracy_m` (Float, nullable).
- `distance_m` (Float, nullable) — distance from the target.
- `location_status` (String) — `hit | near | far | missing | no_target | off`.
- `proximity_points_awarded` (Integer, default 0).
- `live_lat`, `live_lon` (Float, nullable) — the phone's own fix at submit time, if sent.

`hunts/*.yaml` items accept the same new keys, loaded beside the existing fields in
`load_hunts` (`app/main.py:226-235`). `required_properties` stays mandatory.

**On the geofence zone:** the owner asked for "geofenced area around items, not perfectly
centered". Interpreted as: the drawn area's centre may be offset from the true target so the
exact spot is not given away. Hence separate `zone_*` columns, defaulting to the true target
when absent. **This interpretation is flagged for owner confirmation** — it is the one design
point that could be wrong.

### 2.2 Parsing GPS

- Read GPS **server-side from the stored original only**, via
  `exif.get_ifd(ExifTags.IFD.GPSInfo)`. **Never** from a multipart field — that is
  client-supplied and forgeable.
- Validate: lat ∈ [-90, 90], lon ∈ [-180, 180], reject NaN/Inf and absurd values.
- A missing or invalid GPS is **not** an error — see §2.4.

### 2.3 Distance and scoring

Implement haversine server-side (~10 lines; do **not** add a geospatial dependency).

`ai_points` keeps its existing meaning exactly (`app/main.py:706-714`). Proximity is added
**beside** it, never instead of it:

| Condition | `location_status` | `proximity_points_awarded` |
|---|---|---|
| item has no target | `no_target` | 0 |
| `proximity_mode == "off"` | `off` | 0 |
| no GPS in the photo | `missing` | 0 |
| `d <= radius_meters` | `hit` | `proximity_points` |
| `d <= radius_meters × near_multiplier` | `near` | `proximity_points // 2` |
| otherwise | `far` | 0 |

`points_awarded = ai_points + proximity_points_awarded`.

**In `gate` mode only**, proximity also gates approval:
`verified = all_required_met and location_status in {"hit", "near"}`. Default is `bonus`,
where a missing or forged coordinate can never deny an otherwise valid photo.

### 2.4 Missing GPS must never fail silently

The owner's own note: *"sometimes GPS doesn't embed in the photo if there's no lock."*
This is expected, not exceptional. Therefore:

- The photo is still judged by the AI and can still earn its AI points.
- `location_status` is **always** persisted.
- The `/result` message states it in words — e.g. "Location unavailable — no proximity
  points" — never a silent zero.
- A distinct `uvicorn.error` log line is emitted (follow the pattern at `app/main.py:668-669`).
- `admin_all_gallery` surfaces `location_status` and `distance_m` per submission.
- Add a `proximity_points_awarded` display to the gallery and dashboard totals, and include
  `proximity_points` in the dashboard `max_points` (`:337`).

### 2.5 Client-supplied live fix

The app (phase 3) may send `live_lat`/`live_lon`. Treat as **untrusted display data**: store
it, never score from it, and never let it override the EXIF-derived value.

---

## 3. Security fixes (owner: "fix all")

1. **`/leaderboard` requires team auth** (`app/main.py:353-354` currently has no dependency).
2. **Team passphrases must not be stored or compared in plaintext**
   (`app/models.py:34`, compared at `app/main.py:305`). Hash them; compare with a
   constant-time check. `load_teams` (`app/main.py:243-257`) must hash on load.
3. **Rate-limit `POST /team/login`** (`app/main.py:302-310`) — there is none today.
4. **Filename traversal** — covered by §1.3.

Do not break the existing hunter flow: a team must still be able to log in with its
passphrase from `teams/teams.yaml`.

---

## 4. Tests — required, and must be able to fail

The repo has **no test suite**. Add one (`pytest`), and make it genuinely red-capable:
for each of the critical fixes, demonstrate the test fails against the pre-fix code.

Minimum coverage:
- **EXIF capture time** — build a camera-shaped JPEG in the test (IFD0 `DateTime`; sub-IFD
  `DateTimeOriginal` + `OffsetTimeOriginal`, i.e. *different* values), and assert the
  function returns the **`DateTimeOriginal`** instant converted to UTC. Also: only
  `DateTimeOriginal` present → returns it (not `None`); no timestamp at all → `None`.
  *This is the test that must fail against the current code — prove it.*
- **GPS parse** — GPS present → correct lat/lon; absent → `None` and `location_status`
  becomes `missing` (not a crash, not a silent pass).
- **Proximity** — hit / near / far / missing / no_target each map to the right status and
  points, at and around the exact boundaries (`d == radius`, `d == radius × multiplier`).
- **Missing GPS still awards AI points** — the explicit anti-silent-failure guarantee.
- **Filename sanitisation** — `../../etc/passwd`, absolute paths, and odd characters cannot
  escape the upload directory.
- **Auth** — `/leaderboard` rejects an unauthenticated request.
- **Passphrase hashing** — a correct passphrase logs in; a wrong one does not; the stored
  value is not the plaintext.

State plainly which tests you ran and their results. If something cannot be tested, say so
rather than implying it passed.

---

## 5. Environment

`venv/` does not exist and the app's dependencies are not installed. Create the venv and
install the pinned dependencies (see `README.md`, `package.json`). Note: the system default
`python3` is 3.14.7, for which some wheels may not exist — if the install fails, use
`/usr/bin/python3.12`. Record what you actually used.

## 6. Acceptance criteria

1. `get_photo_taken_at` returns the `DateTimeOriginal` instant, verified against a
   hand-built camera-shaped JPEG, with the offset applied to the correct tag.
2. `captured_at` is persisted on submissions.
3. Thumbnails are upright for a rotated photo.
4. A submission with GPS inside the radius earns AI points **plus** proximity points.
5. A submission with **no GPS** still earns its AI points, records
   `location_status = "missing"`, and says so in the result message.
6. A filename containing `../` cannot write outside `uploads/originals/`.
7. `/leaderboard` returns 403/redirect when unauthenticated.
8. Team passphrases are stored hashed and login still works.
9. `pytest` passes; the EXIF test is demonstrated failing against the original function.
10. The existing hunter flow (login → dashboard → submit → gallery → leaderboard) still
    works end to end.

## 7. Out of scope

- The map UI and `/api/map` (phase 2).
- The Android companion (phase 3).
- Admin surfaces in the Android app.
- Making coordinates tamper-proof — they cannot be. This is a game mechanic.

## 8. Honesty requirement

Report what you verified versus what you could not. Do not claim a test passed if you did
not run it, and do not describe behaviour you did not observe. If you deviate from these
requirements, say so and say why.
