# REQUIREMENTS — Expose hunt open/closed state to the JSON API

Repo: `~/projects/scavenge` (branch **`master`**). 141 tests passing before you start.

## Why

`get_hunt_state()` is currently only injected into the HTML template context
(`app/main.py:84`). No JSON endpoint exposes it, so the Android app cannot tell whether the
hunt is open and cannot disable submission.

The server **already enforces** the gate (`app/main.py:1391` rejects a submission with
"The hunt is not currently open"). This change only surfaces that state to clients so the app
can reflect it *before* the user taps submit.

## The change

Add one team-authenticated endpoint:

```
GET /api/hunt  ->  {"is_open": bool, "ends_at": str|null, "duration_minutes": int, "seconds_remaining": int}
```

Return exactly the dict `get_hunt_state()` already produces (it already has those four keys
— do not invent a new shape). `ends_at` must be JSON-serialisable (ISO 8601 string or null).

**Do not change `/api/items`.** It returns a bare JSON array and the Android client parses it
as one; changing it to an object would break the app.

The endpoint must fail closed: unauthenticated -> 403, like the other `/api/*` routes.

## Tests

- `/api/hunt` returns 403 unauthenticated.
- With `hunt_ends_at` empty (the default) it reports `is_open: false`, `ends_at: null`,
  `seconds_remaining: 0`.
- With `hunt_ends_at` set in the future it reports `is_open: true` and a positive
  `seconds_remaining`.
- With `hunt_ends_at` in the past it reports `is_open: false`.

Prove red-capability for the unauthenticated case.

## Out of scope

Anything in `~/projects/scavenge-android` — that is the next dispatch.

## Honesty

Report the test count before and after, and what you verified.
