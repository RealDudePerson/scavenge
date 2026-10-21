"""Independent verification (written by Hermes) of the null-island sentinel fix.

Hand-builds the GPS IFD rather than reusing the repo's fixture helper, and validates
each fixture by reading the raw tags AND refs back before asserting.

The ref validation matters: an earlier version of this builder wrote the 2-byte ASCII
ref tags as an *offset* when TIFF stores values of <= 4 bytes INLINE. Pillow read
garbage for the ref, the "W" negation never applied, and negative longitudes came out
positive - which looked exactly like a sign bug in the code. A fixture that is not
validated can produce a confident, wrong verdict.
"""
import io
import struct

import pytest
from PIL import Image, ExifTags

from app.main import parse_gps


def _rat(num, den=1):
    return struct.pack("<II", num, den)


def _dms(deg, minute, sec):
    return _rat(deg) + _rat(minute) + _rat(sec)


def _inline(tag, typ, count, raw):
    """Value of <= 4 bytes, stored in the value field itself."""
    return struct.pack("<HHI", tag, typ, count) + raw.ljust(4, b"\x00")[:4]


def _offset(tag, typ, count, off):
    """Value larger than 4 bytes, stored at an offset."""
    return struct.pack("<HHI", tag, typ, count) + struct.pack("<I", off)


def build_gps(lat_ref, lat_dms, lon_ref, lon_dms):
    ifd0_size = 2 + 12 + 4
    gps_off = 8 + ifd0_size
    lat_dms_off = gps_off + (2 + 12 * 4 + 4)
    lat_b = _dms(*lat_dms)
    lon_dms_off = lat_dms_off + len(lat_b)
    lon_b = _dms(*lon_dms)

    ifd0 = struct.pack("<H", 1) + _offset(0x8825, 4, 1, gps_off) + struct.pack("<I", 0)
    gps = struct.pack("<H", 4)
    gps += _inline(1, 2, 2, lat_ref.encode() + b"\x00")
    gps += _offset(2, 5, 3, lat_dms_off)
    gps += _inline(3, 2, 2, lon_ref.encode() + b"\x00")
    gps += _offset(4, 5, 3, lon_dms_off)
    gps += struct.pack("<I", 0)

    blob = (b"Exif\x00\x00" + b"II\x2a\x00" + struct.pack("<I", 8)
            + ifd0 + gps + lat_b + lon_b)
    im = Image.new("RGB", (16, 16), "red")
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=blob)
    return buf.getvalue()


def _gps_ifd(data):
    return Image.open(io.BytesIO(data)).getexif().get_ifd(ExifTags.IFD.GPSInfo) or {}


def _fixture(data, lat_ref, lon_ref):
    """Validate the fixture really carries the refs and DMS we meant to write."""
    gps = _gps_ifd(data)
    missing = [t for t in (1, 2, 3, 4) if t not in gps]
    assert not missing, f"GPS IFD missing {missing}; has {sorted(gps.keys())}"

    def s(v):
        return v.decode("utf-8", "ignore").strip("\x00") if isinstance(v, bytes) else str(v)

    assert s(gps[1]) == lat_ref, f"lat ref is {s(gps[1])!r}, wanted {lat_ref!r}"
    assert s(gps[3]) == lon_ref, f"lon ref is {s(gps[3])!r}, wanted {lon_ref!r}"
    return data


def test_redaction_signature_is_treated_as_absent():
    """All-zero tags are Android's GPS redaction, not a fix at Null Island."""
    data = _fixture(build_gps("N", (0, 0, 0), "W", (0, 0, 0)), "N", "W")
    assert parse_gps(Image.open(io.BytesIO(data))) is None


@pytest.mark.parametrize(
    "lat_ref,lat_dms,lon_ref,lon_dms,want",
    [
        # over-correction guards: a real location containing a zero must still parse
        ("N", (0, 0, 0), "W", (116, 30, 0), (0.0, -116.5)),      # on the equator
        ("N", (34, 6, 0), "W", (0, 0, 0), (34.1, 0.0)),          # on the prime meridian
        # ordinary coordinates, both hemispheres
        ("N", (34, 6, 0), "W", (116, 30, 0), (34.1, -116.5)),
        ("S", (33, 54, 0), "E", (151, 12, 0), (-33.9, 151.2)),
    ],
)
def test_real_coordinates_still_parse(lat_ref, lat_dms, lon_ref, lon_dms, want):
    data = _fixture(build_gps(lat_ref, lat_dms, lon_ref, lon_dms), lat_ref, lon_ref)
    got = parse_gps(Image.open(io.BytesIO(data)))
    assert got is not None, f"{want} was rejected"
    assert got == pytest.approx(want), f"got {got}, wanted {want}"
