"""Lead capture request validation (Stage 10, Task 4)."""

import json
import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from app.models.support import (
    MAX_DESCRIPTION_LENGTH,
    MAX_NAME_LENGTH,
    MAX_SUBJECT_LENGTH,
    ContactPreference,
    SupportRequestCreate,
    SupportRequestCreated,
    TicketType,
    request_fingerprint,
)
from tests.ticket_test_data import form_body


def request(**changes: Any) -> dict[str, Any]:
    return form_body(**changes)


def error_type(body: dict[str, Any]) -> str:
    with pytest.raises(ValidationError) as caught:
        SupportRequestCreate.model_validate_json(_json(body))
    return str(caught.value.errors()[0]["type"])


def _json(body: dict[str, Any]) -> str:
    return json.dumps(body)


def valid(**changes: Any) -> SupportRequestCreate:
    return SupportRequestCreate.model_validate_json(_json(request(**changes)))


# --- Accepted ---------------------------------------------------------------------------


def test_a_minimal_request_needs_no_phone() -> None:
    parsed = valid()

    assert parsed.phone is None
    assert parsed.contact_preference is ContactPreference.EMAIL
    assert parsed.ticket_type is TicketType.SUPPORT


def test_surrounding_whitespace_is_trimmed_and_inner_text_kept() -> None:
    parsed = valid(
        name="  Zoë  O'Brien​ ",
        email="  alex@example.com ",
        subject="\tDamaged lid  ",
        message="  First line.\n\nSecond line, with  two spaces.  ",
    )

    assert parsed.name == "Zoë  O'Brien"
    assert parsed.email == "alex@example.com"
    assert parsed.subject == "Damaged lid"
    assert parsed.message == "First line.\n\nSecond line, with  two spaces."


def test_email_domains_are_normalised() -> None:
    assert valid(email="Alex.Rivera@Example.COM").email == "Alex.Rivera@example.com"


@pytest.mark.parametrize(
    "phone",
    [
        "+44 20 7946 0958",
        "+1 (555) 010-9999",
        "020 7946 0958",
        "+91 98765 43210",
        "+86 138 0013 8000",
        "+49 (0)30 123456",
        "0612345678",
        "+81-3-1234-5678",
        "555.010.9999",
        "+44 20 7946 0958 ext. 12",
        "+1 555 010 9999 x123",
    ],
)
def test_international_phone_numbers_are_accepted(phone: str) -> None:
    assert valid(phone=phone, contact_preference="phone").phone == phone


@pytest.mark.parametrize("phone", ["", "   ", None])
def test_a_blank_phone_counts_as_none(phone: str | None) -> None:
    assert valid(phone=phone).phone is None


def test_names_in_any_script_are_accepted() -> None:
    for name in ["李小龍", "Ελένη", "محمد", "Anne-Marie d'Arc", "X Æ A-12"]:
        assert valid(name=name).name == name


def test_sales_inquiries_are_a_separate_type() -> None:
    assert valid(ticket_type="sales").ticket_type is TicketType.SALES


# --- Refused ----------------------------------------------------------------------------


@pytest.mark.parametrize("consent", [False, "true", "yes", "on", 1, None])
def test_consent_must_be_exactly_true(consent: Any) -> None:
    assert error_type(request(consent_given=consent)) in ("consent_required", "bool_type")


def test_consent_has_no_default() -> None:
    assert error_type(request(consent_given=...)) == "missing"
    assert SupportRequestCreate.model_fields["consent_given"].is_required()
    schema = SupportRequestCreate.model_json_schema()
    assert "default" not in schema["properties"]["consent_given"]
    assert "consent_given" in schema["required"]


@pytest.mark.parametrize(
    "email", ["", "alex", "alex@", "@example.com", "alex@example", "a b@x.com"]
)
def test_invalid_email_addresses_are_refused(email: str) -> None:
    assert error_type(request(email=email)) == "value_error"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", ""),
        ("name", "   "),
        ("name", "​"),
        ("subject", " "),
        ("message", ""),
        ("message", " \n\t "),
    ],
)
def test_empty_names_subjects_and_descriptions_are_refused(field: str, value: str) -> None:
    assert error_type(request(**{field: value})) == "blank_text"


