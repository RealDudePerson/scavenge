# ANALYSIS — scavenge: map + coordinate judging + Android companion

Scope: read-only analysis of `~/projects/scavenge` (and `~/projects/beakon` for the map
pattern only). **No repository file other than this one was created or modified.**
Every claim about existing code cites `file:line`.

**How to read this document.** Statements are labelled:

- **[V]** — *Verified*: read directly in the repo (or reproduced by running the repo's
  own logic in isolation, described in §0.2).
- **[I]** — *Inferred*: my reasoning, library/platform behaviour, or extrapolation.
  Not established by the repo.
- **[?]** — *Unknown*: could not be determined from the repo alone.

---

## 0. Method and evidence

### 0.1 What I read

Repo files: `app/main.py` (776 lines), `app/models.py`, `app/config.py`,
`app/database.py`, `app/ai_review.py`, all 10 files in `templates/`, `hunts/joshua_tree.yaml`,
`teams/teams.yaml`, `README.md`, `AGENTS.md`, `package.json`, `run_server.sh`, `.gitignore`.
Reference project: `~/projects/beakon` — `src/app.py` (map routes + helpers),
`static/js/shared_map.js`, `static/js/map.js`, `templates/shared_map.html`,
`REQUIREMENTS-SHARED-MAP.md`.

### 0.2 How the EXIF claims were verified

`venv/` does not exist in the repo and neither `fastapi` nor `starlette` is installed, so
the server cannot be run here (and I did not run it). For EXIF behaviour I used the
system-installed **Pillow 10.2.0** (`/usr/bin/python3.12`), which satisfies the repo's
pinned `pillow: ^10.0.0` (`package.json:11`). I built JPEGs in memory with controlled
EXIF, then executed a **verbatim copy** of `get_photo_taken_at` (`app/main.py:537-578`)
against them. Results are in §2.3. This is a real reproduction, not a guess.

### 0.3 What is *not* on disk

`admin_config.yaml`, `uploads/`, and `scavenge.db` do not exist in the working tree
(they are gitignored: `.gitignore:16,19`; the upload dirs are created at import time by
`app/config.py:15-20`). So no live config, no database, no photos were available. **[V]**

---

## 1. Architecture map

### 1.1 The `/submit` flow, end to end

Entry point: `POST /submit` (`app/main.py:599-600`). Signature requires
`item_id`, `team_id`, `file`, and a team session (`_: None = Depends(require_team)`).

| # | Step | Location | Notes |
|---|---|---|---|
| 1 | Team-session match | `app/main.py:601-604` | posted `team_id` must equal `session["team_id"]`; else reject |
| 2 | Hunt-open gate | `app/main.py:606-611` | `get_hunt_state()["is_open"]`; else reject |
| 3 | Load team + item | `app/main.py:616-624` | two `select`s; unknown item → reject |
| 4 | Read upload into memory | `app/main.py:626-629` | `contents = await file.read()`; empty → reject |
| 5 | Build paths | `app/main.py:631-635` | `filename = f"{uuid4().hex}_{file.filename}"`; original/thumb/display paths |
| 6 | Write original | `app/main.py:637-638` | raw bytes written to `uploads/originals/<uuid>_<name>` |
| 7 | Thumbnails | `app/main.py:640` → `581-596` | 200px thumb (q85) + 1200px display (q90) |
| 8 | EXIF read | `app/main.py:642-647` | opens the **saved original**, calls `get_photo_taken_at` (`537-578`) |
| 9 | No-timestamp gate | `app/main.py:649-652` | `taken_at is None` → cleanup + reject |
| 10 | Age computation | `app/main.py:654-662` | aware→compare to UTC now; naive→compare to server-local now |
| 11 | Freshness gate | `app/main.py:664-670` | `age > max_age` → cleanup + reject |
| 12 | Future warning | `app/main.py:672-674` | `age < -1h` only logs, does not reject |
| 13 | Locked check | `app/main.py:676-687` | any `verified` submission for (team,item) → cleanup + reject |
| 14 | Attempt count | `app/main.py:689-698` | `count >= MAX_ATTEMPTS_PER_ITEM (10)` → cleanup + reject |
| 15 | Decode properties | `app/main.py:700-701` | `json.loads(required_properties/bonus_properties)` |
| 16 | AI review | `app/main.py:702-704` | `run_in_threadpool(review_image, original_path, …)` |
| 17 | Scoring | `app/main.py:706-714` | see §1.2 |
| 18 | Race re-check | `app/main.py:716-735` | re-runs locked + attempt queries, then insert or bail |
| 19 | DB write | `app/main.py:737-750` | `Submission(...)`; `attempt_number = attempt_count + 1` |
| 20 | Response | `app/main.py:752-767` | JSON `{message, redirect}`; success vs rejection |

Error handling: `Image.UnidentifiedImageError` → "Unsupported format"
(`app/main.py:769-772`); bare `except Exception` → "Server error" (`app/main.py:774-777`).
Both clean up files.

**[V]** Ordering note: the actual code runs the **freshness gate before** the
locked/attempt gates (steps 11 → 13-14). `AGENTS.md:62` documents the order as
"empty file → locked check (10 attempts max) → freshness check", which is **wrong on two
counts** (locked/attempts come *after* freshness, and the documented step list omits the
item lookup). `AGENTS.md:102` also lists log reasons `no-GPS`, `outside-radius`,
`missing-required`, `missing-bonus` that **do not exist** anywhere in `app/main.py` — stale
documentation from the removed GPS/radius schema.

