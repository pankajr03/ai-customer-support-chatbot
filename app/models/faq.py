"""FAQ knowledge-base models.

``FAQ`` is the internal record used for retrieval. ``FAQCollection`` validates a
whole knowledge base (e.g. loaded from a JSON file) and rejects duplicate IDs.
``FAQResponse`` is the public shape returned to API clients.

Normalisation policy:
- Identifiers, categories and keywords are machine-facing: trimmed and lower-cased.
- Questions are trimmed and repeated whitespace collapsed (useful for matching).
- Answers are customer-facing: only leading/trailing whitespace is removed, so
  line breaks, lists and capitalisation are preserved exactly as written.
"""

from collections import Counter
from collections.abc import Iterable
from enum import StrEnum
from typing import Annotated, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    RootModel,
    StrictBool,
    StringConstraints,
    model_validator,
)

FAQ_ID_PATTERN = r"^faq_[0-9]{3,6}$"
MAX_QUESTION_LENGTH = 500
MAX_ANSWER_LENGTH = 5000
MAX_KEYWORDS = 20
MAX_KEYWORD_LENGTH = 50
# Letters or digits in any language, optionally joined by spaces, hyphens or apostrophes.
KEYWORD_PATTERN = r"^\w[\w '-]*$"


class FAQCategory(StrEnum):
    """Allowed FAQ categories. Add a member here to introduce a new category."""

    GENERAL = "general"
    ORDERS = "orders"
    SHIPPING = "shipping"
    RETURNS = "returns"
    REFUNDS = "refunds"
    PAYMENTS = "payments"
    ACCOUNT = "account"
    PRODUCTS = "products"
    TECHNICAL = "technical"


# --- Normalisers (run before type validation; non-strings pass through and are rejected) ---


def _strip(value: object) -> object:
    return value.strip() if isinstance(value, str) else value


def _strip_lower(value: object) -> object:
    return value.strip().lower() if isinstance(value, str) else value


def _collapse_whitespace(value: object) -> object:
    return " ".join(value.split()) if isinstance(value, str) else value


def _collapse_lower(value: object) -> object:
    return " ".join(value.split()).lower() if isinstance(value, str) else value


# --- Validators ---


def _require_text(value: str) -> str:
    if not value:
        raise ValueError("must not be empty or whitespace-only")
    return value


def _dedupe(values: list[str]) -> list[str]:
    """Remove repeated keywords while keeping their original order."""
    return list(dict.fromkeys(values))


# --- Reusable field types ---
# BeforeValidators always run before the constraints regardless of position, but
# constraints must be listed first or Pydantic omits them from the JSON schema.

FAQId = Annotated[
    str,
    StringConstraints(pattern=FAQ_ID_PATTERN),
    BeforeValidator(_strip_lower),
]
Question = Annotated[
    str,
    StringConstraints(max_length=MAX_QUESTION_LENGTH),
    BeforeValidator(_collapse_whitespace),
    AfterValidator(_require_text),
]
Answer = Annotated[
    str,
    StringConstraints(max_length=MAX_ANSWER_LENGTH),
    BeforeValidator(_strip),
    AfterValidator(_require_text),
]
Category = Annotated[FAQCategory, BeforeValidator(_strip_lower)]
Keyword = Annotated[
    str,
    StringConstraints(min_length=1, max_length=MAX_KEYWORD_LENGTH, pattern=KEYWORD_PATTERN),
    BeforeValidator(_collapse_lower),
]
Keywords = Annotated[
    list[Keyword],
    Field(min_length=1, max_length=MAX_KEYWORDS),
    AfterValidator(_dedupe),
]

FAQ_EXAMPLE = {
    "id": "faq_001",
    "question": "How can I track my order?",
    "answer": "You can track your order from the My Orders section of your account.",
    "category": "orders",
    "keywords": ["track", "order", "shipping", "delivery"],
    "is_active": True,
}


class FAQ(BaseModel):
    """A single question/answer pair in the support knowledge base."""

    # extra="forbid" catches typos such as "is_activ" instead of silently ignoring them.
    # frozen=True prevents accidental edits to the knowledge base at runtime.
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={"examples": [FAQ_EXAMPLE]},
    )

    id: FAQId = Field(description="Unique identifier in the form faq_NNN (3-6 digits).")
    question: Question = Field(description="The customer question.")
    answer: Answer = Field(description="The support response shown to the customer.")
    category: Category = Field(description="FAQ category.")
    keywords: Keywords = Field(description="Search keywords (lower-cased, de-duplicated).")
    is_active: StrictBool = Field(
        default=True, description="Whether the FAQ is available for retrieval."
    )


def find_duplicate_ids(faqs: Iterable[FAQ]) -> list[str]:
    """Return the sorted IDs that appear more than once (empty if all are unique)."""
    counts = Counter(faq.id for faq in faqs)
    return sorted(faq_id for faq_id, count in counts.items() if count > 1)


class FAQCollection(RootModel[list[FAQ]]):
    """A validated knowledge base: a list of FAQs with unique identifiers."""

    @model_validator(mode="after")
    def _ids_must_be_unique(self) -> Self:
        if duplicates := find_duplicate_ids(self.root):
            raise ValueError(f"Duplicate FAQ ids: {', '.join(duplicates)}")
        return self

    def __len__(self) -> int:
        return len(self.root)

    def active(self) -> list[FAQ]:
        """FAQs that are available for retrieval."""
        return [faq for faq in self.root if faq.is_active]

    def get(self, faq_id: str) -> FAQ | None:
        """Look up an FAQ by id (case-insensitive), or return None."""
        wanted = faq_id.strip().lower()
        return next((faq for faq in self.root if faq.id == wanted), None)


class FAQResponse(BaseModel):
    """Public view of an FAQ.

    Omits ``keywords`` and ``is_active``, which are internal retrieval details.
    Build it from an ``FAQ`` with ``FAQResponse.model_validate(faq)``.
    """

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "examples": [{k: FAQ_EXAMPLE[k] for k in ("id", "question", "answer", "category")}]
        },
    )

    id: str
    question: str
    answer: str
    category: FAQCategory
