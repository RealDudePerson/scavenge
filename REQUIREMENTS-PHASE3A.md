# REQUIREMENTS — Phase 3a: JSON API for the hunter surface

Repo: `~/projects/scavenge`. Phase 2 committed (`c4d2ae9`), tree clean, 103 tests passing.

**Why this phase exists.** The app is a Jinja2 HTML application. A native Android client
cannot consume rendered HTML — it needs JSON. This phase adds a JSON API mirroring the
hunter surface. It is the prerequisite for phase 3b (the Android app) and ships
independently: it changes nothing about the existing HTML flows.

**Scope: server-side only.** Do not build any Android code here.

---

## 1. Endpoints to add

All are **team-authenticated** (existing session cookie), and all **fail closed** —
unauthenticated requests get 403, never partial data.

### `POST /api/login` — JSON
Body `{"passphrase": "..."}` → `{"team_id": int, "team_name": "..."}`.
Reuse the existing login logic and the phase-1 rate limiter. A wrong passphrase → 401
with a JSON error body (not an HTML redirect). This is what the app calls instead of
the form-based `/team/login`.

### `GET /api/items` — JSON
The active hunt's items, each with: `id`, `name`, `description`, `points`,
`bonus_points`, `bonus_hint`, and this team's state for that item
(`locked`, `attempts`, `points_awarded`, `has_bonus`) — the same information the
dashboard template renders.

**Must not include `target_lat`, `target_lon`, or `radius_meters`.** This is team-facing,
and the same rule as phase 2 §1 applies: leaking the target lets a player walk straight
to the spot. `zone_*` may be included (the map already exposes it).

### `POST /api/submit` — multipart
`item_id`, `file`, optional `live_lat` / `live_lon`. The existing `/submit` already
returns JSON (`message`, `redirect`) but is form-bound and redirects on some paths — the
app needs a clean JSON contract. Return:
`{"verified": bool, "points_awarded": int, "proximity_points_awarded": int,
  "location_status": str, "message": str}`.
**Do not duplicate the submission logic** — factor the existing handler so both routes
share one implementation. The AI review, EXIF parsing, proximity scoring and the
attempt/lock rules must all still apply identically.

### `GET /api/gallery` — JSON
This team's submissions: `item_name`, `thumb_url`, `display_url`, `verified`,
`points_awarded`, `proximity_points_awarded`, `location_status`, `attempt_number`,
`submitted_at`. The photo URLs already work over HTTP
(`/uploads/thumb/<base>.jpg`, `/uploads/display/<base>.jpg`) — reuse them.

### `GET /api/leaderboard` — JSON
Team names and totals, honouring the phase-1 auth fix (this must **not** be public —
that was a defect). Shape: `[{"team_name": str, "points": int}]`.

### `POST /api/position` — accept JSON as well as form
This is the flagged gap from phase 2: it currently reads `request.form()` only. Accept a
JSON body (`{"lat": float, "lon": float, "accuracy_m": float|null}`) **in addition to**
the existing form encoding, so nothing breaks. Keep the range validation, the
"never scored from" rule, and the rate limiter.

## 2. Constraints

- **No duplicated business logic.** The HTML routes and the JSON routes must share the
  underlying functions. A divergence between them is a bug.
- **No change to existing HTML flows** — the browser experience must be untouched.
- Item payloads must not leak the true target (§1).
- Errors return JSON, not HTML, on `/api/*` routes.

## 3. Tests — required and red-capable

Extend the suite. Minimum:

- `/api/login` succeeds with the right passphrase, 401s with the wrong one, and is
  rate-limited like the form route.
- Every new endpoint returns 403 unauthenticated.
- `/api/items` contains the team's item state and **no** `target_lat`/`target_lon`/
  `radius_meters` — assert structurally, as phase 2's leak test does.
- `/api/items` shows a locked item as locked after an approved submission.
- `/api/submit` applies the **same** rules as `/submit`: an approved photo earns points,
  a no-GPS photo still earns AI points with `location_status == "missing"`, and proximity
  requires approval (the phase-1/2 fix must hold on this path too).
- `/api/gallery` returns only this team's submissions.
- `/api/leaderboard` is **not** reachable unauthenticated.
- `/api/position` accepts a JSON body *and* still accepts the form encoding.

Prove red-capability for the `/api/items` target-leak test by making the endpoint emit
the true target, showing the test fail, then restoring it.

## 4. Out of scope
- The Android app (phase 3b).
- Changing judging, scoring, or the map.
- The public mount of `/uploads/originals` (see §5).

## 5. A finding to report, not fix

`app/main.py:48` mounts `/uploads/originals` with **no authentication**, and those files
carry the submission's GPS EXIF. Filenames are `uuid4`-prefixed so enumeration is
impractical, but the exposure is real. **Do not change it in this phase** — report it in
your summary so the owner can decide, and note whether `/uploads/thumb` and
`/uploads/display` carry EXIF (they should not, since they are re-encoded).

## 6. Honesty requirement
Report what you verified versus what you could not. If you deviate, say so and say why.