**[V]** Thumbnails are generated (step 7) *before* the freshness gate, so a too-old photo
still costs a full decode + two resizes before being discarded (`app/main.py:640` vs `664-670`).

**[V]** The response is JSON, not a redirect: `{"message": ..., "redirect": "/result?..."}`
(`app/main.py:604`, `608-611`, etc.). The browser follows `redirect` client-side
(`templates/hunt_dashboard.html:157-158`, `templates/base.html:144`).

### 1.2 The scoring formula, exactly

`app/main.py:706-714`:

```python
all_required_met = (
    review["is_target"]
    and review["confidence"] >= CONFIDENCE_THRESHOLD      # 0.7
    and len(review["missed_required"]) == 0
)
all_bonus_met = all_required_met and len(review["missed_bonus"]) == 0 and len(bonus) > 0
awarded_bonus = item.bonus_points if all_bonus_met else 0
points_awarded = item.points + awarded_bonus if all_required_met else 0
is_locked = all_required_met
```

- `CONFIDENCE_THRESHOLD = 0.7` (`app/main.py:51`), `MAX_ATTEMPTS_PER_ITEM = 10` (`:50`).
- `verified` (the DB flag) is set to `all_required_met` (`app/main.py:743`), **not** to
  `all_bonus_met`. So a verified submission may have `awarded_bonus == 0`.

**Every place points are computed or summed:**

1. **Award** — `app/main.py:713` (submit) and `app/main.py:282` (admin re-review). Identical formula.
2. **Per-item bonus** — `app/main.py:712` / `:281`.
3. **Dashboard total** — `app/main.py:336`:
   `total_points = sum(s["points_awarded"] for s in item_state.values() if s["locked"])`.
   `item_state` is built at `:325-334` from that team's submissions, one entry per item.
4. **Dashboard maximum** — `app/main.py:337`:
   `max_points = sum(item.points + item.bonus_points for item in items)`.
   **Caveat [V]:** `items` comes from `select(HuntItem)` with **no hunt filter**
   (`app/main.py:317`). If more than one hunt YAML has been loaded, the dashboard lists and
   sums items from *all* hunts, and `progress` (`:338`) is computed against that inflated max.
5. **Dashboard progress** — `app/main.py:338`.
6. **Leaderboard total** — `app/main.py:356-362`:
   `select(Team.name, func.sum(Submission.points_awarded)) … .where(Submission.verified == True).group_by(Team.name)`.
   Inner join → teams with no verified submission are absent.
7. **Gallery / admin gallery** display `submission.points_awarded` directly
   (`templates/gallery.html:39`, `templates/admin_all_gallery.html:45`).
8. **Admin re-review** can *change* `points_awarded` and `verified` on an existing row
   (`app/main.py:286-290`), which retroactively changes the leaderboard sum. It does **not**
   re-check attempts or the lock state, and it can set `verified=False` on an approved row
   (`:289`), which unlocks the item for the team on the next submit.

### 1.3 Auth / session model

- One signed cookie session via Starlette `SessionMiddleware`, keyed by `_SESSION_SECRET`
  (`app/main.py:42`). If `SESSION_SECRET` is unset a random per-process key is generated
  (`app/main.py:28-31`), so every restart invalidates all sessions **[V]**.
- **Team login**: `POST /team/login` matches the raw passphrase string against the `Team`
  table (`app/main.py:302-310`) and stores `request.session["team_id"] = team.id` (`:309`).
  Passphrases are stored and compared in plaintext (no hashing) **[V]**.
- **Admin login**: `POST /admin/login` compares `password.strip()` to
  `get_admin_password()` and stores `request.session["is_admin"] = True`
  (`app/main.py:141-146`). Password comes from `admin_config.yaml` (`app/config.py:31-32`).
- **Guards**: `require_admin` (`app/main.py:105-107`) checks `is_admin == True`;
  `require_team` (`app/main.py:110-112`) checks `team_id` truthiness. Both raise HTTP 403.
- **Identity carried** purely in the signed cookie; there is no server-side session store
  **[V]**.
- **Auth coverage [V]:** `require_team` is on `/hunt/dashboard` (`:314`), `/gallery` (`:404`),
  `/submit` (`:600`). `require_admin` is on every `/admin/*` route (`:149,169,198,243,258,437,442,451,461,467,503`).
  **`/leaderboard` (`app/main.py:353-354`) has no auth dependency** — despite `AGENTS.md`
  listing it under "Team (auth: team_id in session)". It is publicly reachable and, once the
  hunt is closed, exposes every approved photo plus team names and scores (`:367-392`).
  `/result` (`:528`) is also unauthenticated.
- **No CSRF token anywhere** (`grep` for `csrf|token` found only `max_tokens` in
  `app/ai_review.py:117`) **[V]**. Starlette's `SessionMiddleware` defaults to
  `same_site="lax"`, which blocks cross-site POST cookies **[I]** — so the CSRF exposure is
  mitigated by the library default, not by anything in this repo.

### 1.4 Image pipeline

- Upload dirs: `uploads/originals`, `uploads/thumb`, `uploads/display`
  (`app/config.py:8-11`), created at import (`:15-20`). Three separate `StaticFiles`
  mounts at `/uploads/{originals,thumb,display}` (`app/main.py:44-46`).
