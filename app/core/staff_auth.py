"""Staff authentication for ticket operations (Stage 10).

Changing a ticket's status (assigning, resolving, closing) is a staff operation. Staff
prove who they are with a personal bearer token, like the conversation tokens in
``app.core.security``: 32 random bytes, prefixed ``stf_``, shown once when created and
stored only as a SHA-256 hash, here in the ``STAFF_TOKEN_HASHES`` setting as
``staff_id:hash`` entries (``python -m scripts.create_staff_token <staff_id>``).

``authenticate`` compares the token against every configured hash in constant time, so
timing reveals neither whether a token is close nor which staff member it belongs to.
With no entries configured, nobody is staff and every staff operation is refused.

This is a prototype scheme for a small team: there is no staff login, expiry or
per-operation role yet. Revoke a token by removing its entry and restarting.
"""

import hashlib
import hmac
import re
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Self

from app.core.config import Settings

STAFF_TOKEN_PREFIX = "stf_"  # noqa: S105 - a public prefix, not a secret
STAFF_TOKEN_BYTES = 32
_TOKEN_PATTERN = re.compile(rf"^{STAFF_TOKEN_PREFIX}[A-Za-z0-9_-]{{43}}$")


class StaffAuthenticationError(Exception):
    """The token is missing, malformed or not a configured staff token."""

    def __init__(self) -> None:
        super().__init__("staff authentication failed")


@dataclass(frozen=True)
class StaffMember:
    """An authenticated staff member. Only ``StaffAuthenticator`` creates these."""

    staff_id: str


def generate_staff_token() -> str:
    """A new random staff token. Show it to its owner once; configure only its hash."""
    return STAFF_TOKEN_PREFIX + secrets.token_urlsafe(STAFF_TOKEN_BYTES)


def hash_staff_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class StaffAuthenticator:
    """Recognises staff tokens from their configured hashes."""

    def __init__(self, entries: Iterable[str] = ()) -> None:
        """
        Args:
            entries: ``staff_id:sha256hex`` strings (``STAFF_TOKEN_HASHES``, validated
                by the settings).
        """
        self._hashes: dict[str, str] = {}
        for entry in entries:
            staff_id, digest = entry.split(":", 1)
            self._hashes[staff_id] = digest

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        return cls(settings.staff_token_hashes)

    @property
    def staff_ids(self) -> frozenset[str]:
        """The configured staff IDs (who a ticket may be assigned to)."""
        return frozenset(self._hashes)

    def authenticate(self, token: str | None) -> StaffMember:
        """The staff member the token belongs to.

        Raises:
            StaffAuthenticationError: missing, malformed or unknown token, or no staff
                configured.
        """
        candidate = token if token is not None and _TOKEN_PATTERN.fullmatch(token) else ""
        digest = hash_staff_token(candidate)
        found: str | None = None
        for staff_id, expected in self._hashes.items():  # every entry, always
            if hmac.compare_digest(digest, expected):
                found = staff_id
        if found is None or not candidate:
            raise StaffAuthenticationError()
        return StaffMember(found)
