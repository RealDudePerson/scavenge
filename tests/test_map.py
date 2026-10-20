"""§6 — Phase 2 map and position endpoints.

Covers: auth guards, target-leak security, admin-vs-team payload difference,
position validation, rate limiting, team-only visibility, vendored leaflet,
and staleness.
"""

import json
import datetime
import os
import re
import shutil
import subprocess
from datetime import timezone

import pytest
import app.main as main
from app.models import HuntItem
from dbutil import seed_hunt, run

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BROWSER = (
    shutil.which("google-chrome")
    or shutil.which("chromium")
    or shutil.which("chromium-browser")
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _login_team(client, passphrase="alpha_secret"):
    return client.post("/team/login", data={"passphrase": passphrase})


def _login_admin(client):
    """Log in as admin, patching the password check."""
    import app.main as main
    return client.post("/admin/login", data={"password": "adminpw"})


def _seed_zone_hunt(session_factory, *, target=(34.0, -116.0), zone=(34.05, -116.05),
                    zone_radius=200.0, radius=100.0, points=100):
    """Create a hunt with one item that has both true target AND display zone.

    The display zone is offset from the true target so the test can detect leaks.
    Values are parameterised so the leak test can use distinctive numbers.
    """
    from sqlalchemy.future import select
    ids = seed_hunt(
        session_factory,
        target=target,
        mode="bonus",
        proximity_points=50,
        radius=radius,
        points=points,
        item_name="Zone Item",
    )
    # Add zone fields (offset from true target).
    async def set_zone():
        async with session_factory() as session:
            item = (await session.execute(select(HuntItem).where(HuntItem.id == ids["item_id"]))).scalar_one()
            item.zone_lat = zone[0]
            item.zone_lon = zone[1]
            item.zone_radius_meters = zone_radius
            await session.commit()
    run(set_zone())
    return ids


def _post_position(client, team_id, lat=34.01, lon=-116.01, accuracy_m=None):
    data = {"lat": str(lat), "lon": str(lon), "team_id": str(team_id)}
    if accuracy_m is not None:
        data["accuracy_m"] = str(accuracy_m)
    return client.post("/api/position", data=data)


# ---------------------------------------------------------------------------
# Auth guards (§6 — all three endpoints reject unauthenticated)
# ---------------------------------------------------------------------------

def test_api_position_rejects_unauthenticated(client):
    assert client.post("/api/position", data={"lat": "0", "lon": "0"}).status_code == 403


def test_api_map_rejects_unauthenticated(client):
    assert client.get("/api/map").status_code == 403


def test_admin_api_map_rejects_unauthenticated(client):
    assert client.get("/admin/map").status_code == 403


def test_map_page_rejects_unauthenticated(client):
    assert client.get("/map", follow_redirects=False).status_code == 403


def test_admin_map_view_rejects_unauthenticated(client):
    assert client.get("/admin/map-view", follow_redirects=False).status_code == 403


# ---------------------------------------------------------------------------
# POST /api/position — validation (§3)
# ---------------------------------------------------------------------------

def test_position_validates_lat_range(client, session_factory, monkeypatch):
    ids = seed_hunt(session_factory)
    monkeypatch.setattr(main, "get_admin_password", lambda: "adminpw")
    _login_team(client)
    assert client.post("/api/position", data={"lat": "91", "lon": "0", "team_id": str(ids["team_id"])}).status_code == 400
    assert client.post("/api/position", data={"lat": "-91", "lon": "0", "team_id": str(ids["team_id"])}).status_code == 400


def test_position_validates_lon_range(client, session_factory, monkeypatch):
    ids = seed_hunt(session_factory)
    monkeypatch.setattr(main, "get_admin_password", lambda: "adminpw")
    _login_team(client)
    assert client.post("/api/position", data={"lat": "0", "lon": "181", "team_id": str(ids["team_id"])}).status_code == 400
    assert client.post("/api/position", data={"lat": "0", "lon": "-181", "team_id": str(ids["team_id"])}).status_code == 400


def test_position_rejects_nan(client, session_factory, monkeypatch):
    ids = seed_hunt(session_factory)
    monkeypatch.setattr(main, "get_admin_password", lambda: "adminpw")
    _login_team(client)
    assert client.post("/api/position", data={"lat": "NaN", "lon": "0", "team_id": str(ids["team_id"])}).status_code == 400
    assert client.post("/api/position", data={"lat": "0", "lon": "Infinity", "team_id": str(ids["team_id"])}).status_code == 400


def test_position_rejects_non_numeric(client, session_factory, monkeypatch):
    ids = seed_hunt(session_factory)
    monkeypatch.setattr(main, "get_admin_password", lambda: "adminpw")
    _login_team(client)
    # "abc" is non-numeric → 400.
    assert client.post("/api/position", data={"lat": "abc", "lon": "0", "team_id": str(ids["team_id"])}).status_code == 400


def test_position_rejects_empty_value(client, session_factory, monkeypatch):
    """Empty string is not a valid coordinate → 400."""
    ids = seed_hunt(session_factory)
    monkeypatch.setattr(main, "get_admin_password", lambda: "adminpw")
    _login_team(client)
    assert client.post("/api/position", data={"lat": "0", "lon": "", "team_id": str(ids["team_id"])}).status_code == 400


# ---------------------------------------------------------------------------
# POST /api/position — rate limiting (§3)
# ---------------------------------------------------------------------------

def test_position_is_rate_limited(client, session_factory, monkeypatch):
    """60 req/min is generous; with limit=5, request 6 triggers 429."""
    ids = seed_hunt(session_factory)
    monkeypatch.setattr(main, "get_admin_password", lambda: "adminpw")
    monkeypatch.setattr(main, "_POSITION_MAX_PER_WINDOW", 5)
    # Clear any lingering rate-limit state from previous tests.
    main._position_attempts.clear()
    _login_team(client)
    statuses = []
    for _ in range(7):
        resp = client.post("/api/position", data={"lat": "34.0", "lon": "-116.0", "team_id": str(ids["team_id"])})
        statuses.append(resp.status_code)
    assert statuses[:5] == [200] * 5
    assert statuses[5] == 429
    assert statuses[6] == 429


# ---------------------------------------------------------------------------
# POST /api/position — records correctly
# ---------------------------------------------------------------------------

def test_position_records_and_prunes(client, session_factory, monkeypatch):
    """Only the latest position per team is retained."""
    ids = seed_hunt(session_factory)
    monkeypatch.setattr(main, "get_admin_password", lambda: "adminpw")
    _login_team(client)

    # Send two positions for the same team.
    assert _post_position(client, ids["team_id"], lat=34.01, lon=-116.01).status_code == 200
    assert _post_position(client, ids["team_id"], lat=34.02, lon=-116.02).status_code == 200

    # The map should reflect only the latest.
    resp = client.get("/api/map")
    assert resp.status_code == 200
    data = resp.json()
    assert data["me"] is not None
    assert data["me"]["lat"] == 34.02
    assert data["me"]["lon"] == -116.02


# ---------------------------------------------------------------------------
# THE KEY SECURITY TEST: team payload leaks no true target (§1 / §6)
# ---------------------------------------------------------------------------

# Distinctive values so a numeric deep-scan cannot collide with unrelated
# fields (points, radius, etc.).
TRUE_TARGET = (34.123456, -116.654321)
DISPLAY_ZONE = (34.987654, -116.012345)
JUDGE_RADIUS = 137.5
DISPLAY_RADIUS = 250.0
ITEM_POINTS = 42


def _walk(obj):
    """Yield every key and every numeric value in a nested JSON structure."""
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


def test_team_map_leaks_no_true_target(client, session_factory, monkeypatch):
    """§1 — the team payload must contain no true target / judging radius.

    The drawn zone is deliberately offset from the true target; leaking the
    target would let a player read the coordinates out of the JavaScript and
    walk straight to the spot. This scans the *serialised* payload for the
    forbidden values anywhere — in any field, nested object or key.
    """
    ids = _seed_zone_hunt(
        session_factory,
        target=TRUE_TARGET,
        zone=DISPLAY_ZONE,
        zone_radius=DISPLAY_RADIUS,
        radius=JUDGE_RADIUS,
        points=ITEM_POINTS,
    )
    monkeypatch.setattr(main, "get_admin_password", lambda: "adminpw")
    _login_team(client)
    assert client.post("/admin/login", data={"password": "adminpw"}).status_code == 200

    team_data = client.get("/api/map").json()
    admin_data = client.get("/admin/map").json()
    team_json = json.dumps(team_data)
    admin_json = json.dumps(admin_data)

    # 1. The admin payload DOES include the true target and judging radius.
    admin_item = next(i for i in admin_data["items"] if i["id"] == ids["item_id"])
    assert admin_item["target_lat"] == TRUE_TARGET[0]
    assert admin_item["target_lon"] == TRUE_TARGET[1]
    assert admin_item["radius_meters"] == JUDGE_RADIUS

    # 2. The team payload carries the *display* zone instead.
    team_item = next(i for i in team_data["items"] if i["id"] == ids["item_id"])
    assert team_item["zone"]["lat"] == DISPLAY_ZONE[0]
    assert team_item["zone"]["lon"] == DISPLAY_ZONE[1]
    assert team_item["zone"]["radius_m"] == DISPLAY_RADIUS

    # 3. No forbidden keys anywhere in the team payload.
    team_keys, team_nums = _keys_and_numbers(team_data)
    for forbidden in ("target_lat", "target_lon", "radius_meters"):
        assert forbidden not in team_keys
        assert forbidden not in team_json

    # 4. No forbidden *values* anywhere in the team payload, at any nesting
    #    depth, and not as raw text either.
    for value in (TRUE_TARGET[0], TRUE_TARGET[1], JUDGE_RADIUS):
        assert value not in team_nums, f"leaked {value} in {team_json}"
        assert str(value) not in team_json, f"leaked {value!r} as text in {team_json}"

    # 5. The display zone value IS present, proving the item wasn't just dropped.
    assert DISPLAY_ZONE[0] in team_nums
    assert DISPLAY_ZONE[1] in team_nums
    assert DISPLAY_RADIUS in team_nums

    # 6. Admin payload differs from the team payload.
    assert team_json != admin_json


# ---------------------------------------------------------------------------
# Team sees only its own position (§6)
# ---------------------------------------------------------------------------

def test_team_map_shows_only_own_position(client, session_factory, monkeypatch):
    """A team's /api/map must show only its own position, never another team's."""
    monkeypatch.setattr(main, "get_admin_password", lambda: "adminpw")

    # Create two teams.
    ids1 = seed_hunt(session_factory, team_name="Team Alpha", passphrase="alpha_secret")
    ids2 = seed_hunt(session_factory, team_name="Team Beta", passphrase="beta_secret", item_name="Item Beta", hunt_name="Test Hunt 2")

    # Beta posts a distinctive position first.
    assert client.post("/team/login", data={"passphrase": "beta_secret"}).status_code == 200
    assert _post_position(client, ids2["team_id"], lat=35.5, lon=-115.5).status_code == 200

    # Log in as Alpha and post its own position.
    assert client.post("/team/login", data={"passphrase": "alpha_secret"}).status_code == 200
    assert _post_position(client, ids1["team_id"], lat=34.01, lon=-116.01).status_code == 200

    # Alpha's map should show only Alpha's own position.
    resp = client.get("/api/map")
    assert resp.status_code == 200
    data = resp.json()
    assert data["me"] is not None
    assert data["me"]["team_id"] == ids1["team_id"]
    assert data["me"]["lat"] == 34.01

    # The team payload must not have a "teams" array, and Beta's position must
    # not appear anywhere in it.
    assert "teams" not in data
    assert "Team Beta" not in json.dumps(data)
    _, numbers = _keys_and_numbers(data)
    assert 35.5 not in numbers
    assert -115.5 not in numbers


# ---------------------------------------------------------------------------
# Admin map includes all teams (§3 / §6)
# ---------------------------------------------------------------------------

def test_admin_map_shows_all_teams(client, session_factory, monkeypatch):
    monkeypatch.setattr(main, "get_admin_password", lambda: "adminpw")

    ids1 = seed_hunt(session_factory, team_name="Team Alpha", passphrase="alpha_secret")
    ids2 = seed_hunt(session_factory, team_name="Team Beta", passphrase="beta_secret", item_name="Item Beta", hunt_name="Admin Test Hunt 2")

    # Log in as Alpha and post position.
    assert client.post("/team/login", data={"passphrase": "alpha_secret"}).status_code == 200
    assert _post_position(client, ids1["team_id"], lat=34.01, lon=-116.01).status_code == 200

    # Log in as admin.
    assert client.post("/admin/login", data={"password": "adminpw"}).status_code == 200
    admin_resp = client.get("/admin/map")
    assert admin_resp.status_code == 200
    data = admin_resp.json()

    assert "teams" in data
    team_names = {t["name"] for t in data["teams"]}
    assert "Team Alpha" in team_names
    assert "Team Beta" in team_names  # Beta has no position but is listed


# ---------------------------------------------------------------------------
# Staleness (§4 / §6)
# ---------------------------------------------------------------------------

def test_old_position_is_stale(client, session_factory, monkeypatch):
    """A position older than STALE_THRESHOLD_SECONDS should be marked stale."""
    ids = seed_hunt(session_factory)
    monkeypatch.setattr(main, "get_admin_password", lambda: "adminpw")
    _login_team(client)

    # Post a position, then monkeypatch the "now" reference to be far in the future.
    assert _post_position(client, ids["team_id"], lat=34.0, lon=-116.0).status_code == 200

    # Force the staleness check by overriding the threshold to 0.
    monkeypatch.setattr(main, "_STALE_THRESHOLD_SECONDS", 0)

    resp = client.get("/api/map")
    assert resp.status_code == 200
    data = resp.json()
    assert data["me"] is not None
    assert data["me"]["stale"] is True


def test_fresh_position_is_not_stale(client, session_factory, monkeypatch):
    ids = seed_hunt(session_factory)
    monkeypatch.setattr(main, "get_admin_password", lambda: "adminpw")
    _login_team(client)

    assert _post_position(client, ids["team_id"], lat=34.0, lon=-116.0).status_code == 200

    resp = client.get("/api/map")
    assert resp.status_code == 200
    data = resp.json()
    assert data["me"] is not None
    # With the default 60s threshold, a just-inserted position is fresh.
    assert data["me"]["stale"] is False


# ---------------------------------------------------------------------------
# Map page renders and references vendored Leaflet (no CDN URL)
# ---------------------------------------------------------------------------

def test_map_page_uses_vendored_leaflet(client, session_factory, monkeypatch):
    """The team map HTML must reference local Leaflet, not a CDN."""
    monkeypatch.setattr(main, "get_admin_password", lambda: "adminpw")
    ids = seed_hunt(session_factory)
    _login_team(client)

    resp = client.get("/map")
    assert resp.status_code == 200
    html = resp.text

    # Must load Leaflet CSS and JS from the vendored path.
    assert '/static/vendor/leaflet/leaflet.css' in html
    assert '/static/vendor/leaflet/leaflet.js' in html

    # Must NOT load from a CDN.
    assert 'cdnjs.cloudflare.com' not in html
    assert 'unpkg.com' not in html
    assert 'leafletjs.com' not in html


def test_admin_map_view_uses_vendored_leaflet(client, session_factory, monkeypatch):
    monkeypatch.setattr(main, "get_admin_password", lambda: "adminpw")
    ids = seed_hunt(session_factory)
    assert client.post("/admin/login", data={"password": "adminpw"}).status_code == 200

    resp = client.get("/admin/map-view")
    assert resp.status_code == 200
    html = resp.text
    assert '/static/vendor/leaflet/leaflet.css' in html
    assert '/static/vendor/leaflet/leaflet.js' in html


# ---------------------------------------------------------------------------
# Items without zone data are absent from the team payload (§1)
# ---------------------------------------------------------------------------

def test_item_without_zone_omitted_from_team_map(client, session_factory, monkeypatch):
    """An item that has no zone_* fields must not appear in the team API response."""
    monkeypatch.setattr(main, "get_admin_password", lambda: "adminpw")
    ids = seed_hunt(session_factory)  # no zone fields set (all None)
    _login_team(client)

    resp = client.get("/api/map")
    assert resp.status_code == 200
    data = resp.json()
    # There should be no items since none have zone data.
    assert len(data["items"]) == 0


# ---------------------------------------------------------------------------
# Admin map shows unapproved submissions
# ---------------------------------------------------------------------------

def test_admin_map_includes_unapproved(client, session_factory, monkeypatch):
    """Admin map must include submissions that are not yet verified."""
    import app.main as main
    from dbutil import fetch_submissions
    from exif_helpers import build_jpeg

    ids = _seed_zone_hunt(session_factory)
    monkeypatch.setattr(main, "get_admin_password", lambda: "adminpw")
    monkeypatch.setattr(main, "get_hunt_state", lambda: {"is_open": True, "ends_at": None, "duration_minutes": 160, "seconds_remaining": 600})

    # Submit a rejected photo (mock AI rejection) to create an unapproved sub with GPS.
    monkeypatch.setattr(main, "review_image", lambda *a, **kw: {
        "is_target": True, "confidence": 0.95,
        "matched_required": [], "missed_required": ["shows a thing"],
        "matched_bonus": [], "missed_bonus": [], "reason": "Nope.",
    })

    _login_team(client)
    now = datetime.datetime.now(timezone.utc)
    dt = now.strftime("%Y:%m:%d %H:%M:%S")
    jpeg = build_jpeg(datetime_original=dt, offset_original="+00:00", gps=(34.01, -116.01)).getvalue()
    sub_resp = client.post("/submit", data={"item_id": str(ids["item_id"]), "team_id": str(ids["team_id"])}, files={"file": ("photo.jpg", jpeg, "image/jpeg")})
    assert sub_resp.status_code == 200

    # Admin map.
    assert client.post("/admin/login", data={"password": "adminpw"}).status_code == 200
    admin_resp = client.get("/admin/map")
    assert admin_resp.status_code == 200
    data = admin_resp.json()
    submissions = data.get("submissions", [])
    assert len(submissions) >= 1
    unverified = [s for s in submissions if not s.get("verified")]
    assert len(unverified) >= 1


# ---------------------------------------------------------------------------
# §4 — tiles fail / poll fails: verified in a REAL headless browser
# ---------------------------------------------------------------------------
#
# These tests render the actual /map page, point the tile URL at a closed port,
# stub fetch, and load the result in headless Chrome. They assert on the live
# DOM (computed styles + rendered Leaflet layers), not on the HTML source, so
# they genuinely exercise the fallback rather than implying it.

PROBE_JS = """
<script>
setTimeout(function () {
    function display(id) {
        var el = document.getElementById(id);
        return el ? getComputedStyle(el).display : 'absent';
    }
    var out = {
        leaflet: (typeof L !== 'undefined'),
        tileNotice: display('tile-fallback-notice'),
        pollError: display('poll-error'),
        circles: document.querySelectorAll('#map path.leaflet-interactive').length,
        markers: document.querySelectorAll('#map .map-marker-icon').length,
        legend: document.querySelectorAll('#legend .legend-toggle').length
    };
    var d = document.createElement('div');
    d.id = 'probe-output';
    d.textContent = JSON.stringify(out);
    document.body.appendChild(d);
}, 4000);
</script>
"""


def _run_map_page(client, tmp_path, monkeypatch, *, tile_url, fetch_js):
    """Render /map, stub fetch/tiles, load in headless Chrome, return probe dict."""
    import app.config as app_config
    monkeypatch.setattr(app_config, "get_tile_url", lambda: tile_url)

    html = client.get("/map").text
    # The page is loaded over file://; point vendored assets at the real files.
    html = html.replace("/static/", REPO_ROOT + "/static/")
    # Stub fetch before the page's own scripts run.
    html = html.replace("</head>", "<script>" + fetch_js + "</script></head>")
    html = html.replace("</body>", PROBE_JS + "</body>")

    harness = tmp_path / "map_harness.html"
    harness.write_text(html, encoding="utf-8")

    proc = subprocess.run(
        [BROWSER, "--headless", "--no-sandbox", "--disable-gpu",
         "--virtual-time-budget=9000", "--dump-dom", "file://" + str(harness)],
        capture_output=True, text=True, timeout=120,
    )
    match = re.search(r'<div id="probe-output">(\{.*?\})</div>', proc.stdout)
    assert match, "browser probe did not run; stdout head:\n" + proc.stdout[:800]
    return json.loads(match.group(1))


@pytest.mark.skipif(BROWSER is None, reason="no headless browser available")
def test_map_still_works_when_tiles_fail(client, session_factory, monkeypatch, tmp_path):
    """§4 — a dead tile server must not blank the page: notice + markers render.

    Real headless-Chrome rendering of the actual /map page with the tile URL
    pointed at a closed port. Assertions are on the live DOM, not the HTML.
    """
    ids = _seed_zone_hunt(session_factory)
    _login_team(client)

    payload = {
        "items": [{"id": ids["item_id"], "name": "Zone Item", "points": 100,
                   "zone": {"lat": 34.05, "lon": -116.05, "radius_m": 200.0}}],
        "me": {"team_id": ids["team_id"], "name": "Team Alpha", "lat": 34.01,
               "lon": -116.01, "accuracy_m": 5.0, "age_seconds": 1, "stale": False},
        "submissions": [],
    }
    fetch_js = (
        "window.fetch=function(){return Promise.resolve({ok:true,json:function(){"
        "return Promise.resolve(" + json.dumps(payload) + ");}});};"
    )

    out = _run_map_page(
        client, tmp_path, monkeypatch,
        tile_url="http://127.0.0.1:1/{z}/{x}/{y}.png",
        fetch_js=fetch_js,
    )

    assert out["leaflet"] is True, out
    assert out["circles"] >= 1, out           # item zone drawn
    assert out["markers"] >= 1, out           # team position drawn
    assert out["tileNotice"] == "block", out  # visible "base map unavailable" notice
    assert out["pollError"] == "none", out    # data loaded fine


@pytest.mark.skipif(BROWSER is None, reason="no headless browser available")
def test_map_fails_closed_on_poll_error(client, session_factory, monkeypatch, tmp_path):
    """§4 — a failed poll clears markers and shows the error, never stale data."""
    _seed_zone_hunt(session_factory)
    _login_team(client)

    out = _run_map_page(
        client, tmp_path, monkeypatch,
        tile_url="http://127.0.0.1:1/{z}/{x}/{y}.png",
        fetch_js="window.fetch=function(){return Promise.reject(new Error('offline'));};",
    )

    assert out["pollError"] == "block", out
    assert out["circles"] == 0, out
    assert out["markers"] == 0, out


# ---------------------------------------------------------------------------
# §5 — the map is reachable from both dashboards
# ---------------------------------------------------------------------------

def test_team_dashboard_links_to_map(client, session_factory):
    seed_hunt(session_factory)
    _login_team(client)
    html = client.get("/hunt/dashboard").text
    assert 'href="/map"' in html


def test_admin_dashboard_links_to_map(client, session_factory, monkeypatch):
    monkeypatch.setattr(main, "get_admin_password", lambda: "adminpw")
    assert client.post("/admin/login", data={"password": "adminpw"}).status_code == 200
    html = client.get("/admin/dashboard").text
    assert 'href="/admin/map-view"' in html