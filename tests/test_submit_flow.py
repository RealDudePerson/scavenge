"""§2.4 / §2.5 / §4 / §6 — end-to-end submit behaviour with a mocked AI.

The AI review is stubbed so these tests are hermetic; the points maths,
coordinate parsing, persistence and the hunter flow are all real.
"""

import datetime
from datetime import timezone

import app.main as main
from dbutil import fetch_submissions, seed_hunt
from exif_helpers import build_jpeg


def _fresh_exif():
    """A DateTimeOriginal equal to 'now' in UTC, with a matching offset."""
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


def _open_hunt(monkeypatch):
    monkeypatch.setattr(
        main,
        "get_hunt_state",
        lambda: {"is_open": True, "ends_at": None, "duration_minutes": 160, "seconds_remaining": 600},
    )
    monkeypatch.setattr(main, "review_image", _approved_review)


def _login(client):
    return client.post("/team/login", data={"passphrase": "alpha_secret"})


def _submit(client, item_id, team_id, jpeg, **extra):
    data = {"item_id": str(item_id), "team_id": str(team_id)}
    data.update({k: str(v) for k, v in extra.items()})
    return client.post(
        "/submit",
        data=data,
        files={"file": ("photo.jpg", jpeg, "image/jpeg")},
    )


TARGET = (34.0, -116.0)


def test_missing_gps_still_awards_ai_points(client, session_factory, monkeypatch):
    ids = seed_hunt(session_factory, target=TARGET, proximity_points=50, points=100)
    _open_hunt(monkeypatch)
    _login(client)

    _, dt, off = _fresh_exif()
    jpeg = build_jpeg(datetime_original=dt, offset_original=off).getvalue()
    response = _submit(client, ids["item_id"], ids["team_id"], jpeg)

    assert response.status_code == 200
    message = response.json()["message"]
    assert message.startswith("Verified!")
    assert "Location unavailable — no proximity points" in message

    sub = fetch_submissions(session_factory)[0]
    assert sub["verified"] is True
    assert sub["points_awarded"] == 100          # AI points, no proximity
    assert sub["proximity_points_awarded"] == 0
    assert sub["location_status"] == "missing"
    assert sub["gps_lat"] is None
    assert sub["distance_m"] is None
    assert sub["captured_at"] is not None


def test_gps_hit_awards_ai_plus_proximity(client, session_factory, monkeypatch):
    ids = seed_hunt(session_factory, target=TARGET, proximity_points=50, points=100)
    _open_hunt(monkeypatch)
    _login(client)

    _, dt, off = _fresh_exif()
    jpeg = build_jpeg(datetime_original=dt, offset_original=off, gps=TARGET).getvalue()
    response = _submit(client, ids["item_id"], ids["team_id"], jpeg)

    assert response.status_code == 200
    message = response.json()["message"]
    assert "Verified!" in message
    assert "+50 proximity points" in message

    sub = fetch_submissions(session_factory)[0]
    assert sub["points_awarded"] == 150          # 100 AI + 50 proximity
    assert sub["proximity_points_awarded"] == 50
    assert sub["location_status"] == "hit"
    assert sub["gps_lat"] == round(sub["gps_lat"], 3) == 34.0
    assert sub["gps_lon"] == -116.0
    assert sub["distance_m"] == 0.0


def test_captured_at_is_the_exif_instant(client, session_factory, monkeypatch):
    ids = seed_hunt(session_factory, target=TARGET, proximity_points=50)
    _open_hunt(monkeypatch)
    _login(client)

    now, dt, off = _fresh_exif()
    jpeg = build_jpeg(datetime_original=dt, offset_original=off, gps=TARGET).getvalue()
    _submit(client, ids["item_id"], ids["team_id"], jpeg)

    sub = fetch_submissions(session_factory)[0]
    assert sub["captured_at"].replace(tzinfo=timezone.utc) == now


def test_live_fix_is_stored_but_never_scores(client, session_factory, monkeypatch):
    ids = seed_hunt(session_factory, target=TARGET, proximity_points=50)
    _open_hunt(monkeypatch)
    _login(client)

    _, dt, off = _fresh_exif()
    jpeg = build_jpeg(datetime_original=dt, offset_original=off, gps=TARGET).getvalue()
    # A wildly different "live" fix must not override the EXIF-derived value.
    response = _submit(
        client, ids["item_id"], ids["team_id"], jpeg, live_lat="0.0", live_lon="0.0"
    )
    assert response.status_code == 200

    sub = fetch_submissions(session_factory)[0]
    assert sub["location_status"] == "hit"        # scored from EXIF
    assert sub["proximity_points_awarded"] == 50
    assert sub["live_lat"] == 0.0                 # stored for display only
    assert sub["live_lon"] == 0.0


def test_gate_mode_blocks_without_gps(client, session_factory, monkeypatch):
    ids = seed_hunt(session_factory, target=TARGET, proximity_points=50, mode="gate")
    _open_hunt(monkeypatch)
    _login(client)

    _, dt, off = _fresh_exif()
    jpeg = build_jpeg(datetime_original=dt, offset_original=off).getvalue()
    response = _submit(client, ids["item_id"], ids["team_id"], jpeg)

    assert response.status_code == 200
    assert "within the item's area" in response.json()["message"]

    sub = fetch_submissions(session_factory)[0]
    assert sub["verified"] is False
    assert sub["location_status"] == "missing"


def test_full_hunter_flow(client, session_factory, monkeypatch):
    """login -> dashboard -> submit -> gallery -> leaderboard (acceptance 10)."""
    ids = seed_hunt(session_factory, target=TARGET, proximity_points=50)
    _open_hunt(monkeypatch)

    assert _login(client).status_code == 200
    assert client.get("/hunt/dashboard").status_code == 200

    _, dt, off = _fresh_exif()
    jpeg = build_jpeg(datetime_original=dt, offset_original=off, gps=TARGET).getvalue()
    assert _submit(client, ids["item_id"], ids["team_id"], jpeg).status_code == 200

    assert client.get("/gallery").status_code == 200
    assert client.get("/leaderboard").status_code == 200


def test_admin_gallery_surfaces_location(client, session_factory, monkeypatch):
    """§2.4 — admin_all_gallery shows location_status and distance_m."""
    ids = seed_hunt(session_factory, target=TARGET, proximity_points=50)
    _open_hunt(monkeypatch)
    monkeypatch.setattr(main, "get_admin_password", lambda: "adminpw")

    _login(client)
    _, dt, off = _fresh_exif()
    jpeg = build_jpeg(datetime_original=dt, offset_original=off, gps=TARGET).getvalue()
    _submit(client, ids["item_id"], ids["team_id"], jpeg)

    assert client.post("/admin/login", data={"password": "adminpw"}).status_code == 200
    body = client.get("/admin/all-gallery").text
    assert "Location:" in body
    assert "hit" in body
    assert "0 m" in body
    assert "+50 proximity" in body
