"""§2.3 / §4 — proximity scoring, including exact boundaries.

``score_proximity`` is exercised with ``haversine_m`` monkeypatched to a fixed
distance so the boundary comparisons (``d == radius``,
``d == radius × near_multiplier``) are deterministic rather than subject to
floating-point drift in a hand-built coordinate.
"""

import pytest

import app.main as main
from app.main import haversine_m, score_proximity
from app.models import HuntItem


def make_item(**overrides):
    fields = dict(
        target_lat=34.0,
        target_lon=-116.0,
        radius_meters=100.0,
        near_multiplier=2.0,
        proximity_points=50,
        proximity_mode="bonus",
    )
    fields.update(overrides)
    return HuntItem(**fields)


def with_distance(monkeypatch, distance):
    monkeypatch.setattr(main, "haversine_m", lambda *args: distance)


# --- status / points table ---

def test_hit_awards_full_points(monkeypatch):
    with_distance(monkeypatch, 40.0)
    status, points, distance = score_proximity(make_item(), (34.0, -116.0))
    assert status == "hit"
    assert points == 50
    assert distance == 40.0


def test_near_awards_half_points(monkeypatch):
    with_distance(monkeypatch, 150.0)
    status, points, _ = score_proximity(make_item(), (34.0, -116.0))
    assert status == "near"
    assert points == 25


def test_far_awards_zero(monkeypatch):
    with_distance(monkeypatch, 5000.0)
    status, points, _ = score_proximity(make_item(), (34.0, -116.0))
    assert status == "far"
    assert points == 0


def test_no_gps_is_missing(monkeypatch):
    with_distance(monkeypatch, 0.0)
    status, points, distance = score_proximity(make_item(), None)
    assert status == "missing"
    assert points == 0
    assert distance is None


def test_no_target_is_no_target():
    status, points, distance = score_proximity(
        make_item(target_lat=None, target_lon=None), (34.0, -116.0)
    )
    assert status == "no_target"
    assert points == 0
    assert distance is None


def test_mode_off_is_off(monkeypatch):
    with_distance(monkeypatch, 0.0)
    status, points, distance = score_proximity(
        make_item(proximity_mode="off"), (34.0, -116.0)
    )
    assert status == "off"
    assert points == 0
    assert distance is None


def test_no_target_beats_off():
    """Table order: no_target is checked before off."""
    status, _, _ = score_proximity(
        make_item(target_lat=None, target_lon=None, proximity_mode="off"), (34.0, -116.0)
    )
    assert status == "no_target"


def test_off_beats_missing():
    status, _, _ = score_proximity(make_item(proximity_mode="off"), None)
    assert status == "off"


# --- exact boundaries ---

def test_exact_radius_is_hit(monkeypatch):
    with_distance(monkeypatch, 100.0)
    assert score_proximity(make_item(), (34.0, -116.0))[0] == "hit"


def test_just_outside_radius_is_near(monkeypatch):
    with_distance(monkeypatch, 100.0000001)
    assert score_proximity(make_item(), (34.0, -116.0))[0] == "near"


def test_exact_outer_ring_is_near(monkeypatch):
    with_distance(monkeypatch, 200.0)
    assert score_proximity(make_item(), (34.0, -116.0))[0] == "near"


def test_just_outside_outer_ring_is_far(monkeypatch):
    with_distance(monkeypatch, 200.0000001)
    assert score_proximity(make_item(), (34.0, -116.0))[0] == "far"


# --- haversine itself ---

def test_haversine_same_point_is_zero():
    assert haversine_m(34.0, -116.0, 34.0, -116.0) == pytest.approx(0.0, abs=1e-6)


def test_haversine_one_degree_of_longitude_at_equator():
    # ~111.19 km per degree of longitude at the equator.
    assert haversine_m(0.0, 0.0, 0.0, 1.0) == pytest.approx(111194.9, rel=1e-3)


def test_haversine_is_symmetric():
    a = haversine_m(34.0, -116.0, 34.1, -115.9)
    b = haversine_m(34.1, -115.9, 34.0, -116.0)
    assert a == pytest.approx(b, rel=1e-9)
