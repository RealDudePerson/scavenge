"""§1.1 / §4 — EXIF capture-time extraction.

The first test is the load-bearing one: it must FAIL against the pre-fix
``get_photo_taken_at`` (which reads IFD0 DateTime and applies the wrong tag's
offset) and pass after the fix.
"""

import datetime

from PIL import Image

from app.main import get_photo_taken_at
from exif_helpers import build_jpeg, utc


def _read(buf):
    with Image.open(buf) as img:
        return get_photo_taken_at(img)


def test_datetime_original_wins_with_its_own_offset():
    """Camera-shaped file: IFD0 DateTime differs from DateTimeOriginal.

    Photo was really taken 2024-06-01 09:30:00 -07:00 == 2024-06-01 16:30 UTC.
    The pre-fix code returns the IFD0 file-write time with the *original*
    offset applied: 2024-01-01 07:00 UTC (153 days off).
    """
    buf = build_jpeg(
        ifd0_datetime="2024:01:01 00:00:00",
        datetime_original="2024:06:01 09:30:00",
        datetime_digitized="2024:06:01 09:30:05",
        offset_original="-07:00",
        offset_digitized="-07:00",
    )
    assert _read(buf) == utc(2024, 6, 1, 16, 30, 0)


def test_only_datetime_original_present_returns_it():
    """DateTimeOriginal lives in the sub-IFD; it must be found, not skipped."""
    buf = build_jpeg(datetime_original="2024:06:01 09:30:00")
    result = _read(buf)
    assert result is not None
    # No offset tag -> naive datetime, treated as server-local by the caller.
    assert result == datetime.datetime(2024, 6, 1, 9, 30, 0)


def test_datetime_digitized_used_when_original_absent():
    buf = build_jpeg(
        ifd0_datetime="2024:01:01 00:00:00",
        datetime_digitized="2024:06:01 09:30:05",
        offset_digitized="-07:00",
    )
    assert _read(buf) == utc(2024, 6, 1, 16, 30, 5)


def test_offset_of_a_different_tag_is_not_applied():
    """OffsetTimeDigitized must not be applied to DateTimeOriginal."""
    buf = build_jpeg(
        datetime_original="2024:06:01 09:30:00",
        datetime_digitized="2024:06:01 09:30:05",
        offset_digitized="-07:00",  # belongs to Digitized, not Original
    )
    # DateTimeOriginal has no offset of its own -> naive, unshifted.
    assert _read(buf) == datetime.datetime(2024, 6, 1, 9, 30, 0)


def test_ifd0_datetime_is_last_resort():
    buf = build_jpeg(ifd0_datetime="2024:06:01 09:30:00")
    assert _read(buf) == datetime.datetime(2024, 6, 1, 9, 30, 0)


def test_no_timestamp_at_all_returns_none():
    buf = build_jpeg()
    assert _read(buf) is None
