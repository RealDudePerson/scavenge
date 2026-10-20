# REQUIREMENTS — Phase 3b: Android companion app

Server: `~/projects/scavenge` at commit `3254bc6` (phase 3a complete, 126 tests passing).
New project: **`~/projects/scavenge-android`** — create it, one git repo.

Read `PROPOSAL-map-coordinates-android.md` §7 for the design rationale, and the server's
`README.md` "Hunter JSON API" section for the contract you are consuming.

## 1. Why this app exists

Uploading a photo through a **web page** on Android loses the photo's GPS. Since Android 10,
`MediaProvider` redacts location metadata for any process without `ACCESS_MEDIA_LOCATION`,
and a web page cannot hold that permission. A `<input type="file">` picker returns a
redacted copy, so the bytes reaching JavaScript already lack GPS — and the tags arrive as
**zeros with a zero denominator (NaN)** rather than absent, which is why it reads as silent
stripping. A native app can hold the permission, so it is the only reliable fix.

## 2. Toolchain

| | |
|---|---|
| Project | `~/projects/scavenge-android` (new git repo, **private** GitHub later) |
| Language/UI | Kotlin + Jetpack Compose |
| Gradle | 8.9 (already cached at `~/.gradle/wrapper/dists/gradle-8.9-bin`) |
| **JDK** | **21** — `/usr/lib/jvm/java-21-openjdk-amd64`. JDK 17 is **no longer installed** on this box, unlike when `~/projects/beakon-android` was built. |
| SDK | `~/android-sdk`, platform `android-35`, build-tools `35.0.0` |
| compileSdk / targetSdk / minSdk | 35 / 35 / 26 |

Use `~/projects/beakon-android` as a **structural reference only** (version catalog,
Compose setup, module layout, how it builds). Do not copy its domain code — this is a
different app against a different API.

## 3. Permissions — all required, all with a stated purpose

```xml
<uses-permission android:name="android.permission.INTERNET" />
<uses-permission android:name="android.permission.ACCESS_MEDIA_LOCATION" />
<uses-permission android:name="android.permission.ACCESS_FINE_LOCATION" />
<uses-permission android:name="android.permission.ACCESS_COARSE_LOCATION" />
<uses-permission android:name="android.permission.CAMERA" />
<uses-permission android:name="android.permission.FOREGROUND_SERVICE" />
<uses-permission android:name="android.permission.FOREGROUND_SERVICE_LOCATION" />
<uses-permission android:name="android.permission.POST_NOTIFICATIONS" />
```

- `ACCESS_MEDIA_LOCATION` is **declared only** — it is a normal permission, not a runtime
  one, so there is no prompt for it. Do not add a runtime request for it.
- Location and camera are runtime permissions: request them in context, explain why, and
  **never crash or silently no-op when denied**. A denied location permission must degrade
  to "no live tracking" with a visible reason, not a broken screen.
- `POST_NOTIFICATIONS` on API 33+ is needed for the tracking notification.

## 4. The EXIF capture paths — the crux of this app

Two paths, **both** implemented. This is the whole reason the app exists.

### 4a. In-app camera capture (preferred)
CameraX (`ImageCapture`) writing to an app-owned file in `getExternalFilesDir`.

**CameraX does not attach GPS automatically.** After capture, write the location into the
file's EXIF yourself (`androidx.exifinterface.media.ExifInterface`: `setLatLong` +
`saveAttributes`) using the device's current fix. Because the app owns the file, nothing is
redacted and no special permission is needed to read it back.

If no location fix is available at capture time, **capture and upload anyway** — the server
records `location_status = "missing"` and the photo still earns its AI points. Never block
a submission for lack of GPS.

### 4b. Gallery pick (the path that motivated the app)
Resolve the picked `content://media/...` URI through **`MediaStore.setRequireOriginal(uri)`**
(requires `ACCESS_MEDIA_LOCATION`) and read it with `ExifInterface`. Without
`setRequireOriginal` the returned stream has GPS redacted to zeros/NaN.

- `setRequireOriginal` can throw (`UnsupportedOperationException`/`SecurityException`) if the
  permission is not held or the URI is not a MediaStore URI. **Handle it explicitly** and
  surface a clear message — do not swallow it.
- Read GPS with `ExifInterface.getLatLong()`, which returns `false` when absent. **Distinguish
  "no GPS tag" from "GPS present but all zeros"** — the latter is the redaction signature and
  should be treated as *no location* and reported as such, not sent as `(0, 0)`.
  Sending `(0, 0)` would place the player in the Gulf of Guinea and could score as a
  distant miss, which is worse than honest absence.
