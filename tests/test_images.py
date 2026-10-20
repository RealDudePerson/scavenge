"""§1.2 / §6 — thumbnails are upright for a rotated photo."""

from PIL import Image

import app.main as main
from exif_helpers import build_jpeg


def test_thumbnails_apply_exif_orientation(tmp_path, monkeypatch):
    thumb_dir = tmp_path / "thumb"
    display_dir = tmp_path / "display"
    thumb_dir.mkdir()
    display_dir.mkdir()
    monkeypatch.setattr(main, "THUMB_DIR", str(thumb_dir))
    monkeypatch.setattr(main, "DISPLAY_DIR", str(display_dir))

    # Landscape pixel buffer with EXIF orientation 6 -> displays as portrait.
    original = tmp_path / "original.jpg"
    original.write_bytes(build_jpeg(orientation=6, size=(60, 40)).getvalue())

    main.make_thumbnails(str(original), "base")

    with Image.open(thumb_dir / "base.jpg") as thumb:
        assert thumb.height > thumb.width
    with Image.open(display_dir / "base.jpg") as display:
        assert display.height > display.width


def test_unrotated_photo_keeps_aspect(tmp_path, monkeypatch):
    thumb_dir = tmp_path / "thumb"
    display_dir = tmp_path / "display"
    thumb_dir.mkdir()
    display_dir.mkdir()
    monkeypatch.setattr(main, "THUMB_DIR", str(thumb_dir))
    monkeypatch.setattr(main, "DISPLAY_DIR", str(display_dir))

    original = tmp_path / "original.jpg"
    original.write_bytes(build_jpeg(size=(60, 40)).getvalue())

    main.make_thumbnails(str(original), "base")

    with Image.open(thumb_dir / "base.jpg") as thumb:
        assert thumb.width > thumb.height
