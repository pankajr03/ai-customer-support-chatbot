"""Staff roles (Stage 11, Task 3): entries, permissions and configuration."""

from collections.abc import Callable

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.staff_auth import (
    ROLE_PERMISSIONS,
    StaffAuthenticationError,
    StaffAuthenticator,
    StaffMember,
    StaffPermission,
    StaffPermissionError,
    StaffRole,
    generate_staff_token,
    hash_staff_token,
    parse_staff_entry,
)
from scripts import create_staff_token

DIGEST = "a" * 64


def test_entries_with_and_without_a_role() -> None:
    assert parse_staff_entry(f"alex:admin:{DIGEST}") == ("alex", StaffRole.ADMIN, DIGEST)
    assert parse_staff_entry(f"maria:support:{DIGEST}") == ("maria", StaffRole.SUPPORT, DIGEST)
    # Stage 10 entries keep working, with the role that grants less.
    assert parse_staff_entry(f"sam:{DIGEST}") == ("sam", StaffRole.SUPPORT, DIGEST)
    with pytest.raises(ValueError, match="unknown staff role"):
        parse_staff_entry(f"eve:root:{DIGEST}")


@pytest.mark.parametrize(
    "entry",
    [f"eve:root:{DIGEST}", f"eve:ADMIN:{DIGEST}", f"eve:admin:support:{DIGEST}", "eve:admin:"],
)
def test_settings_refuse_unknown_roles_without_echoing_the_entry(
    make_settings: Callable[..., Settings], entry: str
) -> None:
    with pytest.raises(ValidationError) as caught:
        make_settings(staff_token_hashes=[entry])
    assert entry not in str(caught.value)


def test_settings_refuse_one_staff_id_with_two_roles(
    make_settings: Callable[..., Settings],
) -> None:
    with pytest.raises(ValidationError, match="repeats a staff ID"):
        make_settings(staff_token_hashes=[f"alex:admin:{DIGEST}", f"alex:support:{'b' * 64}"])


def test_the_authenticator_returns_the_configured_role() -> None:
    admin, support, legacy = (generate_staff_token() for _ in range(3))
    staff = StaffAuthenticator(
        [
            f"alex:admin:{hash_staff_token(admin)}",
            f"maria:support:{hash_staff_token(support)}",
            f"sam:{hash_staff_token(legacy)}",
        ]
    )
    assert staff.authenticate(admin) == StaffMember("alex", StaffRole.ADMIN)
    assert staff.authenticate(support) == StaffMember("maria", StaffRole.SUPPORT)
    assert staff.authenticate(legacy) == StaffMember("sam", StaffRole.SUPPORT)
    assert staff.staff_ids == {"alex", "maria", "sam"}


def test_conversation_tokens_never_authenticate_as_staff() -> None:
    customer = "cst_" + "x" * 43
    # Even if someone configured the hash of a conversation token by mistake.
    staff = StaffAuthenticator([f"alex:admin:{hash_staff_token(customer)}"])
    with pytest.raises(StaffAuthenticationError):
        staff.authenticate(customer)


def test_role_permissions() -> None:
    assert ROLE_PERMISSIONS[StaffRole.ADMIN] == frozenset(StaffPermission)
    support = StaffMember("maria", StaffRole.SUPPORT)
    assert support.can(StaffPermission.MANAGE_TICKETS)
    assert support.can(StaffPermission.VIEW_CUSTOMER_CONTACTS)
    for permission in (StaffPermission.MANAGE_FAQS, StaffPermission.VIEW_ANALYTICS):
        assert not support.can(permission)
        with pytest.raises(StaffPermissionError):
            support.require(permission)
    StaffMember("alex", StaffRole.ADMIN).require(StaffPermission.MANAGE_FAQS)


def test_the_token_script_prints_an_entry_with_the_role(capsys: pytest.CaptureFixture[str]) -> None:
    assert create_staff_token.main(["alex", "--role", "admin"]) == 0

    lines = [line.strip() for line in capsys.readouterr().out.splitlines()]
    token = next(line for line in lines if line.startswith("stf_"))
    entry = next(line for line in lines if line.startswith('"alex:')).strip('"')
    assert entry.startswith("alex:admin:")
    Settings(_env_file=None, staff_token_hashes=[entry])  # a valid setting
    assert StaffAuthenticator([entry]).authenticate(token).role is StaffRole.ADMIN


def test_the_token_script_defaults_to_support(capsys: pytest.CaptureFixture[str]) -> None:
    create_staff_token.main(["maria"])
    assert '"maria:support:' in capsys.readouterr().out
    with pytest.raises(SystemExit):
        create_staff_token.main(["maria", "--role", "owner"])


def test_the_settings_summary_counts_admins_without_revealing_entries(
    make_settings: Callable[..., Settings],
) -> None:
    settings = make_settings(
        staff_token_hashes=[f"alex:admin:{DIGEST}", f"maria:support:{'b' * 64}", f"sam:{'c' * 64}"]
    )
    summary = settings.safe_summary()
    assert summary["staff_accounts"] == 3 and summary["staff_admin_accounts"] == 1
    assert DIGEST not in str(summary)
