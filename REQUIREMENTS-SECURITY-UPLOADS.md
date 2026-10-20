# REQUIREMENTS — Security: remove the unauthenticated originals mount

Repo: `~/projects/scavenge` (branch **`master`**), committed at the phase-3b server state.
126 tests passing before you start.

## The issue

`app/main.py:48` serves full-resolution submission originals over HTTP with **no
authentication**:

```python
app.mount("/uploads/originals", StaticFiles(directory="uploads/originals"), name="uploads-originals")
```

Those files are the **as-uploaded** bytes, so they carry the submission's **GPS EXIF** —
the player's location. Filenames are `uuid4`-prefixed, so enumeration is impractical, but
anyone holding a URL can fetch a full-resolution original including its coordinates.

## The fix

**Remove the mount.** Verified: `/uploads/originals` has **no consumer anywhere** — nothing
in `templates/`, nothing in `static/`, nothing in `app/main.py` other than the mount itself,
and nothing in the Android client (`~/projects/scavenge-android`). Removing it closes the
exposure with no functional regression.

Originals stay on disk for diagnostics; they are simply no longer reachable over HTTP.

Do **not**:
- leave the mount in place behind a "we'll gate it later" comment — remove it;
- change `/uploads/thumb` or `/uploads/display`. Both are actively used by the web gallery
  and the Android app, and both are re-encoded by `make_thumbnails`, so they carry no EXIF.
  They stay as they are.

## Tests

Add to the suite:

- `/uploads/originals/<anything>` is **not** publicly reachable — assert the mount is gone
  (a 404, not a 200). Pick a filename that exists on disk so the test would fail if the
  mount were still serving, rather than passing for the wrong reason.
- **`/uploads/thumb/…` and `/uploads/display/…` still serve** — a guard against over-correcting
  and breaking the gallery on both clients.

Prove red-capability: with the mount restored, the originals test must fail.

## Out of scope

- `parse_gps` accepting `(0,0)` — a separate open issue, do not touch it here.
- Authenticating thumb/display.
- Anything in the Android repo.

## Honesty

Report what you verified and how. State the test count before and after. If you deviate,
say so and why.
