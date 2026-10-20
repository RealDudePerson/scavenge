"""Phase 3a — JSON API for the hunter surface.

Covers: /api/login, /api/items (including the target-leak rule), /api/submit
(shared pipeline with /submit), /api/gallery, /api/leaderboard, and
/api/position accepting JSON as well as form encoding.
"""

import datetime
import json
from datetime import timezone

import app.main as main
from app.models import HuntItem
from dbutil import fetch_submissions, seed_hunt, run
from exif_helpers import build_jpeg
from sqlalchemy.future import select


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fresh_exif():
    now = datetime.datetime.now(timezone.utc).replace(microsecond=0)
    return now, now.strftime("%Y:%m:%d %H:%M:%S"), "+00:00"


def _approved_review(*args, **kwargs):
    return {
        "is_target": True,
        "confidence": 0.95,
        "matched_required": ["shows a thing"],
        "missed_required": [],
        "matched_bonus": [],
        "missed_bonus": [],
        "reason": "Looks right.",
    }


def _rejected_review(*args, **kwargs):
    return {
        "is_target": True,
        "confidence": 0.95,
        "matched_required": [],
        "missed_required": ["shows a thing"],
        "matched_bonus": [],
        "missed_bonus": [],
        "reason": "Does not show the thing.",
    }


def _open_hunt(monkeypatch):
    monkeypatch.setattr(
        main,
        "get_hunt_state",
        lambda: {"is_open": True, "ends_at": None, "duration_minutes": 160, "seconds_remaining": 600},
    )
    monkeypatch.setattr(main, "review_image", _approved_review)


def _login(client, passphrase="alpha_secret"):
    return client.post("/api/login", json={"passphrase": passphrase})


def _api_submit(client, item_id, jpeg, **extra):
    data = {"item_id": str(item_id)}
    data.update({k: str(v) for k, v in extra.items()})
    return client.post("/api/submit", data=data, files={"file": ("photo.jpg", jpeg, "image/jpeg")})


def _set_zone(session_factory, item_id, zone, zone_radius):
    async def _s():
        async with session_factory() as session:
            item = (await session.execute(select(HuntItem).where(HuntItem.id == item_id))).scalar_one()
            item.zone_lat = zone[0]
            item.zone_lon = zone[1]
            item.zone_radius_meters = zone_radius
            await session.commit()
    run(_s())


# ---------------------------------------------------------------------------
# §1 — POST /api/login
# ---------------------------------------------------------------------------

def test_api_login_succeeds_with_correct_passphrase(client, session_factory):
    ids = seed_hunt(session_factory)
    resp = _login(client)
    assert resp.status_code == 200
    body = resp.json()
    assert body["team_id"] == ids["team_id"]
    assert body["team_name"] == "Team Alpha"


def test_api_login_wrong_passphrase_is_401_json(client, session_factory):
    seed_hunt(session_factory)
    resp = _login(client, passphrase="nope")
    assert resp.status_code == 401
    # Must be JSON, not an HTML redirect.
    assert resp.headers["content-type"].startswith("application/json")
    assert "detail" in resp.json()


def test_api_login_missing_passphrase_is_400(client, session_factory):
    seed_hunt(session_factory)
    resp = client.post("/api/login", json={})
    assert resp.status_code == 400


def test_api_login_is_rate_limited(client, session_factory):
    seed_hunt(session_factory)
    statuses = [_login(client).status_code for _ in range(11)]
    assert statuses[:10] == [200] * 10
    assert statuses[10] == 429


# ---------------------------------------------------------------------------
# §1 / §3 — every protected endpoint fails closed (403 unauthenticated)
# ---------------------------------------------------------------------------

def test_all_protected_api_endpoints_reject_unauthenticated(client):
    assert client.get("/api/items").status_code == 403
    assert client.get("/api/gallery").status_code == 403
    assert client.get("/api/leaderboard").status_code == 403
    assert client.post("/api/position", json={"lat": 0, "lon": 0}).status_code == 403
    assert client.post(
        "/api/submit",
        data={"item_id": "1"},
        files={"file": ("x.jpg", b"x", "image/jpeg")},
    ).status_code == 403