- Sizes: thumb `200×200` LANCZOS q85; display `1200×1200` LANCZOS q90; both forced to
  JPEG (`app/main.py:581-596`).
- Stored: original bytes verbatim (`:637-638`); paths recorded on the row
  (`:740-742`; columns `app/models.py:42-44`).
- Gallery URL: derived by stripping the extension from `thumbnail_path`
  (`_thumb_base`, `app/main.py:124-125`) and appending `.jpg` (`:390-391`, `:423-424`).
- HEIC: `pillow_heif.register_heif_opener()` at import (`app/main.py:22-24`), so HEIC opens
  via Pillow; `review_image` re-encodes to JPEG before the API call
  (`app/ai_review.py:47-57`).
- **EXIF is dropped from derived images [V]:** `make_thumbnails` saves without an `exif=`
  argument (`app/main.py:592,596`) and `_image_to_base64_jpeg` likewise (`app/ai_review.py:56`).
  Only `uploads/originals/` retains metadata.
- **Orientation is not applied [V]:** no `ImageOps.exif_transpose` / `Orientation` handling
  exists (grep across `app/` returned nothing). Thumbnails of rotated phone photos will
  render sideways. *(Inference: this is the standard symptom of the missing call.)*
- Cleanup: `_cleanup_files` on every rejection path (`app/main.py:115-121`, called at
  `:650,667,685,696,733,771,776`); `/admin/cleanup-orphans` deletes DB-unreferenced files
  (`:169-195`); `/admin/reset` drops tables and empties all three dirs (`:149-166`).

### 1.5 Config surface

`app/config.py` — all read from `admin_config.yaml` (relative path, `:25`) except
`DATABASE_URL` (`:6`):

| Function | Key(s) | Default | Line |
|---|---|---|---|
| `get_admin_password` | `admin_password` | `""` | `:31-32` |
| `get_openai_config` | `openai_api_key`, `openai_model`, `openai_base_url` | `gpt-4o-mini`, OpenAI URL | `:35-41` |
| `get_max_photo_age_hours` | `max_photo_age_hours` | `4.0` | `:44-45` |
| `get_hunt_state` | `hunt_ends_at`, `hunt_duration_minutes` | `160` | `:48-89` |
| `set_hunt_ends_at` | rewrites `hunt_ends_at` in place | — | `:92-113` |

`OPENAI_API_KEY` env var overrides the YAML key (`app/config.py:38`). `get_hunt_state` is
called on **every** rendered page via `_ctx` (`app/main.py:69-79`) and again in `/submit`
(`:606`), and each call re-opens and re-parses `admin_config.yaml` (`app/config.py:23-28,57`)
**[V]**. `set_hunt_ends_at` rewrites the file line-by-line, preserving comments (`:92-113`).

---

## 2. EXIF handling — precise

### 2.1 Exactly which fields are read, by which function

Only **one** function reads EXIF: `get_photo_taken_at(img)` (`app/main.py:537-578`). It
reads:

- **Timestamp**, first hit of `36867` (DateTimeOriginal), `36868` (DateTimeDigitized),
  `306` (DateTime), via `exif.get(tag_id)` (`app/main.py:548-554`).
- **`0x9010` (OffsetTimeOriginal)** from the Exif sub-IFD, via
  `exif.get_ifd(ExifTags.IFD.Exif).get(0x9010)` (`app/main.py:559-560`).

```python
# app/main.py:542-560
    from PIL import ExifTags
    exif = img.getexif()
    if not exif:
        return None

    taken_at = None
    for tag_id in (36867, 36868, 306):
        val = exif.get(tag_id)
        ...
    exif_sub_ifd = exif.get_ifd(ExifTags.IFD.Exif) or {}
    offset_str = exif_sub_ifd.get(0x9010)
```

No other EXIF tag is read anywhere. The returned value is used only for the freshness gate
(`app/main.py:654-670`); it is **never stored** on the submission.

### 2.2 Is GPS read anywhere? **No.**

**[V]** There is no reference to GPS anywhere in `app/`: no `GPSInfo`, no `34853`,
no `0x8825`, no `get_ifd(ExifTags.IFD.GPSInfo)`. `app/models.py` has no latitude/longitude
columns (`Hunt` `:8-13`, `HuntItem` `:16-27`, `Team` `:30-34`, `Submission` `:37-50`).
`hunts/joshua_tree.yaml` has no coordinates. `AGENTS.md:59` states plainly: *"AI is the sole
gate — no GPS/radius check. Old schema had lat/lon/radius_meters/location; removed."* That
statement is accurate against the code.

GPS is physically present in uploaded originals (I reproduced a GPS sub-IFD being readable
from a JPEG in §2.3) — the server simply never looks at it.

### 2.3 The timestamp path is broken for the standard EXIF tag — reproduced

This is the most consequential finding for the proposed work, so I reproduced it.

Pillow's `Exif.get`/`__getitem__` only consults IFD0 (`_data` + `_info`); it does **not**
merge the Exif sub-IFD (`PIL/Image.py:3920-3924` in Pillow 10.2.0). `DateTimeOriginal`
(36867) and `DateTimeDigitized` (36868) live in the **Exif sub-IFD**, so `exif.get(36867)`
returns `None`. Only `DateTime` (306), which is an IFD0 tag, is reachable this way.

