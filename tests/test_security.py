"""§3.2 / §4 — passphrase hashing."""

from app.security import hash_passphrase, verify_passphrase


def test_stored_value_is_not_the_plaintext():
    stored = hash_passphrase("alpha_secret")
    assert "alpha_secret" not in stored
    assert stored.startswith("pbkdf2_sha256$")


def test_correct_passphrase_verifies():
    stored = hash_passphrase("alpha_secret")
    assert verify_passphrase("alpha_secret", stored) is True


def test_wrong_passphrase_does_not_verify():
    stored = hash_passphrase("alpha_secret")
    assert verify_passphrase("alpha_secret ", stored) is False
    assert verify_passphrase("beta_secret", stored) is False
    assert verify_passphrase("", stored) is False


def test_same_passphrase_hashes_differently():
    """Random salt means two hashes of the same passphrase differ."""
    assert hash_passphrase("x") != hash_passphrase("x")


def test_malformed_stored_value_does_not_crash():
    assert verify_passphrase("x", "not-a-hash") is False
    assert verify_passphrase("x", "") is False
    assert verify_passphrase("x", None) is False