# ---------------------------------------------------------------------------
# §1 / §3 — GET /api/items payload and the target-leak rule
# ---------------------------------------------------------------------------

TRUE_TARGET = (34.123456, -116.654321)
DISPLAY_ZONE = (34.987654, -116.012345)
JUDGE_RADIUS = 137.5
DISPLAY_RADIUS = 250.0
ITEM_POINTS = 42


def _walk(obj):
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield ("key", key)
            yield from _walk(value)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            yield from _walk(value)
    elif isinstance(obj, bool):
        return
    elif isinstance(obj, (int, float)):
        yield ("num", float(obj))


def _keys_and_numbers(obj):
    keys, numbers = set(), set()
    for kind, value in _walk(obj):
        (keys if kind == "key" else numbers).add(value)
    return keys, numbers


def test_api_items_leaks_no_true_target(client, session_factory, monkeypatch):
    """§1 — the team-facing item list must never carry the true target.

    Structurally identical in spirit to phase 2's /api/map leak test: scan the
    serialised payload for the forbidden keys *and* values anywhere.
    """
    ids = seed_hunt(
        session_factory,
        target=TRUE_TARGET,
        mode="bonus",
        proximity_points=50,
        radius=JUDGE_RADIUS,
        points=ITEM_POINTS,
        item_name="Zone Item",
    )
    _set_zone(session_factory, ids["item_id"], DISPLAY_ZONE, DISPLAY_RADIUS)

    assert _login(client).status_code == 200
    resp = client.get("/api/items")
    assert resp.status_code == 200
    payload = resp.json()
    body = json.dumps(payload)

    item = next(i for i in payload if i["id"] == ids["item_id"])
    # The item is present and carries its real, non-secret fields.
    assert item["name"] == "Zone Item"
    assert item["points"] == ITEM_POINTS
    assert item["zone"]["lat"] == DISPLAY_ZONE[0]
    assert item["zone"]["lon"] == DISPLAY_ZONE[1]
    assert item["zone"]["radius_m"] == DISPLAY_RADIUS

    # No forbidden keys anywhere.
    keys, numbers = _keys_and_numbers(payload)
    for forbidden in ("target_lat", "target_lon", "radius_meters"):
        assert forbidden not in keys, f"leaked key {forbidden}"
        assert forbidden not in body, f"leaked key {forbidden} in raw body"

    # No forbidden values anywhere, at any nesting depth, nor as raw text.
    for value in (TRUE_TARGET[0], TRUE_TARGET[1], JUDGE_RADIUS):
        assert value not in numbers, f"leaked value {value} in {body}"
        assert str(value) not in body, f"leaked value {value!r} as text"

    # The display zone values ARE present, proving the item was not dropped.
    assert DISPLAY_ZONE[0] in numbers
    assert DISPLAY_ZONE[1] in numbers
    assert DISPLAY_RADIUS in numbers


def test_api_items_includes_team_state(client, session_factory, monkeypatch):
    ids = seed_hunt(session_factory, points=100)
    _open_hunt(monkeypatch)
    assert _login(client).status_code == 200

    _, dt, off = _fresh_exif()
    jpeg = build_jpeg(datetime_original=dt, offset_original=off).getvalue()
    assert _api_submit(client, ids["item_id"], jpeg).status_code == 200

    resp = client.get("/api/items")
    item = next(i for i in resp.json() if i["id"] == ids["item_id"])
    assert item["locked"] is True
    assert item["attempts"] == 1
    assert item["points_awarded"] == 100
    assert item["has_bonus"] is False


# ---------------------------------------------------------------------------
# §1 / §3 — POST /api/submit shares the pipeline and rules
# ---------------------------------------------------------------------------

