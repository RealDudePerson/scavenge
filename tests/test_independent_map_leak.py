"""Independent verification (written by Hermes, not the implementing agent).

Asserts the security property that matters most in phase 2: the team-facing
/api/map payload must never carry an item's true target. Uses coordinates that
are far apart so a leak cannot be missed by rounding, and walks the parsed JSON
structurally rather than only grepping the raw body.
"""
import json

from sqlalchemy.future import select

from app.main import app, require_admin
from app.models import Hunt, HuntItem, Team
from app.security import hash_passphrase
from tests.dbutil import run

TRUE_LAT, TRUE_LON = 34.123456, -116.654321      # secret: where the item really is
ZONE_LAT, ZONE_LON = 34.987654, -116.123456      # public: the offset drawn zone
ZONE_R = 250.0
JUDGE_R = 100.0                                   # also must not leak (reveals precision)


def _seed(session_factory):
    async def _s():
        async with session_factory() as session:
            h = Hunt(name="Leak Hunt", theme_json="{}")
            session.add(h)
            await session.commit()
            it = HuntItem(
                hunt_id=h.id, name="Leak Item", description="d", points=100,
                bonus_points=0, required_properties='["x"]', bonus_properties="[]",
                target_lat=TRUE_LAT, target_lon=TRUE_LON, radius_meters=JUDGE_R,
                near_multiplier=2.0, proximity_points=50, proximity_mode="bonus",
                zone_lat=ZONE_LAT, zone_lon=ZONE_LON, zone_radius_meters=ZONE_R,
            )
            session.add(it)
            t = Team(name="Leak Team", passphrase_hash=hash_passphrase("leak_secret"))
            session.add(t)
            await session.commit()
            return h.id, it.id, t.id

    return run(_s())


def _numbers(node, out):
    """Collect every numeric value anywhere in a nested JSON structure."""
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


def test_team_map_payload_leaks_no_true_target(client, session_factory):
    _seed(session_factory)
    r = client.post("/team/login", data={"passphrase": "leak_secret"}, follow_redirects=False)
    assert r.status_code in (200, 302), f"team login failed: {r.status_code}"

    r = client.get("/api/map")
    assert r.status_code == 200, f"/api/map returned {r.status_code}"

    body = r.text
    # 1. raw text: neither the key nor the value may appear
    assert "target_lat" not in body, "team payload exposes the target_lat key"
    assert "target_lon" not in body, "team payload exposes the target_lon key"
    assert str(TRUE_LAT) not in body, f"team payload contains true lat {TRUE_LAT}"
    assert str(TRUE_LON) not in body, f"team payload contains true lon {TRUE_LON}"

    # 2. structural: no numeric value anywhere may be the true target
    payload = json.loads(body)
    nums = []
    _numbers(payload, nums)
    for secret, label in ((TRUE_LAT, "lat"), (TRUE_LON, "lon")):
        assert not any(abs(n - secret) < 1e-6 for n in nums), \
            f"team payload leaks the true {label} ({secret}) as a numeric value"

    # 3. the display zone IS present, so the map still has something to draw
    assert any(abs(n - ZONE_LAT) < 1e-6 for n in nums), "display zone lat missing"
    assert any(abs(n - ZONE_LON) < 1e-6 for n in nums), "display zone lon missing"


def test_admin_map_payload_does_include_true_target(client, session_factory):
    _seed(session_factory)
    app.dependency_overrides[require_admin] = lambda: None
    try:
        r = client.get("/admin/map")
        assert r.status_code == 200, f"/admin/map returned {r.status_code}"
        nums = []
        _numbers(json.loads(r.text), nums)
        assert any(abs(n - TRUE_LAT) < 1e-6 for n in nums), \
            "admin payload is missing the true target (admins must see it)"
        assert any(abs(n - ZONE_LAT) < 1e-6 for n in nums), "admin payload missing zone"
    finally:
        app.dependency_overrides.pop(require_admin, None)


def test_team_and_admin_payloads_differ(client, session_factory):
    _seed(session_factory)
    client.post("/team/login", data={"passphrase": "leak_secret"}, follow_redirects=False)
    team_body = client.get("/api/map").text

    app.dependency_overrides[require_admin] = lambda: None
    try:
        admin_body = client.get("/admin/map").text
    finally:
        app.dependency_overrides.pop(require_admin, None)

    assert team_body != admin_body, "team and admin payloads are identical"
    assert str(TRUE_LAT) in admin_body, "admin payload should contain the true target"
    assert str(TRUE_LAT) not in team_body, "team payload must not contain the true target"


def test_map_endpoints_reject_unauthenticated(client):
    for path in ("/api/map", "/admin/map"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code in (401, 403, 302), f"{path} allowed unauthenticated ({r.status_code})"
    r = client.post("/api/position", data={"lat": 34.0, "lon": -116.0}, follow_redirects=False)
    assert r.status_code in (401, 403, 302), f"/api/position allowed unauthenticated ({r.status_code})"


def test_leaflet_is_vendored_not_cdn(client):
    r = client.get("/map", follow_redirects=False)
    if r.status_code in (401, 403, 302):
        return  # not logged in in this fixture; the other tests cover the payload
    body = r.text
    for cdn in ("unpkg.com", "cdn.jsdelivr.net", "cdnjs.cloudflare.com"):
        assert cdn not in body, f"map page still references CDN {cdn}"
