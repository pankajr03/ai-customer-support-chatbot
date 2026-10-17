"""Staff authentication and roles (Stage 10, roles added in Stage 11).

Ticket handling, FAQ management and analytics are staff operations. Staff prove who
they are with a personal bearer token, like the conversation tokens in
``app.core.security``: 32 random bytes, prefixed ``stf_``, shown once when created and
stored only as a SHA-256 hash, here in the ``STAFF_TOKEN_HASHES`` setting
(``python -m scripts.create_staff_token <staff_id> --role admin``). Entries are
``staff_id:role:hash``; a Stage 10 entry without a role (``staff_id:hash``) is a
``support`` member, the role with fewer rights, so upgrading never grants anyone FAQ
management by accident.

Roles (``ROLE_PERMISSIONS``):

- ``admin``: FAQ management, ticket handling (with customer contact details), analytics.
- ``support``: ticket review and the workflow's status changes, with contact details
  (needed to reply). No FAQ changes, no analytics.

``authenticate`` compares the token against every configured hash in constant time, so
timing reveals neither whether a token is close nor which staff member it belongs to.
With no entries configured, nobody is staff and every staff operation is refused.
Conversation tokens (``cst_...``) never match: the format is checked first and they are
never in this store.

This is a **development and small-team prototype**, not a production identity system:
there is no password login, token expiry, rotation schedule, MFA or audit of sign-ins.
Revoke a token by removing its entry and restarting.
"""

import hashlib
import hmac
import re
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Self

from app.core.config import Settings

STAFF_TOKEN_PREFIX = "stf_"  # noqa: S105 - a public prefix, not a secret
STAFF_TOKEN_BYTES = 32
_TOKEN_PATTERN = re.compile(rf"^{STAFF_TOKEN_PREFIX}[A-Za-z0-9_-]{{43}}$")


class StaffRole(StrEnum):
    ADMIN = "admin"
    SUPPORT = "support"


class StaffPermission(StrEnum):
    MANAGE_TICKETS = "manage_tickets"  # list, read and change the status of tickets
    VIEW_CUSTOMER_CONTACTS = "view_customer_contacts"  # a ticket's contact details
    MANAGE_FAQS = "manage_faqs"  # list, create, edit, deactivate FAQs
    VIEW_ANALYTICS = "view_analytics"  # dashboard overview and analytics


ROLE_PERMISSIONS: dict[StaffRole, frozenset[StaffPermission]] = {
    StaffRole.ADMIN: frozenset(StaffPermission),
    StaffRole.SUPPORT: frozenset(
        {StaffPermission.MANAGE_TICKETS, StaffPermission.VIEW_CUSTOMER_CONTACTS}
    ),
}
# The role of a Stage 10 entry, which names none.
DEFAULT_ROLE = StaffRole.SUPPORT


class StaffAuthenticationError(Exception):
    """The token is missing, malformed or not a configured staff token."""

    def __init__(self) -> None:
        super().__init__("staff authentication failed")


class StaffPermissionError(Exception):
    """An authenticated staff member whose role does not allow the operation."""

    def __init__(self, permission: StaffPermission) -> None:
        super().__init__(f"staff role lacks permission {permission.value}")
        self.permission = permission


@dataclass(frozen=True)
class StaffMember:
    """An authenticated staff member. Only ``StaffAuthenticator`` creates these."""

    staff_id: str
    role: StaffRole = DEFAULT_ROLE

    @property
    def permissions(self) -> frozenset[StaffPermission]:
        return ROLE_PERMISSIONS[self.role]

    def can(self, permission: StaffPermission) -> bool:
        return permission in self.permissions

    def require(self, permission: StaffPermission) -> None:
        """Raises ``StaffPermissionError`` unless this member's role allows it."""
        if not self.can(permission):
            raise StaffPermissionError(permission)


def parse_staff_entry(entry: str) -> tuple[str, StaffRole, str]:
    """``staff_id:role:hash`` or ``staff_id:hash`` (a support member) as its parts.

    Raises:
        ValueError: not two or three parts, or an unknown role. The entry is not echoed.
    """
    parts = entry.split(":")
    if len(parts) == 2:
        return parts[0], DEFAULT_ROLE, parts[1]
    if len(parts) == 3:
        try:
            return parts[0], StaffRole(parts[1]), parts[2]
        except ValueError:
            raise ValueError("unknown staff role") from None
    raise ValueError("expected staff_id:role:hash")


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
            entries: ``staff_id:role:sha256hex`` or ``staff_id:sha256hex`` strings
                (``STAFF_TOKEN_HASHES``, validated by the settings).
        """
        self._hashes: dict[str, str] = {}
        self._roles: dict[str, StaffRole] = {}
        for entry in entries:
            staff_id, role, digest = parse_staff_entry(entry)
            self._hashes[staff_id] = digest
            self._roles[staff_id] = role

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
        return StaffMember(found, self._roles[found])
