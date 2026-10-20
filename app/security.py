"""Passphrase hashing and verification (§3.2).

No new dependency: PBKDF2-HMAC-SHA256 from the standard library, with a random
per-team salt and a constant-time comparison on verify. Stored format:

    pbkdf2_sha256$<iterations>$<salt-hex>$<hash-hex>
"""

import hashlib
import hmac
import secrets

PBKDF2_ITERATIONS = 200_000
_ALGO = "pbkdf2_sha256"
_SALT_BYTES = 16


def hash_passphrase(passphrase: str) -> str:
    """Hash a plaintext passphrase for storage."""
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"{_ALGO}${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_passphrase(passphrase: str, stored: str) -> bool:
    """Constant-time check of a plaintext passphrase against a stored hash."""
    if not passphrase or not stored or not isinstance(stored, str):
        return False
    try:
        algo, iterations_str, salt_hex, digest_hex = stored.split("$", 3)
        if algo != _ALGO:
            return False
        iterations = int(iterations_str)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except (ValueError, AttributeError):
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(candidate, expected)