def test_api_submit_approved_earns_ai_plus_proximity(client, session_factory, monkeypatch):
    target = (34.0, -116.0)
    ids = seed_hunt(session_factory, target=target, proximity_points=50, points=100)
    _open_hunt(monkeypatch)
    assert _login(client).status_code == 200

    _, dt, off = _fresh_exif()
    jpeg = build_jpeg(datetime_original=dt, offset_original=off, gps=target).getvalue()
    resp = _api_submit(client, ids["item_id"], jpeg)
    assert resp.status_code == 200
    body = resp.json()

    assert body["verified"] is True
    assert body["points_awarded"] == 150
    assert body["proximity_points_awarded"] == 50
    assert body["location_status"] == "hit"
    assert body["message"].startswith("Verified!")

    # Same persisted result as the HTML route would produce.
    sub = fetch_submissions(session_factory)[0]
    assert sub["verified"] is True
    assert sub["points_awarded"] == 150
    assert sub["proximity_points_awarded"] == 50
    assert sub["location_status"] == "hit"


def test_api_submit_no_gps_still_earns_ai_points(client, session_factory, monkeypatch):
    ids = seed_hunt(session_factory, target=(34.0, -116.0), proximity_points=50, points=100)
    _open_hunt(monkeypatch)
    assert _login(client).status_code == 200

    _, dt, off = _fresh_exif()
    jpeg = build_jpeg(datetime_original=dt, offset_original=off).getvalue()
    body = _api_submit(client, ids["item_id"], jpeg).json()

    assert body["verified"] is True
    assert body["points_awarded"] == 100
    assert body["proximity_points_awarded"] == 0
    assert body["location_status"] == "missing"
    assert "Location unavailable" in body["message"]


def test_api_submit_proximity_requires_approval(client, session_factory, monkeypatch):
    """The phase-1/2 fix must hold on the JSON path too."""
    target = (34.0, -116.0)
    ids = seed_hunt(session_factory, target=target, proximity_points=50, points=100)
    _open_hunt(monkeypatch)
    monkeypatch.setattr(main, "review_image", _rejected_review)
    assert _login(client).status_code == 200

    _, dt, off = _fresh_exif()
    jpeg = build_jpeg(datetime_original=dt, offset_original=off, gps=target).getvalue()
    body = _api_submit(client, ids["item_id"], jpeg).json()

    assert body["verified"] is False
    assert body["points_awarded"] == 0
    assert body["proximity_points_awarded"] == 0
    assert body["location_status"] == "hit"   # diagnostics preserved
    assert body["message"].startswith("Not approved")

    sub = fetch_submissions(session_factory)[0]
    assert sub["proximity_points_awarded"] == 0
    assert sub["location_status"] == "hit"


def test_api_submit_and_html_submit_agree(client, session_factory, monkeypatch):
    """Both routes share one implementation, so the outcomes must match."""
    target = (34.0, -116.0)
    ids_a = seed_hunt(session_factory, target=target, proximity_points=50, points=100,
                      team_name="Team A", passphrase="alpha_secret")
    ids_b = seed_hunt(session_factory, target=target, proximity_points=50, points=100,
                      team_name="Team B", passphrase="beta_secret",
                      item_name="Item B", hunt_name="Hunt B")
    _open_hunt(monkeypatch)

    _, dt, off = _fresh_exif()
    jpeg = build_jpeg(datetime_original=dt, offset_original=off, gps=target).getvalue()

    assert _login(client, "alpha_secret").status_code == 200
    html = client.post(
        "/submit",
        data={"item_id": str(ids_a["item_id"]), "team_id": str(ids_a["team_id"])},
        files={"file": ("photo.jpg", jpeg, "image/jpeg")},
    ).json()

    assert _login(client, "beta_secret").status_code == 200
    api = _api_submit(client, ids_b["item_id"], jpeg).json()

    assert api["verified"] == (html["message"].startswith("Verified!"))
    assert api["points_awarded"] == 150
    assert api["location_status"] == "hit"


def test_api_submit_hunt_closed_is_json_not_html(client, session_factory):
    """A closed hunt returns the JSON contract (verified=False), never a redirect page."""
    ids = seed_hunt(session_factory)
    monkeypatch_state = {"is_open": False, "ends_at": None, "duration_minutes": 160, "seconds_remaining": 0}
    import app.main as _main
    original = _main.get_hunt_state
    _main.get_hunt_state = lambda: monkeypatch_state
    try:
        assert _login(client).status_code == 200
        _, dt, off = _fresh_exif()
        jpeg = build_jpeg(datetime_original=dt, offset_original=off).getvalue()
        resp = _api_submit(client, ids["item_id"], jpeg)
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")
        body = resp.json()
        assert body["verified"] is False
        assert body["message"] == "Hunt closed"
    finally:
        _main.get_hunt_state = original


