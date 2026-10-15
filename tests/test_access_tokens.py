"""Opaque conversation access tokens: generation, hashing and verification."""

import hashlib

import pytest

from app.core.security import (
    ACCESS_TOKEN_PREFIX,
    generate_access_token,
    hash_access_token,
    verify_access_token,
)


def test_tokens_are_random_prefixed_and_long() -> None:
    tokens = {generate_access_token() for _ in range(200)}

    assert len(tokens) == 200
    for token in tokens:
        assert token.startswith(ACCESS_TOKEN_PREFIX)
        assert len(token) == len(ACCESS_TOKEN_PREFIX) + 43  # 32 random bytes, base64url
        assert token.isascii() and "/" not in token and "+" not in token  # URL-safe


def test_hash_is_sha256_hex_and_not_the_token() -> None:
    token = generate_access_token()

    digest = hash_access_token(token)

    assert digest == hashlib.sha256(token.encode()).hexdigest()
    assert len(digest) == 64
    assert token not in digest


def test_verify_accepts_only_the_matching_token() -> None:
    token, other = generate_access_token(), generate_access_token()
    stored = hash_access_token(token)

    assert verify_access_token(token, stored)
    assert not verify_access_token(other, stored)
    assert not verify_access_token(token, hash_access_token(other))


@pytest.mark.parametrize(
    "candidate",
    [None, "", "   ", "cst_short", "Bearer cst_x", "x" * 10_000, "cst_" + "a" * 42 + "!"],
)
def test_verify_rejects_missing_and_malformed_tokens(candidate: str | None) -> None:
    stored = hash_access_token(generate_access_token())

    assert not verify_access_token(candidate, stored)


def test_verify_without_stored_hash_is_false() -> None:
    assert not verify_access_token(generate_access_token(), None)


def test_malformed_token_never_matches_even_its_own_hash() -> None:
    # Only tokens in the issued format can ever be accepted.
    assert not verify_access_token("", hash_access_token(""))
    assert not verify_access_token("guess", hash_access_token("guess"))
