"""Independent verification (written by Hermes) that the originals mount is gone.

The StaticFiles mounts were created at import time against the real `uploads/` paths,
so this writes probe files into those real directories (gitignored) and cleans up
after itself. The point is to prove the 404 is real — i.e. the file genuinely exists
on disk and would be served if the mount were still there — not that a nonexistent
file returns 404 for the wrong reason.
"""
import os
import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent
ORIG = REPO / "uploads" / "originals"
THUMB = REPO / "uploads" / "thumb"
DISP = REPO / "uploads" / "display"

NAME = "hermes-independent-probe.jpg"
PAYLOAD = b"\xff\xd8\xff\xe0independent-probe\xff\xd9"


def _write(d):
    d.mkdir(parents=True, exist_ok=True)
    p = d / NAME
    p.write_bytes(PAYLOAD)
    return p


def test_originals_are_not_served_but_the_file_exists(client):
    p = _write(ORIG)
    try:
        assert p.exists(), "probe file was not created - test would pass for the wrong reason"
        r = client.get(f"/uploads/originals/{NAME}", follow_redirects=False)
        assert r.status_code == 404, (
            f"/uploads/originals still serves files (got {r.status_code}) - "
            "GPS EXIF is exposed to anyone with a URL"
        )
    finally:
        p.unlink(missing_ok=True)


def test_thumb_and_display_still_serve(client):
    """Guards against over-correcting and breaking the gallery on both clients."""
    for d, prefix in ((THUMB, "/uploads/thumb"), (DISP, "/uploads/display")):
        p = _write(d)
        try:
            r = client.get(f"{prefix}/{NAME}", follow_redirects=False)
            assert r.status_code == 200, f"{prefix} stopped serving (got {r.status_code})"
            assert r.content == PAYLOAD, f"{prefix} returned wrong bytes"
        finally:
            p.unlink(missing_ok=True)


def test_no_other_public_path_to_originals(client):
    """The mount was the only HTTP path; check nothing else exposes the directory."""
    for path in ("/uploads/originals", "/originals", "/uploads/originals/"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code in (404, 403), f"{path} unexpectedly reachable ({r.status_code})"
