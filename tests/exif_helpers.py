"""Helpers for building camera-shaped JPEGs in tests.

Pillow's ``Image.Exif`` object only knows IFD0 by default: assigning
``exif[36867]`` (DateTimeOriginal) writes it into IFD0, which is *not* what a
camera does. A real phone writes DateTimeOriginal/DateTimeDigitized and the
EXIF 2.31 offset tags into the **Exif sub-IFD**. To exercise the code paths
that read the sub-IFD we have to build the nested structure explicitly, which
is what ``build_jpeg`` below does.
"""

import datetime
from io import BytesIO

from PIL import Image, ExifTags, TiffImagePlugin

# EXIF tag ids
IFD0_DATETIME = 306           # DateTime (file-write time)
DATETIME_ORIGINAL = 36867     # DateTimeOriginal
DATETIME_DIGITIZED = 36868    # DateTimeDigitized
OFFSET_ORIGINAL = 0x9010      # OffsetTimeOriginal (EXIF 2.31)
OFFSET_DIGITIZED = 0x9011     # OffsetTimeDigitized (EXIF 2.31)
ORIENTATION = 274


def _rat(numerator, denominator=1):
    return TiffImagePlugin.IFDRational(numerator, denominator)


def _dms(value):
    """Decimal degrees -> (deg, min, sec) rational tuple, as GPS stores them."""
    value = abs(value)
    degrees = int(value)
    rem = (value - degrees) * 60
    minutes = int(rem)
    seconds = (rem - minutes) * 60
    # Keep seconds as a rational with 1/10000 s resolution.
    return (_rat(degrees), _rat(minutes), _rat(int(round(seconds * 10000)), 10000))


def build_jpeg(
    *,
    ifd0_datetime=None,
    datetime_original=None,
    datetime_digitized=None,
    offset_original=None,
    offset_digitized=None,
    gps=None,
    orientation=None,
    size=(60, 40),
    color="white",
):
    """Build an in-memory JPEG with a camera-shaped EXIF block.

    ``gps`` is a ``(lat, lon)`` tuple in decimal degrees. ``orientation`` is an
    EXIF orientation value (e.g. 6 = rotate 90° CW on display).
    """
    exif = Image.Exif()
    if ifd0_datetime is not None:
        exif[IFD0_DATETIME] = ifd0_datetime
    if orientation is not None:
        exif[ORIENTATION] = orientation

    sub_ifd = {}
    if datetime_original is not None:
        sub_ifd[DATETIME_ORIGINAL] = datetime_original
    if datetime_digitized is not None:
        sub_ifd[DATETIME_DIGITIZED] = datetime_digitized
    if offset_original is not None:
        sub_ifd[OFFSET_ORIGINAL] = offset_original
    if offset_digitized is not None:
        sub_ifd[OFFSET_DIGITIZED] = offset_digitized
    if sub_ifd:
        exif[ExifTags.IFD.Exif] = sub_ifd

    if gps is not None:
        lat, lon = gps
        exif[ExifTags.IFD.GPSInfo] = {
            1: "N" if lat >= 0 else "S",
            2: _dms(lat),
            3: "E" if lon >= 0 else "W",
            4: _dms(lon),
        }

    img = Image.new("RGB", size, color)
    buf = BytesIO()
    img.save(buf, "JPEG", exif=exif)
    buf.seek(0)
    return buf


def utc(year, month, day, hour, minute, second=0):
    return datetime.datetime(year, month, day, hour, minute, second, tzinfo=datetime.timezone.utc)
