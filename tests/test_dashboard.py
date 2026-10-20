"""§1.4 / §2.4 — the dashboard is scoped to the active hunt and counts proximity."""

import asyncio

import app.main as main
from dbutil import seed_hunt


def _login(client):
    return client.post("/team/login", data={"passphrase": "alpha_secret"})


def test_dashboard_only_shows_active_hunt_items(client, session_factory):
    seed_hunt(session_factory, hunt_name="Hunt A", item_name="Item Alpha")
    seed_hunt(session_factory, hunt_name="Hunt B", item_name="Item Beta", with_team=False)
    # Make Hunt A the active hunt (first by id), as the app does at startup.
    asyncio.run(main._refresh_theme())

    assert _login(client).status_code == 200
    body = client.get("/hunt/dashboard").text
    assert "Item Alpha" in body
    assert "Item Beta" not in body


def test_dashboard_max_points_includes_proximity(client, session_factory, monkeypatch):
    seed_hunt(session_factory, points=100, proximity_points=50, target=(34.0, -116.0))
    asyncio.run(main._refresh_theme())
    monkeypatch.setattr(
        main, "get_hunt_state",
        lambda: {"is_open": True, "ends_at": None, "duration_minutes": 160, "seconds_remaining": 600},
    )

    assert _login(client).status_code == 200
    body = client.get("/hunt/dashboard").text
    # 100 base + 50 proximity reachable, none earned yet.
    assert "0 / 150 points" in body
