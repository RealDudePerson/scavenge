"""Team-authenticated hunt open/closed state — ``GET /api/hunt``.

The Android client needs to know whether the hunt is open *before* the user
taps submit. The server already enforces the gate; this endpoint only surfaces
the exact dict ``get_hunt_state()`` produces, with ``ends_at`` as an ISO 8601
string (or ``null``).

``get_hunt_state`` reads its config via ``app.config._load_cfg``, so these
tests patch that loader to drive the real code path (rather than stubbing the
state dict out from under the endpoint).
"""

import datetime
from datetime import timezone

import app.config as config
from dbutil import seed_hunt


def _login(client, passphrase="alpha_secret"):
    return client.post("/api/login", json={"passphrase": passphrase})


def _set_config(monkeypatch, ends_at, duration_minutes=160):
    monkeypatch.setattr(
        config,
        "_load_cfg",
        lambda: {"hunt_ends_at": ends_at, "hunt_duration_minutes": duration_minutes},
    )


# ---------------------------------------------------------------------------
# Fail closed
# ---------------------------------------------------------------------------

def test_api_hunt_requires_auth(client):
    """Unauthenticated requests are rejected — the endpoint must fail closed."""
    resp = client.get("/api/hunt")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# State reporting
# ---------------------------------------------------------------------------

def test_api_hunt_closed_default(client, session_factory, monkeypatch):
    """Empty ``hunt_ends_at`` (the default) reports a closed hunt."""
    seed_hunt(session_factory)
    _set_config(monkeypatch, "")

    assert _login(client).status_code == 200
    body = client.get("/api/hunt").json()

    assert body["is_open"] is False
    assert body["ends_at"] is None
    assert body["seconds_remaining"] == 0
    assert body["duration_minutes"] == 160


def test_api_hunt_open_in_future(client, session_factory, monkeypatch):
    """An end timestamp in the future reports an open hunt with time left."""
    seed_hunt(session_factory)
    ends_at = datetime.datetime.now(timezone.utc) + datetime.timedelta(minutes=30)
    _set_config(monkeypatch, ends_at.isoformat())

    assert _login(client).status_code == 200
    body = client.get("/api/hunt").json()

    assert body["is_open"] is True
    assert body["seconds_remaining"] > 0
    # ends_at must be JSON-serialisable ISO 8601 text, not a datetime.
    assert isinstance(body["ends_at"], str)
    parsed = datetime.datetime.fromisoformat(body["ends_at"])
    assert parsed == ends_at


def test_api_hunt_closed_in_past(client, session_factory, monkeypatch):
    """An end timestamp in the past reports a closed hunt."""
    seed_hunt(session_factory)
    ends_at = datetime.datetime.now(timezone.utc) - datetime.timedelta(minutes=30)
    _set_config(monkeypatch, ends_at.isoformat())

    assert _login(client).status_code == 200
    body = client.get("/api/hunt").json()

    assert body["is_open"] is False
    assert body["seconds_remaining"] == 0