Running the repo's function verbatim against crafted JPEGs:

| Case | EXIF content | `get_photo_taken_at` result |
|---|---|---|
| A | only `DateTimeOriginal=2024:06:01 09:30:00`, `OffsetTimeOriginal=-07:00` | **`None`** → submission rejected as "no EXIF timestamp" |
| B | no EXIF | `None` → rejected |
| C | EXIF present, only `Orientation` | `None` → rejected |
| D | IFD0 `DateTime=2024:06:01 09:30:00` only | `datetime(2024,6,1,9,30)` (naive) |
| E | IFD0 `DateTime=2024:01:01 00:00:00` **and** sub-IFD `DateTimeOriginal=2024:06:01 09:30:00` | picks **`2024-01-01 00:00:00`** (the wrong tag) |

And with both tags present *plus* `OffsetTimeOriginal`, the offset is applied to the
IFD0 `DateTime` value, not to `DateTimeOriginal`:
`DateTime=2020:01:01 10:00:00` + `OffsetTimeOriginal=+05:00` → `2020-01-01 05:00 UTC`.

**Consequences [V]:**
1. The `DateTimeOriginal` branch (`app/main.py:548`) is effectively dead for normal camera
   files; the code silently falls through to `DateTime` (306) — the IFD0 file-datetime,
   which is a *different* semantic value.
2. A photo whose only timestamp is `DateTimeOriginal` (very common) is **rejected** with
   "Photo has no EXIF timestamp" (`app/main.py:649-652`), even though it has one.
3. `OffsetTimeOriginal` — the offset **of `DateTimeOriginal`** — is applied to whatever tag
   was actually read (`app/main.py:560,574`). That mixes the timezone of one tag with the
   wall-clock of another, shifting the computed instant by up to ±14 h. This can flip the
   freshness decision in either direction.
4. `README.md:136` and `AGENTS.md:65` both assert the server reads `DateTimeOriginal` and
   interprets it as UTC when the offset is present. **The code does not do that.** The docs
   describe intended behaviour, not actual behaviour.

**[I]** This looks like a migration bug: the old Pillow API `Image._getexif()` returned a
dict that merged the Exif sub-IFD (so `_getexif()[36867]` worked), whereas the newer
`Image.getexif()` does not. The offset lookup at `app/main.py:559` correctly uses
`get_ifd(...)`, which is the tell that the author knew about sub-IFDs but not that `get()`
doesn't reach into them.

**[?]** `pillow_heif` is not installed here, so I could not test whether HEIC files expose
EXIF differently (some HEIC decoders flatten the Exif IFD). If they do, HEIC and JPEG
submissions would take different code paths.

### 2.4 What is discarded, and when

- **From derived images [V]:** thumbnails/display (`app/main.py:592,596`) and the AI copy
  (`app/ai_review.py:56`) are saved without EXIF, so all metadata (including GPS and
  orientation) is dropped at save time.
- **From the original [V]:** nothing — the raw upload is stored byte-for-byte
  (`app/main.py:637-638`). Any GPS in the original is still on disk; it is just never read.
- **Rejected uploads [V]:** all three files are deleted (`_cleanup_files`,
  `app/main.py:115-121`), so a rejected photo's GPS never persists.

### 2.5 What is already broken by a photo lacking EXIF

**[V]** (per §2.3): no-EXIF, EXIF-without-timestamp, and *only-`DateTimeOriginal`* photos are
all rejected outright at `app/main.py:649-652`. A photo with IFD0 `DateTime` but no offset is
treated as server-local time (`app/main.py:655-660`); if the phone and server disagree on
timezone, the computed age is wrong by that offset, which can produce false "too old"
rejections or future-timestamp warnings (`:672-674`).

---

## 3. Anti-cheat, as it exists

### 3.1 The photo-age gate

- Threshold: `get_max_photo_age_hours()` (`app/config.py:44-45`), default `4.0`.
- Mechanism: EXIF timestamp vs server clock, with the aware/naive split described in §1.1
  step 10-11 (`app/main.py:654-670`).
- It **depends entirely on EXIF**, which the client controls byte-for-byte. Any EXIF editor
  (or a re-encode that sets `DateTime`) defeats it. And per §2.3, the tag it actually reads
  is the *file* datetime, not the *capture* datetime — so the gate is both forgeable and
  semantically wrong.

### 3.2 Attempt counter and locking

- `MAX_ATTEMPTS_PER_ITEM = 10` (`app/main.py:50`), enforced by counting all submissions for
  (team, item) (`:689-698`), and re-checked inside the insert transaction to close the race
  opened by the AI call (`:716-735`). **[V]**
- Locking: any `verified` submission for (team, item) blocks further submits
  (`:676-687`), also re-checked (`:718-724`). `attempt_number` is stored (`:747`). **[V]**
- The AI is the **sole content gate** (`app/main.py:706-714`); no GPS, radius, or human review
  in the path. **[V]**

### 3.3 Other integrity controls present

- Team-session/`team_id` match (`app/main.py:601-604`).
- Global hunt timer gates `/submit` (`:606-611`, `app/config.py:48-89`).
- Admin re-review for disputes (`:258-297`).
- Server-side only: the client never computes points; scoring is recomputed from the AI
  response server-side. **[V]**