- If GPS is absent, upload anyway (§4a's rule applies equally).

Upload the **original bytes unmodified**; the server parses GPS from the stored original.

## 5. Screens — hunter parity with the web UI

The owner asked for the interface to **largely replicate the web version**. Cover:

1. **Login** — passphrase → `POST /api/login`. Persist the session cookie.
2. **Dashboard** — the item list from `GET /api/items`: name, description, points,
   bonus hint, and this team's state (locked, attempts, points awarded). Locked items are
   visibly locked and not submittable.
3. **Capture/submit** — pick camera or gallery, preview, upload to `POST /api/submit`
   with progress, then show the result: verified or not, points, and the
   `location_status` in words (e.g. "Location unavailable — no proximity points"). Mirror
   the web result screen's honesty: never imply location was used when it was not.
4. **Gallery** — grid from `GET /api/gallery` using the `thumb_url` / `display_url` the
   server returns; unapproved photos visually marked, as the web gallery does.
5. **Leaderboard** — `GET /api/leaderboard`.
6. **Map** — load the server's `/map` page in a **WebView** with the session cookie
   attached. The map is a Leaflet HTML page; embedding it gives exact parity without
   reimplementing Leaflet natively. Must handle a failure to load with a visible message.
7. **Settings** — server URL, tracking on/off, reporting interval.

## 6. Live team tracking

A **foreground service** (with the mandatory ongoing notification) that reports the device
position to `POST /api/position` as **JSON** (`{"lat":…, "lon":…, "accuracy_m":…}`) on an
interval.

- **Default interval: 30 seconds.** Configurable in settings. State the battery implication
  in the UI — do not hide it.
- User-toggleable, and **off by default**. Turning it on is an explicit choice.
- Must survive the app being backgrounded (that is the point) and stop cleanly when toggled
  off.
- Handle the server being unreachable without crashing or spamming; back off and surface the
  state in the UI. **Never fail silently** — that is a standing rule for this owner.
- Location for tracking and location for photo EXIF may come from the same fix; do not
  request twice.

## 7. Config

Server base URL is user-entered and persisted. Default to a sensible LAN placeholder and
make it editable. **No hardcoded production hostname.**

TLS: use the **platform trust store by default** (the beakon-android lesson — do not force a
certificate pin, which breaks on every Let's Encrypt renewal). No trust-all `TrustManager`.
No cleartext traffic (`cleartextTrafficPermitted="false"`) — if the server runs plain HTTP on
a LAN, that is a deliberate decision the owner must make, so **surface it rather than
silently enabling cleartext**.

## 8. Tests

Unit and integration tests where they can genuinely run on the JVM. At minimum:

- **EXIF GPS extraction** — a photo with GPS → correct lat/lon; a photo with the **redaction
  signature** (tags present, all zeros) → treated as **absent**, not `(0,0)`; no tags →
  absent. This is the app's central behaviour and must be tested with real EXIF fixtures.
- **Capture-location decision** — a pure function deciding whether to attach a location, and
  that a missing fix does **not** block submission.
- **API client** against `MockWebServer`: login, items, submit (multipart), gallery,
  leaderboard, position. Assert the JSON position body shape the server expects.
- **Session handling** — the cookie is persisted and sent on subsequent calls.
- **Error paths** — a 403 and a 500 both produce a visible error state, never a silent
  failure.

**You cannot test on a device** — none is available. Do not claim device behaviour was
observed. State plainly that camera, gallery-picker and foreground-service behaviour is
verified by compilation, unit tests and manifest inspection only.

## 9. Acceptance criteria

1. `./gradlew assembleDebug` builds a debug APK with `JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64` and `ANDROID_HOME=~/android-sdk`. Report the APK path and its sha256.
2. The manifest declares all §3 permissions, and `ACCESS_MEDIA_LOCATION` is present.
3. No trust-all `TrustManager`, no cleartext permitted.
4. All §8 tests pass; state the count.
5. The gallery path calls `MediaStore.setRequireOriginal` and handles its failure modes.
6. A photo with no GPS still uploads and the result screen says location was unavailable.
7. Live tracking is off by default, toggleable, and posts JSON to `/api/position`.
8. The map screen loads the server's `/map` page and reports a load failure visibly.
9. The app builds and the tests pass — but say clearly that nothing was run on a device.

## 10. Out of scope
- Admin surfaces (hunter-facing only).
- Server-side changes — if you need one, **report it rather than making it**; the server is
  committed and verified at `3254bc6`.
- iOS.
- Making coordinates tamper-proof.

## 11. Honesty requirement
Report what you verified versus what you could not. Do not claim a device behaviour you did
not observe. If you deviate, say so and say why.
