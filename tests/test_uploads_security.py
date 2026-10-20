"""Security — the unauthenticated ``/uploads/originals`` mount must be gone.

Originals are the *as-uploaded* bytes, so they carry the submission's GPS EXIF
(the player's location). The mount served them with no authentication, so any
holder of a URL could fetch a full-resolution original including its
coordinates. The mount was removed; originals stay on disk for diagnostics but
are no longer reachable over HTTP.

``/uploads/thumb`` and ``/uploads/display`` are re-encoded (no EXIF) and remain
public because the web gallery and the Android client depend on them.
"""

import os
import uuid

import app.main as main


def _mount_directory(path):
    """Return the StaticFiles directory for a mounted URL path, or ``None``."""
    for route in main.app.routes:
        if getattr(route, "path", None) != path:
            continue
        directory = getattr(getattr(route, "app", None), "directory", None)
        if directory is not None:
            return directory
    return None


def _originals_dir():
    """The on-disk dir the (removed) originals mount would have served.

    Derived from the live thumb mount so it follows the app's real mount root
    regardless of the process CWD, and so a missing thumb mount is itself
    caught rather than silently probing the wrong place.
    """
    thumb_dir = _mount_directory("/uploads/thumb")
    assert thumb_dir is not None, "thumb mount is missing — the gallery would break"
    return os.path.join(os.path.dirname(thumb_dir), "originals")


def _write_probe(directory):
    """Create a real file under ``directory`` and return (filename, path).

    The file genuinely exists on disk, so a still-present mount would serve it
    with a 200; the 404 assertion below therefore fails for the right reason.
    """
    os.makedirs(directory, exist_ok=True)
    filename = f"security-probe-{uuid.uuid4().hex}.jpg"
    path = os.path.join(directory, filename)
    with open(path, "wb") as fh:
        fh.write(b"\xff\xd8\xff\xe0probe")
    return filename, path


def test_originals_are_not_publicly_reachable(client):
    filename, path = _write_probe(_originals_dir())
    try:
        # The file exists on disk, so a still-present mount would serve it 200.
        resp = client.get(f"/uploads/originals/{filename}")
        assert resp.status_code == 404
    finally:
        os.remove(path)

    assert _mount_directory("/uploads/originals") is None, (
        "the unauthenticated /uploads/originals mount must not exist"
    )


def test_thumb_and_display_still_serve(client):
    for mount_path in ("/uploads/thumb", "/uploads/display"):
        directory = _mount_directory(mount_path)
        assert directory is not None, f"{mount_path} mount is missing"

        filename, path = _write_probe(directory)
        try:
            resp = client.get(f"{mount_path}/{filename}")
            assert resp.status_code == 200
            assert resp.content == b"\xff\xd8\xff\xe0probe"
        finally:
            os.remove(path)
