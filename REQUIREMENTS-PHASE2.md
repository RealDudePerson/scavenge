# REQUIREMENTS — Phase 2: interactive map with live team tracking

Repo: `~/projects/scavenge`. Phase 1 is committed (`066dc2c`), tree clean, 73 tests passing.
Read `PROPOSAL-map-coordinates-android.md` for the design context.

**Scope: the web map only.** The Android companion is phase 3 — do not build it, and do not
build any Android code here.

## Owner decisions this implements

- **Map contents:** item geofence areas **plus live team tracking**.
- **Geofence ("not perfectly centered"):** the zone that **judges** stays centred on the item's
  true `target_lat`/`target_lon`; only the zone **drawn on the map** is offset, via the
  `zone_lat`/`zone_lon`/`zone_radius_meters` columns added in phase 1. Judging is unchanged.
- **Visibility:** admin sees all teams; a team sees the items and **its own** team only.
- **Tiles:** vendored Leaflet with a blank-canvas fallback when tiles cannot load.

---

## 1. The critical constraint: never leak the true target

Because the drawn zone is deliberately offset from the true target, **the true target must
never reach a team's browser.** If it did, a player could read it out of the JavaScript and
walk straight to the spot — defeating the entire point of the offset.

Therefore:

- `GET /api/map` (team-facing) must send **only** the display zone
  (`zone_lat`, `zone_lon`, `zone_radius_meters`) and **must not** include `target_lat`,
  `target_lon`, or the judging `radius_meters` — in any field, nested object, or comment.
- If an item has no `zone_*` set, the team payload must **omit that item's location entirely**
  (or send an explicit `"zone": null`) rather than falling back to the true target.
- `GET /admin/map` **may** include the true target, since admins author them.
- `radius_meters` (the judging tolerance) is also not for players — it reveals precision.

**This must be covered by a test** that asserts the serialised team payload contains no
`target_lat`/`target_lon` value anywhere, and that it differs from the admin payload.

## 2. Schema

Add a `team_positions` table (phase 1 confirmed a destructive reset is acceptable):

- `id`, `team_id` (FK `teams.id`), `lat`, `lon` (Float), `accuracy_m` (Float, nullable),
  `recorded_at` (DateTime), `source` (String, default `"app"`).

Keep only the latest position per team in the query (or prune old rows) so the table does not
grow without bound during an event. State which approach you chose.

## 3. Endpoints

### `POST /api/position` — team auth
Body: `lat`, `lon`, optional `accuracy_m`. Records the team's live position.

- **Treat as untrusted input.** Validate ranges (`lat ∈ [-90,90]`, `lon ∈ [-180,180]`),
  reject NaN/Inf, coerce non-numeric to a 400. This is client-supplied and forgeable.
- **Never scored from** — consistent with phase 1 §2.5. It is display data for the map only.
- **Rate-limit** it. Phones will report on an interval; pick a generous per-IP limit and
  state the value chosen (the existing login limiter at `app/main.py` is the pattern).

### `GET /api/map` — team auth
Single grouped payload (the beakon pattern — one request, not N+1):

```
{ "items":      [ {id, name, zone:{lat,lon,radius_m}, points} ],   # display zone only
  "me":         {team_id, name, lat, lon, accuracy_m, age_seconds, stale},
  "submissions":[ {item_id, lat, lon, status, age_seconds} ] }     # this team's own
```

### `GET /admin/map` — admin auth
Same shape, plus every team in a `teams` array, plus each item's **true** target, plus
unapproved submissions. Include `age_seconds` numerically so the client can dim without
parsing strings.

All three endpoints fail closed: unauthenticated → 403, never partial data.

## 4. The map page

- `GET /map` — team view. `GET /admin/map` — admin view (route name must not collide with the
  API route; pick a distinct path and state it).
- **Vendor Leaflet locally** into `static/vendor/leaflet/` (JS + CSS + marker images). The app
  must not depend on a CDN at runtime — the hunt may run on a LAN with no internet. Pin a
  specific version and state it.
- Draw: item zones as circles (from the **display** zone), the team's own position, its
  submission pins. Admin view adds all teams and unapproved pins.
- **10 s poll**, full rebuild each poll so entities that disappear vanish (the beakon pattern).
- **Pause polling when the tab is hidden** (`visibilitychange`) and resume on show.
- **Fail closed on error:** on a failed poll, clear the markers and show a visible message —
  never silently show stale data as if current.
- **Dim stale positions** using `age_seconds` (pick and document a threshold).
- Legend with per-entity show/hide toggles.

### Tiles and the offline fallback
- Tile URL configurable in `app/config.py` + `admin_config.yaml`; default to an OSM URL.
- **If tiles fail to load, the page must still work**: markers, zones and legend render on a
  plain background with a visible, non-blocking notice that the base map is unavailable. A
  broken tile server must not produce a blank or broken page.
- This fallback must be exercised in a test (simulate tile load failure) or, if that is not
  feasible, say so explicitly rather than implying it was verified.

## 5. Nav
Add links to the map from the team dashboard and the admin dashboard, so it is reachable
without typing a URL.

## 6. Tests — required and red-capable

Extend the existing suite. Minimum:

- `/api/map` and `/admin/map` and `/api/position` reject unauthenticated requests (403).
- **The team payload leaks no true target** (§1) — the key security test.
- The admin payload *does* include the true target, and differs from the team payload.
- `POST /api/position` rejects out-of-range, NaN and non-numeric coordinates.
- `POST /api/position` is rate-limited.
- A team's `/api/map` shows only its own position, never another team's.
- The map page renders and references the vendored Leaflet assets (no CDN URL in the HTML).
- Positions older than the staleness threshold are marked stale.

Prove red-capability for at least the target-leak test: show it failing when the endpoint is
made to send the true target, then passing when fixed. Report the results honestly.

## 7. Out of scope
- The Android app (phase 3) — but note in your report anything the app will need from these
  endpoints, so phase 3 can be specified accurately.
- Changing the judging rules from phase 1.
- Making positions tamper-proof — they cannot be.

## 8. Honesty requirement
Report what you verified versus what you could not. Do not claim a browser behaviour was
observed if you only asserted on HTML. If you deviate, say so and say why.