@pytest.mark.parametrize(
    ("field", "limit"),
    [
        ("name", MAX_NAME_LENGTH),
        ("subject", MAX_SUBJECT_LENGTH),
        ("message", MAX_DESCRIPTION_LENGTH),
    ],
)
def test_length_limits_apply_after_trimming(field: str, limit: int) -> None:
    assert len(getattr(valid(**{field: "a" * limit + "   "}), field)) == limit
    assert error_type(request(**{field: "a" * (limit + 1)})) == "string_too_long"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "Alex\nRivera"),
        ("subject", "Help\rme"),
        ("name", "Alex\x00"),
        ("message", "Hello\x1b[31m"),
    ],
)
def test_control_characters_and_line_breaks_in_single_line_fields_are_refused(
    field: str, value: str
) -> None:
    assert error_type(request(**{field: value})) == "invalid_characters"


@pytest.mark.parametrize("phone", ["call me", "12", "1" * 16, "+44 20 7946 0958; DROP", "+" * 5])
def test_implausible_phone_numbers_are_refused(phone: str) -> None:
    assert error_type(request(phone=phone)) in ("phone_format", "phone_digits", "phone_length")


def test_phone_contact_needs_a_phone_number() -> None:
    assert error_type(request(contact_preference="phone")) == "phone_required"


@pytest.mark.parametrize("preference", ["fax", "sms", "EMAIL", ""])
def test_unsupported_contact_preferences_are_refused(preference: str) -> None:
    assert error_type(request(contact_preference=preference)) == "enum"


@pytest.mark.parametrize("ticket_type", ["complaint", "", None])
def test_unknown_ticket_types_are_refused(ticket_type: Any) -> None:
    assert error_type(request(ticket_type=ticket_type)) == "enum"


@pytest.mark.parametrize("field", ["session_id", "access_token", "status", "priority", "role"])
def test_unknown_fields_are_refused(field: str) -> None:
    assert error_type(request(**{field: "x"})) == "extra_forbidden"


@pytest.mark.parametrize("confirmed", [False, "true", 1, None])
def test_the_request_must_be_explicitly_confirmed(confirmed: Any) -> None:
    assert error_type(request(confirmed=confirmed)) in ("confirmation_required", "bool_type")
    assert error_type(request(confirmed=...)) == "missing"
    assert "default" not in SupportRequestCreate.model_json_schema()["properties"]["confirmed"]


@pytest.mark.parametrize(
    ("value", "error"),
    [
        (..., "missing"),
        ("not-a-uuid", "uuid_format"),
        ("{4c981eb1-7847-41c0-bf6b-0a0e1b9d6d91}", "uuid_format"),
        ("4c981eb1784741c0bf6b0a0e1b9d6d91", "uuid_format"),
        ("00000000-0000-0000-0000-000000000000", "uuid_reserved"),
    ],
)
def test_client_request_id_must_be_a_canonical_uuid(value: Any, error: str) -> None:
    assert error_type(request(client_request_id=value)) == error


def test_the_fingerprint_ignores_the_id_spacing_and_case_only() -> None:
    base = valid()
    same = valid(
        client_request_id=str(uuid.uuid4()),
        name="ALEX   rivera",
        email="ALEX@example.com",
        message="The lid  was cracked.\nCan you replace it?",
    )

    assert request_fingerprint(base) == request_fingerprint(same)
    for changes in (
        {"subject": "Other"},
        {"message": "Other"},
        {"ticket_type": "sales"},
        {"phone": "+44 20 7946 0958"},
        {"email": "other@example.com"},
    ):
        assert request_fingerprint(valid(**changes)) != request_fingerprint(base), changes


# --- What the customer gets back --------------------------------------------------------


def test_the_confirmation_contains_no_contact_details() -> None:
    fields = set(SupportRequestCreated.model_fields)

    assert fields == {
        "reference_number",
        "ticket_type",
        "status",
        "created_at",
        "outcome",
        "message",
        "expected_response",
    }
    assert not fields & {"name", "email", "phone", "contact_id", "contact_preference"}
