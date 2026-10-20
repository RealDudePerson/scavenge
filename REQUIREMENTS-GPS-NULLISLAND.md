# REQUIREMENTS — Treat the null-island GPS sentinel as absent

Repo: `~/projects/scavenge` (branch **`master`**). 128 tests passing before you start.

## The issue

`parse_gps` (`app/main.py:207`) range-validates coordinates but **`(0.0, 0.0)` passes** —
it is inside `lat ∈ [-90,90]`, `lon ∈ [-180,180]`. So a photo whose GPS tags are the
**redaction signature** (present but all zeros, written by Android's `MediaProvider` for any
app without `ACCESS_MEDIA_LOCATION`) is accepted as a real location.

Consequence: the submission is scored as though taken in the Gulf of Guinea —
`location_status = "far"` with a distance of thousands of kilometres — instead of the honest
`location_status = "missing"`. It does **not** cost the player points (bonus mode is
unaffected, and proximity requires approval anyway), but it tells them something false. That
violates this project's standing rule that a missing GPS must be visible and never silently
misrepresented.

`(0.0, 0.0)` is Null Island — open ocean. No hunt is ever held there, so treating it as
absent is safe.

## The fix

In `parse_gps`, treat an exactly-zero coordinate pair as **absent** — return `None`, the same
as no GPS at all. Downstream that already produces `location_status = "missing"` and the
correct "Location unavailable — no proximity points" message, so no other change is needed.

- Check **both** components: only `lat == 0.0 and lon == 0.0` is the sentinel. A photo at
  `0.0, -116.5` (on the equator) or `34.1, 0.0` (on the prime meridian) is a real location and
  must still parse. Do not reject a coordinate merely for containing a zero.
- Log it distinctly, so an operator can tell "no GPS" from "GPS was the redaction sentinel".
  Follow the existing logging style; do not log silently.

Confirm the **NaN** case is already handled and say so — `0/0` denominators yield `NaN`, and
`NaN` fails the range comparison, so it should already return `None`. Verify that rather than
assuming it.

## Tests

- A GPS-tagged image whose tags are the redaction signature (`0/0` → NaN, and `0/1` → `0.0`)
  → `parse_gps` returns `None`.
- End-to-end: submitting such a photo yields `location_status == "missing"`, `gps_lat is
  None`, `distance_m is None`, **and still awards the AI points** — a missing location must
  never deny an otherwise valid photo.
- **A real coordinate containing a zero still parses** — e.g. `(0.0, -116.5)` and
  `(34.1, 0.0)`. This is the over-correction guard.
- A normal non-zero coordinate is unaffected.

Prove red-capability: with the sentinel check removed, the redaction test must fail.

## Out of scope

- The Android app. It already strips the bogus tags from its own uploads; that now becomes
  redundant but stays as defence in depth — **do not change the app**.
- `/uploads/originals` (already fixed) and anything else.

## Honesty

Report the test count before and after, what you verified, and anything you could not.
