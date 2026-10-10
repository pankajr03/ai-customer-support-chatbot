"""Validation and normalisation rules for the FAQ models."""

from typing import Any

import pytest
from pydantic import ValidationError

from app.models.faq import (
    FAQ,
    FAQ_EXAMPLE,
    KEYWORD_PATTERN,
    MAX_ANSWER_LENGTH,
    MAX_KEYWORD_LENGTH,
    MAX_KEYWORDS,
    MAX_QUESTION_LENGTH,
    FAQCategory,
    FAQCollection,
    FAQResponse,
)


def make_faq_data(**overrides: Any) -> dict[str, Any]:
    """A valid FAQ payload (the example from the spec) with optional overrides."""
    return {**FAQ_EXAMPLE, "keywords": list(FAQ_EXAMPLE["keywords"]), **overrides}


def error_fields(exc: pytest.ExceptionInfo[ValidationError]) -> set[str]:
    """Top-level field names that failed validation."""
    return {str(error["loc"][0]) for error in exc.value.errors() if error["loc"]}


# --- Valid data ---------------------------------------------------------------


def test_spec_example_is_valid() -> None:
    faq = FAQ.model_validate(make_faq_data())

    assert faq.id == "faq_001"
    assert faq.question == "How can I track my order?"
    assert faq.category is FAQCategory.ORDERS
    assert faq.keywords == ["track", "order", "shipping", "delivery"]
    assert faq.is_active is True


def test_is_active_defaults_to_true() -> None:
    data = make_faq_data()
    del data["is_active"]

    assert FAQ.model_validate(data).is_active is True


def test_json_round_trip() -> None:
    faq = FAQ.model_validate(make_faq_data())

    assert FAQ.model_validate_json(faq.model_dump_json()) == faq
    assert faq.model_dump(mode="json") == FAQ_EXAMPLE


def test_faq_is_immutable() -> None:
    faq = FAQ.model_validate(make_faq_data())

    with pytest.raises(ValidationError):
        faq.answer = "changed"  # type: ignore[misc]


# --- Required fields ----------------------------------------------------------


@pytest.mark.parametrize("field", ["id", "question", "answer", "category", "keywords"])
def test_required_fields(field: str) -> None:
    data = make_faq_data()
    del data[field]

    with pytest.raises(ValidationError) as exc:
        FAQ.model_validate(data)

    assert error_fields(exc) == {field}
    assert exc.value.errors()[0]["type"] == "missing"


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        FAQ.model_validate(make_faq_data(is_activ=False))

    assert error_fields(exc) == {"is_activ"}


# --- Questions and answers ----------------------------------------------------


@pytest.mark.parametrize("field", ["question", "answer"])
@pytest.mark.parametrize("value", ["", "   ", "\n\t  \r\n"])
def test_blank_question_or_answer_is_rejected(field: str, value: str) -> None:
    with pytest.raises(ValidationError, match="must not be empty or whitespace-only") as exc:
        FAQ.model_validate(make_faq_data(**{field: value}))

    assert error_fields(exc) == {field}


@pytest.mark.parametrize("field", ["question", "answer"])
def test_non_string_question_or_answer_is_rejected(field: str) -> None:
    with pytest.raises(ValidationError) as exc:
        FAQ.model_validate(make_faq_data(**{field: 123}))

    assert error_fields(exc) == {field}


def test_question_whitespace_is_normalised() -> None:
    faq = FAQ.model_validate(make_faq_data(question="  How can I\n   track my   order?  "))

    assert faq.question == "How can I track my order?"


def test_answer_formatting_is_preserved() -> None:
    answer = "Steps:\n1. Open My Orders\n2. Select your order\n\nNeed help? Contact Support."

    faq = FAQ.model_validate(make_faq_data(answer=f"  \n{answer}\n  "))

    assert faq.answer == answer


@pytest.mark.parametrize(
    ("field", "max_length"), [("question", MAX_QUESTION_LENGTH), ("answer", MAX_ANSWER_LENGTH)]
)
def test_question_and_answer_length_limits(field: str, max_length: int) -> None:
    FAQ.model_validate(make_faq_data(**{field: "x" * max_length}))

    with pytest.raises(ValidationError) as exc:
        FAQ.model_validate(make_faq_data(**{field: "x" * (max_length + 1)}))

    assert error_fields(exc) == {field}


# --- Identifiers --------------------------------------------------------------


@pytest.mark.parametrize("value", ["faq_001", "faq_123456", "  FAQ_042  "])
def test_valid_identifiers(value: str) -> None:
    faq = FAQ.model_validate(make_faq_data(id=value))

    assert faq.id == value.strip().lower()


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "001",
        "faq_",
        "faq_1",
        "faq_01",
        "faq_1234567",
        "faq-001",
        "faq_00a",
        "faq_ 001",
        1,
    ],
)
def test_invalid_identifiers_are_rejected(value: object) -> None:
    with pytest.raises(ValidationError) as exc:
        FAQ.model_validate(make_faq_data(id=value))

    assert error_fields(exc) == {"id"}


