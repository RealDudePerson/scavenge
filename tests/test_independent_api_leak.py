"""Independent verification of the phase 3a JSON API (written by Hermes).

Checks the two properties that matter: /api/items must not leak an item's true
target (same rule as phase 2's map), and /api/position must accept BOTH a JSON
body and the legacy form encoding.
"""
import json

from app.main import app, require_admin
from app.models import Hunt, HuntItem, Team
from app.security import hash_passphrase
from tests.dbutil import run

TRUE_LAT, TRUE_LON = 34.123456, -116.654321
ZONE_LAT, ZONE_LON = 34.987654, -116.123456
JUDGE_R = 137.0          # also must not leak: it reveals judging precision
PASS = "leak_secret"


def _seed(session_factory):
    async def _s():
        async with session_factory() as session:
            h = Hunt(name="API Leak Hunt", theme_json="{}")
            session.add(h)
            await session.commit()
            it = HuntItem(
                hunt_id=h.id, name="API Leak Item", description="d", points=100,
                bonus_points=0, required_properties='["x"]', bonus_properties="[]",
                target_lat=TRUE_LAT, target_lon=TRUE_LON, radius_meters=JUDGE_R,
                near_multiplier=2.0, proximity_points=50, proximity_mode="bonus",
                zone_lat=ZONE_LAT, zone_lon=ZONE_LON, zone_radius_meters=250.0,
            )
            session.add(it)
            t = Team(name="API Leak Team", passphrase_hash=hash_passphrase(PASS))
            session.add(t)
            await session.commit()
            return h.id, it.id, t.id

    return run(_s())


def _numbers(node, out):
    if isinstance(node, bool):
        return
    if isinstance(node, (int, float)):
        out.append(float(node))
    elif isinstance(node, dict):
        for v in node.values():
            _numbers(v, out)
    elif isinstance(node, list):
        for v in node:
            _numbers(v, out)


def _login(client):
    r = client.post("/api/login", json={"passphrase": PASS})
    assert r.status_code == 200, f"/api/login failed: {r.status_code} {r.text[:200]}"
    return r


def test_api_items_leaks_no_true_target(client, session_factory):
    _seed(session_factory)
    _login(client)

    r = client.get("/api/items")
    assert r.status_code == 200, f"/api/items returned {r.status_code}"
    body = r.text

    for key in ("target_lat", "target_lon", "radius_meters"):
        assert key not in body, f"/api/items exposes the {key} key"

    payload = json.loads(body)
    nums = []
    _numbers(payload, nums)
    for secret, label in ((TRUE_LAT, "lat"), (TRUE_LON, "lon"), (JUDGE_R, "judging radius")):
        assert not any(abs(n - secret) < 1e-6 for n in nums), \
            f"/api/items leaks the true {label} ({secret})"

    # the item itself must still be there, with its team state
    items = payload["items"] if isinstance(payload, dict) and "items" in payload else payload
    assert len(items) == 1, f"expected the seeded item, got {items}"
    assert "locked" in items[0], "item payload is missing team state"


def test_api_position_accepts_json_and_form(client, session_factory):
    _seed(session_factory)
    _login(client)

    r = client.post("/api/position", json={"lat": 34.5, "lon": -116.5, "accuracy_m": 8.0})
    assert r.status_code in (200, 201, 204), f"JSON body rejected: {r.status_code} {r.text[:200]}"

    r = client.post("/api/position", data={"lat": "34.6", "lon": "-116.6"})
    assert r.status_code in (200, 201, 204), f"form encoding rejected: {r.status_code} {r.text[:200]}"


def test_api_position_rejects_bad_coordinates(client, session_factory):
    _seed(session_factory)
    _login(client)
    for body in ({"lat": 91.0, "lon": 0.0}, {"lat": 0.0, "lon": 181.0},
                 {"lat": "abc", "lon": "xyz"}):
        r = client.post("/api/position", json=body)
        assert r.status_code >= 400, f"accepted invalid coordinates {body} ({r.status_code})"


def test_new_api_endpoints_require_auth(client):
    for path in ("/api/items", "/api/gallery", "/api/leaderboard", "/api/map", "/admin/map"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code in (401, 403), f"{path} allowed unauthenticated ({r.status_code})"
    r = client.post("/api/submit", data={"item_id": "1"}, follow_redirects=False)
    assert r.status_code in (401, 403), f"/api/submit allowed unauthenticated ({r.status_code})"


def test_leaderboard_is_not_public_via_api(client):
    r = client.get("/api/leaderboard", follow_redirects=False)
    assert r.status_code in (401, 403), "the API leaderboard is public (phase-1 defect)"
