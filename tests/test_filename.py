"""§1.3 / §4 — upload filename sanitisation cannot escape the upload directory."""

import os

import pytest

from app.config import ORIGINALS_DIR
from app.main import build_original_filename, sanitize_filename


@pytest.mark.parametrize(
    "evil, expected",
    [
        ("../../etc/passwd", "passwd"),
        ("/etc/passwd", "passwd"),
        ("..\\..\\windows\\system32\\cmd.exe", "cmd.exe"),
        ("....//....//etc/shadow", "shadow"),
        ("/", "upload"),
        ("..", "upload"),
        ("...", "upload"),
        ("", "upload"),
        (None, "upload"),
        ("\x00\x00", "upload"),
        ("photo (1).JPG", "photo__1_.JPG"),
        ("my photo!.png", "my_photo_.png"),
    ],
)
def test_sanitize_filename(evil, expected):
    assert sanitize_filename(evil) == expected


def test_sanitize_strips_directory_components():
    result = sanitize_filename("../../etc/passwd")
    assert "/" not in result
    assert "\\" not in result
    assert ".." not in result


def test_sanitize_caps_length():
    result = sanitize_filename("a" * 500 + ".jpg")
    assert len(result) <= 100
    assert result.endswith(".jpg")


@pytest.mark.parametrize(
    "evil",
    ["../../etc/passwd", "/etc/passwd", "..\\..\\boot.ini", "....//x/y/z", "..", ""],
)
def test_generated_path_stays_inside_originals(evil):
    filename = build_original_filename(evil)
    # uuid4 prefix is retained
    assert len(filename) > 32 and filename[32] == "_"
    full = os.path.realpath(os.path.join(ORIGINALS_DIR, filename))
    root = os.path.realpath(ORIGINALS_DIR)
    assert os.path.commonpath([full, root]) == root
    assert os.path.dirname(filename) == ""


def test_uuid_prefix_differs_between_calls():
    a = build_original_filename("photo.jpg")
    b = build_original_filename("photo.jpg")
    assert a != b
    assert a.endswith("_photo.jpg")