# ---------------------------------------------------------------------------
# §1 / §3 — GET /api/gallery is team-scoped
# ---------------------------------------------------------------------------

def test_api_gallery_returns_only_this_team(client, session_factory, monkeypatch):
    target = (34.0, -116.0)
    ids_a = seed_hunt(session_factory, target=target, points=100,
                      team_name="Team A", passphrase="alpha_secret", item_name="Item A")
    ids_b = seed_hunt(session_factory, target=target, points=100,
                      team_name="Team B", passphrase="beta_secret",
                      item_name="Item B", hunt_name="Hunt B")
    _open_hunt(monkeypatch)

    _, dt, off = _fresh_exif()
    jpeg = build_jpeg(datetime_original=dt, offset_original=off, gps=target).getvalue()

    assert _login(client, "alpha_secret").status_code == 200
    assert _api_submit(client, ids_a["item_id"], jpeg).status_code == 200

    assert _login(client, "beta_secret").status_code == 200
    assert _api_submit(client, ids_b["item_id"], jpeg).status_code == 200

    assert _login(client, "alpha_secret").status_code == 200
    entries = client.get("/api/gallery").json()
    assert len(entries) == 1
    entry = entries[0]
    assert entry["item_name"] == "Item A"
    assert entry["verified"] is True
    assert entry["points_awarded"] == 100
    assert entry["attempt_number"] == 1
    assert entry["thumb_url"].startswith("/uploads/thumb/")
    assert entry["display_url"].startswith("/uploads/display/")
    assert isinstance(entry["submitted_at"], str)


# ---------------------------------------------------------------------------
# §1 / §3 — GET /api/leaderboard
# ---------------------------------------------------------------------------

def test_api_leaderboard_requires_auth(client):
    assert client.get("/api/leaderboard").status_code == 403


def test_api_leaderboard_shape_and_points(client, session_factory, monkeypatch):
    target = (34.0, -116.0)
    ids = seed_hunt(session_factory, target=target, points=100)
    _open_hunt(monkeypatch)
    assert _login(client).status_code == 200

    _, dt, off = _fresh_exif()
    jpeg = build_jpeg(datetime_original=dt, offset_original=off, gps=target).getvalue()
    assert _api_submit(client, ids["item_id"], jpeg).status_code == 200

    board = client.get("/api/leaderboard").json()
    assert isinstance(board, list)
    assert board[0]["team_name"] == "Team Alpha"
    assert board[0]["points"] == 100


# ---------------------------------------------------------------------------
# §1 / §3 — POST /api/position accepts JSON and form
# ---------------------------------------------------------------------------

def test_api_position_accepts_json_body(client, session_factory, monkeypatch):
    seed_hunt(session_factory)
    assert _login(client).status_code == 200

    resp = client.post("/api/position", json={"lat": 34.01, "lon": -116.01, "accuracy_m": 5.0})
    assert resp.status_code == 200
    assert resp.json()["message"] == "Position updated."

    data = client.get("/api/map").json()
    assert data["me"] is not None
    assert data["me"]["lat"] == 34.01
    assert data["me"]["lon"] == -116.01
    assert data["me"]["accuracy_m"] == 5.0


def test_api_position_still_accepts_form(client, session_factory, monkeypatch):
    seed_hunt(session_factory)
    assert _login(client).status_code == 200

    resp = client.post("/api/position", data={"lat": "34.02", "lon": "-116.02"})
    assert resp.status_code == 200
    data = client.get("/api/map").json()
    assert data["me"]["lat"] == 34.02


def test_api_position_json_validation(client, session_factory, monkeypatch):
    seed_hunt(session_factory)
    assert _login(client).status_code == 200
    assert client.post("/api/position", json={"lat": "NaN", "lon": 0}).status_code == 400
    assert client.post("/api/position", json={"lat": 91, "lon": 0}).status_code == 400
    assert client.post("/api/position", json={"lon": 0}).status_code == 400
    assert client.post("/api/position", json={"lat": "abc", "lon": 0}).status_code == 400
