"""Opaque bearer tokens for conversation access (local prototype).

A token is 32 random bytes from the operating system's CSPRNG (256 bits), URL-safe
base64 encoded and prefixed with ``cst_`` so that leaked tokens are easy to recognise
(for example by secret scanners). Only its SHA-256 hash is stored.

A plain (unsalted) SHA-256 is appropriate here because the token is long and random:
there is no dictionary to try, and a stolen hash cannot be turned back into a token.
Passwords, which people choose, would need a slow salted hash instead.

Tokens are never logged and never placed in URLs; they travel in the
``Authorization: Bearer`` header.
"""

import hashlib
import hmac
import re
import secrets

ACCESS_TOKEN_PREFIX = "cst_"  # noqa: S105 - a public prefix, not a secret
ACCESS_TOKEN_BYTES = 32
# Prefix + 43 base64url characters; anything else cannot be a token we issued.
_TOKEN_PATTERN = re.compile(rf"^{ACCESS_TOKEN_PREFIX}[A-Za-z0-9_-]{{43}}$")
# Compared against when there is no stored hash, so a missing session costs the same work.
_DUMMY_HASH = hashlib.sha256(b"no session").hexdigest()


def generate_access_token() -> str:
    """A new random access token. Show it to the client once; store only its hash."""
    return ACCESS_TOKEN_PREFIX + secrets.token_urlsafe(ACCESS_TOKEN_BYTES)


def hash_access_token(token: str) -> str:
    """The SHA-256 hex digest stored instead of the token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_access_token(token: str | None, stored_hash: str | None) -> bool:
    """Whether ``token`` matches ``stored_hash``, in constant time.

    Missing or malformed tokens and a missing hash all return False after the same
    hash-and-compare work, so timing does not reveal which case applied.
    """
    candidate = token if token is not None and _TOKEN_PATTERN.fullmatch(token) else ""
    expected = stored_hash or _DUMMY_HASH
    matches = hmac.compare_digest(hash_access_token(candidate), expected)
    return matches and bool(candidate) and stored_hash is not None