# --- Categories ---------------------------------------------------------------


@pytest.mark.parametrize("value", ["orders", "ORDERS", "  Orders "])
def test_category_is_normalised(value: str) -> None:
    assert FAQ.model_validate(make_faq_data(category=value)).category is FAQCategory.ORDERS


@pytest.mark.parametrize("value", ["", "order", "refunds", "misc", None, 1])
def test_invalid_categories_are_rejected(value: object) -> None:
    with pytest.raises(ValidationError) as exc:
        FAQ.model_validate(make_faq_data(category=value))

    assert error_fields(exc) == {"category"}


# --- Keywords -----------------------------------------------------------------


def test_keywords_are_normalised_and_deduplicated() -> None:
    faq = FAQ.model_validate(
        make_faq_data(keywords=["  Track ", "ORDER", "track", "Delivery   Time", "order"])
    )

    assert faq.keywords == ["track", "order", "delivery time"]


@pytest.mark.parametrize("value", ["e-mail", "don't", "lieferung", "café", "2fa", "a"])
def test_valid_keyword_values(value: str) -> None:
    assert FAQ.model_validate(make_faq_data(keywords=[value])).keywords == [value]


@pytest.mark.parametrize(
    "keywords",
    [
        [],
        [""],
        ["   "],
        ["track", ""],
        ["track!"],
        ["-track"],
        ["order#123"],
        ["x" * (MAX_KEYWORD_LENGTH + 1)],
        [f"kw{i}" for i in range(MAX_KEYWORDS + 1)],
        [123],
        [None],
        "track",
    ],
)
def test_invalid_keyword_values_are_rejected(keywords: object) -> None:
    with pytest.raises(ValidationError) as exc:
        FAQ.model_validate(make_faq_data(keywords=keywords))

    assert error_fields(exc) == {"keywords"}


# --- is_active ----------------------------------------------------------------


@pytest.mark.parametrize("value", ["yes", "false", 0, 1, None])
def test_is_active_requires_a_real_boolean(value: object) -> None:
    with pytest.raises(ValidationError) as exc:
        FAQ.model_validate(make_faq_data(is_active=value))

    assert error_fields(exc) == {"is_active"}


def test_is_active_false_from_json() -> None:
    faq = FAQ.model_validate_json(FAQ.model_validate(make_faq_data()).model_dump_json())
    inactive = faq.model_copy(update={"is_active": False})

    assert FAQ.model_validate_json(inactive.model_dump_json()).is_active is False


# --- Collections and duplicate identifiers ------------------------------------


def test_collection_accepts_unique_ids() -> None:
    collection = FAQCollection.model_validate(
        [make_faq_data(id="faq_001"), make_faq_data(id="faq_002", is_active=False)]
    )

    assert len(collection) == 2
    assert [faq.id for faq in collection.active()] == ["faq_001"]


def test_collection_rejects_duplicate_ids() -> None:
    with pytest.raises(ValidationError, match="Duplicate FAQ ids: faq_001, faq_002"):
        FAQCollection.model_validate(
            [
                make_faq_data(id="faq_001"),
                make_faq_data(id="faq_002"),
                make_faq_data(id="faq_003"),
                make_faq_data(id="faq_002"),
                make_faq_data(id="faq_001"),
            ]
        )


def test_duplicate_detection_is_case_insensitive() -> None:
    with pytest.raises(ValidationError, match="Duplicate FAQ ids: faq_001"):
        FAQCollection.model_validate([make_faq_data(id="faq_001"), make_faq_data(id="FAQ_001")])


def test_collection_reports_which_item_is_invalid() -> None:
    with pytest.raises(ValidationError) as exc:
        FAQCollection.model_validate([make_faq_data(), make_faq_data(id="faq_002", answer=" ")])

    assert exc.value.errors()[0]["loc"] == (1, "answer")


def test_collection_loads_from_json() -> None:
    faq_json = FAQ.model_validate(make_faq_data()).model_dump_json()

    collection = FAQCollection.model_validate_json(f"[{faq_json}]")

    assert collection.get("FAQ_001") == collection.root[0]
    assert collection.get("faq_999") is None


def test_empty_collection_is_valid() -> None:
    assert len(FAQCollection.model_validate([])) == 0


# --- Public response model ----------------------------------------------------


def test_response_model_hides_internal_fields() -> None:
    faq = FAQ.model_validate(make_faq_data())

    response = FAQResponse.model_validate(faq)

    assert response.model_dump(mode="json") == {
        "id": "faq_001",
        "question": "How can I track my order?",
        "answer": "You can track your order from the My Orders section of your account.",
        "category": "orders",
    }


def test_json_schema_includes_example() -> None:
    schema = FAQ.model_json_schema()

    assert schema["examples"] == [FAQ_EXAMPLE]
    assert schema["properties"]["id"]["pattern"] == "^faq_[0-9]{3,6}$"
    assert schema["properties"]["keywords"]["items"]["pattern"] == KEYWORD_PATTERN
    assert set(schema["required"]) == {"id", "question", "answer", "category", "keywords"}