### 3.4 What stops cheating today, and what does not

**Does:** the attempt cap + lock (limits brute-forcing the AI); the hunt timer; the team
session match (you can't post as another team via the form field alone).

**Does not [V]/[I]:**
- Any EXIF-based check (age) — client-controlled and, per §2.3, reading the wrong tag.
- Nothing verifies *where* a photo was taken — there is no coordinate check at all
  (`AGENTS.md:59`).
- No rate limiting on `/team/login` (`app/main.py:302-310`); passphrases are plaintext and
  shared per team → online guessing **[I]**.
- `/leaderboard` is public (`app/main.py:353-354`) — results/photos leak without login.
- No CSRF token (`app/main.py` has none); mitigated only by the session cookie's
  `SameSite=Lax` default **[I]**.
- `/admin/review-submission/{id}` can un-verify an approved row (`:289`), silently changing
  the leaderboard — no audit trail beyond the AI JSON blob.
- Upload filename is used verbatim in the path (`app/main.py:631-632`); a filename containing
  `../` could escape `ORIGINALS_DIR` if the target directory exists **[I]** — not verified
  as exploitable here.

---

## 4. Integration design (design only — no code written)

### 4.1 (a) Coordinate judging

**Goal:** keep the existing required/bonus AI model intact and add proximity as a *separate,
additive* signal — never as a silent gate.

**Proposed schema change** (new columns; **requires `/admin/reset`** — `AGENTS.md:60-61`,
`README.md:185-191`, because `init_db` only calls `create_all`, `app/database.py:10-12`):

`HuntItem` (`app/models.py:16-27`) add:
- `target_lat` (Float, nullable), `target_lon` (Float, nullable) — the target coordinate.
- `radius_meters` (Float, default 100) — "exact hit" ring.
- `proximity_points` (Integer, default 0) — points for an exact hit.
- `proximity_mode` (String, default `"bonus"`) — `"off" | "bonus" | "gate"`.
- `near_multiplier` (Float, default 2.0) — outer ring = `radius_meters × near_multiplier`.

`Submission` (`app/models.py:37-50`) add:
- `gps_lat`, `gps_lon` (Float, nullable), `gps_accuracy_m` (Float, nullable).
- `distance_m` (Float, nullable), `location_status` (String) — one of
  `hit | near | far | missing | no_target`.
- `proximity_points_awarded` (Integer, default 0).

`hunts/*.yaml` item block gains the same keys (`target_lat`, `target_lon`,
`radius_meters`, `proximity_points`, `proximity_mode`, `near_multiplier`), loaded in
`load_hunts` next to the existing fields (`app/main.py:226-235`). Keep `required_properties`
mandatory (`:221-225`) so the load contract is unchanged.

**Judging, blended without breaking the AI model:**

- Parse GPS **server-side from the stored original** (Pillow `get_ifd(ExifTags.IFD.GPSInfo)`,
  the same mechanism I used to read the GPS IFD in §2.3) — never from a multipart field.
- `ai_points` = existing `item.points + awarded_bonus` when `all_required_met`
  (`app/main.py:706-713`) — **unchanged**.
- `d = haversine(photo, target)`:
  - `d <= radius_meters` → `proximity_points_awarded = item.proximity_points`, status `hit`.
  - `radius_meters < d <= radius_meters × near_multiplier` → half, status `near`.
  - `d >` outer ring → `0`, status `far`.
  - no GPS → `0`, status `missing`.
  - no target configured → `0`, status `no_target`.
- `points_awarded = ai_points + proximity_points_awarded` (extend `app/main.py:713`).
- **Gate vs bonus:** recommend `"bonus"` (proximity adds points, never blocks the AI
  approval). Reserve `"gate"` for items where location *is* the challenge: then
  `verified = all_required_met and status in {hit, near}` and the response must say exactly
  why. Do **not** silently zero out a valid AI approval.
- **No-GPS must not fail silently [V requirement]:** always persist `location_status`, always
  include a human-readable line in the `/result` message (the `redirect` at `app/main.py:752-767`),
  surface it in `admin_all_gallery`, and emit a distinct `uvicorn.error` log line (the pattern
  at `app/main.py:668-669`). A missing GPS must read as "Location unavailable — no proximity
  points" — not as a generic rejection.
- Update the dashboard max to include `proximity_points` (`app/main.py:337`) and the
  per-item state to carry `location_status` (`:325-334`).
- **Hunt scoping bug to fix while here [V]:** `select(HuntItem)` at `app/main.py:317` has no
  hunt filter, so multi-hunt installs already mix items. Adding coordinates makes this worse;
  filter by the active hunt.

**Distance function:** implement haversine (or the equirectangular approximation) server-side.
The repo has no geospatial dependency (`package.json:5-14`); a ~10-line haversine avoids adding
one. The reference project ships exactly such a function (`beakon/static/js/map.js:44-56`),
which is a fine algorithm reference — not code to copy into a Python app.

### 4.2 (b) The map

**Pattern to borrow from beakon [V]:** one grouped JSON endpoint returning everyone who is
permitted, per-person colour, a legend with show/hide toggles, a 10 s poll, a rebuild-from-
scratch render so disappeared entities vanish, and `visibilitychange` pausing
(`beakon/static/js/shared_map.js:31-51,53-100,162-213`; `beakon/src/app.py:855-912`).
Fail-closed on error (`beakon/src/app.py:908-912`). Leaflet is vendored under `static/`
(`beakon/templates/layout.html:11,14`; `beakon/static/leaflet/`).

**Endpoints (new):**

- `GET /api/map` — team-auth (`Depends(require_team)`), same-origin cookie like every other
  page (the existing fetch in `templates/base.html:132-135` already sends the session cookie
  same-origin).
  ```json
  {
    "items": [{"id":3,"name":"Cholla Cactus","lat":34.01,"lon":-116.16,
               "radius_meters":120,"points":100,"bonus_points":50}],
    "teams": [{"id":1,"name":"Team Alpha","color_index":0,
               "latest":{"lat":34.02,"lon":-116.15,"timestamp":"3 minutes ago",
                          "age_seconds":187,"item_name":"Cholla Cactus",
                          "location_status":"hit"},
               "trail":[{"lat":34.02,"lon":-116.15,"submitted_at":"..."}]}]
  }
  ```
- `GET /admin/map` — admin-auth (`Depends(require_admin)`), same payload plus unapproved
  submissions.
- Keep `/api/map` **separate** from `/leaderboard`; do not extend the public route.

**What is plotted:** item targets as `L.circle` sized by `radius_meters` (with a marker at the
centre), one team marker at its latest GPS fix, and a polyline trail of its submission
coordinates. Colour by golden-angle index (`beakon/static/js/shared_map.js:48-51`).

**Refresh:** `setInterval(..., 10000)` with a rebuild each poll and a `visibilitychange` pause
(mirroring `beakon/static/js/shared_map.js:35,192-213`). Include `age_seconds` server-side so
the client can dim stale positions without parsing strings
(`beakon/src/app.py:896-897`).

**Auth model:** every map endpoint requires a session (`require_team` / `require_admin`,
`app/main.py:105-112`). Fail closed: on error return an empty payload plus a message, never
"show everything". Note `/leaderboard`'s current public exposure (`app/main.py:353-354`) —
team positions are more sensitive than scores and must not follow it.

**Offline note:** AGENTS.md claims the UI works with no cell signal because Foundation/jQuery
are vendored. Leaflet JS/CSS can be vendored the same way, **but the base map tiles still come
from a tile server over the network** **[I]** — for a desert hunt that is a real limitation.
Options: pre-cache an offline tile area, or accept a degraded "no basemap, markers on a blank
canvas" mode.

### 4.3 (c) The Android companion — what it must do

Summary; the mechanics are in §5.

1. Declare **and runtime-request** `ACCESS_MEDIA_LOCATION` (API 29+).
2. Prefer **in-app capture** (CameraX) writing to a file the app owns, and attach the location
   at capture time; the resulting JPEG keeps GPS and needs no special permission to re-read.
3. If picking from the gallery, resolve the `content://media/...` URI through
   `MediaStore.setRequireOriginal(uri)` before reading.
4. Read GPS with `ExifInterface.getLatLong()`; if it returns `false`, upload anyway and let the
   server mark `location_status = missing`.
5. Upload the **original bytes** unmodified (multipart `file`), matching the existing
   `/submit` contract (`app/main.py:599-600`): fields `item_id`, `team_id`, `file`.
6. Handle the JSON `{message, redirect}` response exactly as the web client does
   (`templates/hunt_dashboard.html:150-166`).
7. Never treat client coordinates as authoritative; the server parses GPS from the bytes.

---

## 5. Android EXIF mechanics

### 5.1 Why Android strips location EXIF from photos a web page receives

**[I] (platform behaviour, not repo code):** the stripping happens in the **OS media
provider**, not in the browser and not in the page. Since Android 10 (API 29) scoped storage,
`MediaProvider` redacts location metadata when it serves an image to a process that does not
hold `ACCESS_MEDIA_LOCATION`. When a web page uses `<input type="file">` and the user picks a
gallery image, the browser (a process without that permission — and a web page cannot request
it) opens the `content://media/...` URI through the provider and receives a **redacted** stream
with the GPS tags removed. The bytes that reach JavaScript therefore already lack GPS.

Layers that can *additionally* strip metadata:
- A camera app invoked without `EXTRA_OUTPUT` returns only a small thumbnail bitmap with no EXIF.
- Any intermediate app that re-encodes (share sheets, "resize for messaging").
- Canvas/JS re-encoding in a page. **Not a factor here:** the web client appends the raw
  `File` object straight into `FormData` (`templates/hunt_dashboard.html:137-145`,
  `templates/base.html:127-135`) — no canvas, no re-encode. So on desktop the original bytes
  (with EXIF) are uploaded; on Android the redaction already happened upstream.

### 5.2 What `ACCESS_MEDIA_LOCATION` grants, and `MediaStore.setRequireOriginal(uri)`

- **`android.permission.ACCESS_MEDIA_LOCATION`** (API 29+): a runtime ("dangerous") permission
  that allows an app to read the **un-redacted** location metadata of media in the shared
  `MediaStore`. Declaring it in the manifest is necessary but not sufficient — it must also be
  requested at runtime. It governs MediaStore-served data only; files the app owns directly
  are never redacted and need no permission.
- **`MediaStore.setRequireOriginal(Uri)`** (API 29+): takes a `content://media/...` URI and
  returns a URI that, when opened, yields the **original** bytes including GPS. It requires
  `ACCESS_MEDIA_LOCATION`; without it the read fails (`SecurityException`). It is meaningful
  only for MediaStore URIs — for a FileProvider/file URI of a file the app wrote, it is
  irrelevant.
- When each is required: use `setRequireOriginal` **only on the gallery/picker path**, where
  the bytes come from MediaStore. The in-app-capture path to an app-owned file needs neither.

### 5.3 In-app capture vs picking from the gallery

- **In-app capture (CameraX)** — preserves GPS *if the location is attached*. CameraX does not
  add GPS automatically; attach it via the capture metadata
  (`ImageCapture.OutputFileOptions.Builder(...).setMetadata(new Metadata().setLocation(location))`,
  camera-core 1.1+) or `Camera2Interop` `JPEG_GPS_LOCATION`. Because the app writes/owns the
  file, reading it back needs no `ACCESS_MEDIA_LOCATION`. **This is the recommended path.**
- **`ACTION_IMAGE_CAPTURE`** — with `EXTRA_OUTPUT` pointing at a URI you own (FileProvider),
  the camera writes a full-resolution JPEG with EXIF including GPS (subject to the camera app
  having location enabled). **Without** `EXTRA_OUTPUT`, you get only a thumbnail bitmap in
  `extras["data"]` and no EXIF.
- **Gallery pick** — returns a MediaStore URI; without `ACCESS_MEDIA_LOCATION` the GPS is
  redacted. Use `setRequireOriginal` to recover it.

### 5.4 Reading GPS with `ExifInterface`, and the absent-field case

- Use `androidx.exifinterface.media.ExifInterface`; construct from an `InputStream`
  (`contentResolver.openInputStream(uri)`) or a `File`.
- `getLatLong(float[] out)` returns **`true` if GPS is present**, filling `out[0]=lat`,
  `out[1]=lon`; it returns **`false` when the field is absent** and leaves the array unchanged.
  `getAttribute(TAG_GPS_LATITUDE)` returns `null` in that case.
- Because historical versions had cases where `getLatLong` returned `true` with `NaN`, validate
  the returned values against valid ranges before use.
- **Absent field handling:** do not block the upload. Send the photo, omit the coordinate
  fields (or send null), and let the server record `location_status = missing`. This satisfies
  the "must not silently fail" requirement from §4.1(a).
- `ExifInterface` does not take a `content://` URI directly — open a stream first. HEIF EXIF
  reading is supported on API 28+.

### 5.5 What the app must send, and how the server must distrust it

- **Send:** the original JPEG/HEIC bytes as the multipart `file` field — no re-encode, no
  metadata stripping. Optionally also send `lat`/`lon`/`accuracy` as separate fields for
  display/debug.
- **Trust model [I]:** everything a client sends is forgeable. EXIF inside a client-supplied
  file can be edited with any EXIF tool, and multipart fields are trivially spoofed. Therefore:
  1. Parse GPS **server-side from the stored original** and treat that as the only coordinate
     value — never a multipart field.
  2. Validate ranges (lat ∈ [-90,90], lon ∈ [-180,180], accuracy sane) and reject NaN/absurd values.
  3. Treat proximity as an **additive bonus**, not the sole gate (except for explicitly
     gate-mode items), so a forged/missing coordinate cannot silently deny a legitimate photo
     and a forged one cannot by itself award the hunt.
  4. Log every coordinate with team/item (the `uvicorn.error` pattern at `app/main.py:668-669`).
  5. Cross-check the EXIF timestamp against the server clock (already done, `:654-670`) and
     consider outlier/cluster checks across teams for high-value items.
  6. Never expose a public endpoint that reveals live positions (`/leaderboard` is currently
     public — `app/main.py:353-354`).

### 5.6 Web UI to replicate for parity (from `templates/`)

**[V]** Reading `templates/`:

| Surface | Template | Must replicate |
|---|---|---|
| Team login | `index.html:14-18` | passphrase form → `POST /team/login` |
| Dashboard | `hunt_dashboard.html:44-99` | item list (name, description, `bonus_hint` `:51-56`), base `points` `:60` and `bonus_points` `:63-68`, per-item state: Locked `:71-75` / Max attempts `:76-80` / Hunt closed `:81-85` / Submit `:86-94` |
| Dashboard progress | `hunt_dashboard.html:13-17` | progress bar from `progress` |
| Countdown | `hunt_dashboard.html:20-23,173-197` | live countdown from `hunt_state.seconds_remaining`; reload at zero |
| Upload UX | `hunt_dashboard.html:102-107,136-170` | overlay "Analyzing your photo with AI…"; multipart `item_id`+`team_id`+`file`; follow `redirect` |
| Result | `result.html:8-24` | success/failure callout + message + auto-return |
| My gallery | `gallery.html:16-56` | grid of thumb, "Not approved" / bonus badge, points, hint, attempt no., date; tap → display image modal `:59-70` |
| Leaderboard | `leaderboard.html:14-48` | ranked list, gold/silver/bronze, team + points |
| Post-hunt gallery | `leaderboard.html:50-121` | all approved photos across teams + "Why this score?" AI reasoning (`reason`, `matched_required`, `matched_bonus`) |
| Chrome | `base.html:86-109,12-23` | nav (Dashboard/Gallery/Leaderboard/Logout), theme colours |

Admin surfaces (`admin_dashboard.html`, `admin_hints.html`, `admin_all_gallery.html`,
`admin_login.html`) are probably out of scope for a hunter companion **[I]**.

---

## 6. Risks and unknowns

### 6.1 Verified risks (read in code, or reproduced)

1. **Timestamp bug (§2.3)** — `DateTimeOriginal` is unreachable via `exif.get()`
   (`app/main.py:548`); the code silently uses IFD0 `DateTime` and can apply
   `OffsetTimeOriginal` to the wrong tag (`:560,574`). Photos with only `DateTimeOriginal` are
   rejected. Any coordinate work that also trusts the timestamp inherits this.
2. **Docs contradict code** — `README.md:136`, `AGENTS.md:62,65,102` describe ordering and
   fields that do not match `app/main.py`.
3. **No GPS anywhere** — `app/models.py` has no coordinate columns; the AI is the only gate.
4. **Schema change needs a destructive reset** — `init_db` only `create_all`s
   (`app/database.py:10-12`); adding columns requires `/admin/reset` + reload
   (`AGENTS.md:60-61`, `README.md:185-191`).
5. **Multi-hunt bleed** — dashboard items and `max_points` come from `select(HuntItem)` with no
   hunt filter (`app/main.py:317,337`).
6. **`/leaderboard` is unauthenticated** (`app/main.py:353-354`) though docs say otherwise;
   adding a live-position map next to it would be a privacy leak.
7. **Admin re-review can un-verify** an approved row and change scores with no audit trail
   (`app/main.py:286-290`).
8. **EXIF stripped from derived images** (`app/main.py:592,596`, `app/ai_review.py:56`) — only
   `uploads/originals/` can ever be a GPS source.
9. **Orientation not applied** — no `exif_transpose`; thumbnails may be sideways.
10. **Per-request config file I/O** — `get_hunt_state()` re-reads `admin_config.yaml` on every
    page (`app/main.py:77`, `app/config.py:57`).
11. **Plaintext secrets** — team passphrases in DB (`app/models.py:34`, compared at
    `app/main.py:305`), admin password in YAML; no login rate limiting.
12. **Filename used verbatim in path** (`app/main.py:631-632`) — traversal risk **[I]**.

### 6.2 Regressions / migration that the new work will need

- Adding `HuntItem`/`Submission` columns → **drop + recreate + reload** (§6.1.4). Any GPS/target
  data on existing rows is lost.
- Changing `points_awarded` to include proximity affects historical rows only if you backfill;
  otherwise old submissions keep the old formula and the leaderboard mixes two formulas.
- Adding `Depends(require_team)` to new map endpoints is additive; do **not** tighten
  `/leaderboard` in the same change unless intended (it may break existing links).
- Android app must handle the JSON `{message, redirect}` contract, not an HTTP redirect
  (`app/main.py:604`).

### 6.3 What I could not determine from the repo alone

- **[?]** Actual runtime Pillow version — `venv/` is absent; I used system Pillow 10.2.0. The
  pin is `^10.0.0` (`package.json:11`), which is consistent, but a different minor could change
  sub-IFD behaviour (unlikely for the `get()` semantics above).
- **[?]** `pillow_heif` behaviour for HEIC EXIF (not installed).
- **[?]** Whether the owner intends proximity as a gate or a bonus, and the desired tier values
  — a product decision, not derivable.
- **[?]** Tile provider / API key for the map (beakon uses a Mapbox key from config,
  `beakon/src/app.py:1144`; scavenge has no map config at all).
- **[?]** Whether the hunt has cell service where teams play (affects live-position feasibility).
- **[?]** Whether the Android app must work fully offline (consistent with the vendored-assets
  design in `AGENTS.md`), which would rule out live tile loading.
- **[I]** SameSite/CSRF behaviour depends on Starlette's `SessionMiddleware` default, which I
  could not import here (`fastapi`/`starlette` not installed); the repo sets no `same_site`
  (`app/main.py:42`), so the library default governs.
- **[I]** All Android platform statements in §5 are from platform/API knowledge, not from any
  file in this repo.

---

## 7. One-paragraph summary

Scavenge is a small async FastAPI app whose only content judge is an OpenAI-compatible vision
model (`app/ai_review.py:60-152`); points are `item.points + bonus_points` when the AI approves
with confidence ≥ 0.7 and no missed required properties (`app/main.py:706-714`). It reads no
GPS and stores no coordinates. Its one EXIF read (`app/main.py:537-578`) is **broken for the
standard `DateTimeOriginal` tag** — verified by reproducing the function against Pillow 10.2.0 —
because `Exif.get()` does not reach into the Exif sub-IFD; it silently falls back to IFD0
`DateTime` and can misapply `OffsetTimeOriginal` to it. Adding coordinate judging is therefore
best done as an **additive proximity bonus** parsed server-side from the stored original, with
an explicit `location_status` so a missing GPS never fails silently; the map should be a
beakon-style 10 s-polled Leaflet view behind team/admin auth, never on the public
`/leaderboard`; and the Android companion must either capture in-app (CameraX, location attached,
app-owned file) or resolve gallery URIs through `MediaStore.setRequireOriginal()` under
`ACCESS_MEDIA_LOCATION` — because on Android 10+ the OS media provider, not the browser, is what
strips GPS from what a web page receives.
