"""§2.2 / §4 — server-side GPS parsing from the stored original."""

import pytest
from PIL import Image

from app.main import parse_gps
from exif_helpers import build_jpeg, null_island_gps


def _read(buf):
    with Image.open(buf) as img:
        return parse_gps(img)


def test_gps_present_returns_coordinates():
    buf = build_jpeg(gps=(34.0522, -116.1234))
    result = _read(buf)
    assert result is not None
    lat, lon = result
    assert lat == pytest.approx(34.0522, abs=1e-3)
    assert lon == pytest.approx(-116.1234, abs=1e-3)


def test_southern_western_hemispheres_are_signed():
    buf = build_jpeg(gps=(-33.8651, 151.2099))  # Sydney-ish, but S and E
    lat, lon = _read(buf)
    assert lat == pytest.approx(-33.8651, abs=1e-3)
    assert lon == pytest.approx(151.2099, abs=1e-3)


def test_gps_absent_returns_none():
    buf = build_jpeg(datetime_original="2024:06:01 09:30:00")
    assert _read(buf) is None


def test_no_exif_at_all_returns_none():
    buf = build_jpeg()
    assert _read(buf) is None


# --- null-island redaction sentinel (Android MediaProvider) ---

def test_redaction_signature_zero_over_one_returns_none():
    """Tags present but 0/1 -> 0.0: the Android redaction signature, not a fix."""
    buf = build_jpeg(gps_raw=null_island_gps(denominator=1))
    assert _read(buf) is None


def test_redaction_signature_zero_over_zero_returns_none():
    """0/0 -> NaN already fails the range check and returns None (verified, not assumed)."""
    buf = build_jpeg(gps_raw=null_island_gps(denominator=0))
    assert _read(buf) is None


def test_real_coordinate_on_equator_still_parses():
    """Over-correction guard: a zero latitude is a real fix, not the sentinel."""
    lat, lon = _read(build_jpeg(gps=(0.0, -116.5)))
    assert lat == pytest.approx(0.0, abs=1e-6)
    assert lon == pytest.approx(-116.5, abs=1e-3)


def test_real_coordinate_on_prime_meridian_still_parses():
    """Over-correction guard: a zero longitude is a real fix, not the sentinel."""
    lat, lon = _read(build_jpeg(gps=(34.1, 0.0)))
    assert lat == pytest.approx(34.1, abs=1e-3)
    assert lon == pytest.approx(0.0, abs=1e-6)
