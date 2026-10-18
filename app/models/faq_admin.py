"""FAQ management models (Stage 11): FAQs as staff edit them.

``FAQ`` (``app.models.faq``) stays the record retrieval and customers use. Staff work
with ``ManagedFAQ``: the same content plus what management needs, a ``version`` for
optimistic concurrency, timestamps (aware UTC) and who created and last changed it.

Inputs reuse the knowledge base's field types, so an FAQ staff write is validated and
normalised exactly like one loaded from ``data/faqs.json``: questions trimmed with
repeated spaces collapsed, answers trimmed only (line breaks kept), categories from
``FAQCategory``, keywords (the FAQ's tags) trimmed, lower-cased, de-duplicated, 1-20 of
them. Unknown fields are rejected, so a typo never silently does nothing.

Staff-written questions and answers must not address the assistant (Stage 12): approved
FAQs are trusted evidence for the model, so text such as "ignore previous instructions",
a line starting "SYSTEM:" or prompt markup ("<faq ...>") is refused with a 422
``prompt_control`` error. This lint only guards the staff API; the prompt still treats
FAQ text as data, and the grounding checks still run on every generated answer.

Staff never choose an FAQ's ID: the server allocates the next ``faq_NNN``. IDs are never
reused, because FAQs are deactivated rather than deleted (see
``app.services.faq_management_service``).
"""

import re
import uuid
from enum import StrEnum
from typing import Annotated, Any, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
    model_validator,
)
from pydantic_core import PydanticCustomError

from app.models.faq import (
    FAQ,
    Answer,
    Category,
    FAQCategory,
    FAQId,
    Keywords,
    Question,
)

MAX_SEARCH_LENGTH = 200
MAX_ACTOR_LENGTH = 64

SearchText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_SEARCH_LENGTH)
]
# Text written to steer a language model rather than inform a customer (Stage 12).
_PROMPT_CONTROL = re.compile(
    r"(?:ignore|disregard|forget|override)\s+(?:all\s+|any\s+|the\s+|your\s+|of\s+)*"
    r"(?:previous|prior|above|earlier|system|existing)?\s*(?:instructions|rules|prompts?|guidelines)"
    r"|system\s+prompt|developer\s+mode|jailbreak"
    # A role label ("SYSTEM:") starting a line or a sentence.
    r"|(?:^|[.!?]\s+)\s*(?:system|assistant|developer)\s*:"
    r"|</?\s*(?:faq|faq_reference_data|customer_question|question|answer|instructions|system)\b"
    r"|\byou\s+are\s+(?:now\s+)?(?:an?\s+|the\s+)?(?:ai|assistant|chatbot|language\s+model)\b"
    r"|\b(?:tell|inform|promise)\s+(?:the\s+|all\s+)?(?:customers?|users?)\s+that\b",
    re.IGNORECASE | re.MULTILINE,
)


def contains_prompt_control(text: str) -> bool:
    """Whether FAQ text addresses the assistant (see ``_PROMPT_CONTROL``). Used for staff
    writes, imports and a warning when the stored catalogue is loaded."""
    return _PROMPT_CONTROL.search(text) is not None


def _no_prompt_control(value: str) -> str:
    if contains_prompt_control(value):
        raise PydanticCustomError(
            "prompt_control",
            "Must not contain instructions addressed to the assistant, such as 'ignore "
            "previous instructions', a 'SYSTEM:' line or prompt tags",
        )
    return value


StaffQuestion = Annotated[Question, AfterValidator(_no_prompt_control)]
StaffAnswer = Annotated[Answer, AfterValidator(_no_prompt_control)]

# Who changed an FAQ: a staff ID, or None for records imported from the JSON file.
ActorId = Annotated[str, StringConstraints(min_length=1, max_length=MAX_ACTOR_LENGTH)]


class ManagedFAQ(BaseModel):
    """An FAQ with its management metadata, as stored."""

    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)

    id: FAQId
    question: Question
    answer: Answer
    category: Category
    keywords: Keywords
    is_active: StrictBool
    version: int = Field(ge=1, description="Increases by one with every change.")
    created_at: AwareDatetime
    updated_at: AwareDatetime
    created_by: ActorId | None = Field(
        description="Staff ID of the author; null for FAQs imported from the JSON file."
    )
    updated_by: ActorId | None = Field(
        description="Staff ID of the last change; null if never changed by staff."
    )

    @model_validator(mode="after")
    def _updated_after_created(self) -> Self:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not be before created_at")
        return self

    def to_faq(self) -> FAQ:
        """The retrieval record (what customers and search see)."""
        return FAQ(
            id=self.id,
            question=self.question,
            answer=self.answer,
            category=self.category,
            keywords=self.keywords,
            is_active=self.is_active,
        )

    def content_equals(self, faq: FAQ) -> bool:
        """Whether this record holds exactly ``faq``'s content (ignoring metadata)."""
        return self.to_faq() == faq


class FAQDraft(BaseModel):
    """A new FAQ written by staff."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "examples": [
                {
                    "question": "Do you ship to Norway?",
                    "answer": "Yes. Orders to Norway usually arrive within 5-7 working days.",
                    "category": "shipping",
                    "keywords": ["norway", "international shipping"],
                    "is_active": True,
                }
            ]
        },
    )

    question: StaffQuestion
    answer: StaffAnswer
    category: Category
    keywords: Keywords = Field(description="Tags used by keyword search (1-20).")
    is_active: StrictBool = Field(
        default=True, description="Whether customers can be answered with it right away."
    )


class FAQChanges(BaseModel):
    """An edit of an existing FAQ: only the fields to change, and the version the edit
    is based on. A stale version is refused (409), never silently overwritten."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "examples": [{"expected_version": 3, "answer": "The updated answer text."}]
        },
    )

    expected_version: int = Field(
        ge=1, description="The `version` of the FAQ as read before editing it."
    )
    question: StaffQuestion | None = None
    answer: StaffAnswer | None = None
    category: Category | None = None
    keywords: Keywords | None = None
    is_active: StrictBool | None = None

    @model_validator(mode="after")
    def _something_to_change(self) -> Self:
        if not self.changed_fields():
            raise ValueError(
                "at least one of question, answer, category, keywords or is_active must be given"
            )
        return self

    def changed_fields(self) -> dict[str, object]:
        """The fields this edit sets (``None`` means "leave as it is")."""
        return {
            name: value
            for name in ("question", "answer", "category", "keywords", "is_active")
            if (value := getattr(self, name)) is not None
        }


class FAQActivation(BaseModel):
    """Deactivate or reactivate an FAQ, based on the version that was read."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    expected_version: int = Field(ge=1)


class FAQFilter(BaseModel):
    """What to list: all filters optional, combined with AND."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    search: SearchText | None = None
    category: FAQCategory | None = None
    is_active: bool | None = None


class FAQRecordPage(BaseModel):
    """One page of managed FAQs, in ID order (``faq_001``, ``faq_002``, ...)."""

    model_config = ConfigDict(frozen=True)

    items: tuple[ManagedFAQ, ...]
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1)

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total


class FAQRevisionAction(StrEnum):
    CREATED = "created"  # by staff, or by the JSON import (changed_by null)
    UPDATED = "updated"  # question, answer, category and/or tags (maybe also is_active)
    ACTIVATED = "activated"  # only is_active, to true
    DEACTIVATED = "deactivated"  # only is_active, to false


# The content fields a revision can record, in a stable order.
FAQ_CONTENT_FIELDS = ("question", "answer", "category", "keywords", "is_active")


class FAQRevision(BaseModel):
    """One change in an FAQ's history: who, when, and only the fields that changed.

    For staff only (``GET /admin/faqs/{id}/revisions``); never part of customer
    responses. Holds FAQ content and a staff ID, never credentials.
    """

    model_config = ConfigDict(frozen=True)

    revision_id: uuid.UUID
    faq_id: str
    version: int = Field(ge=1, description="The FAQ's version after this change.")
    action: FAQRevisionAction
    changed_fields: list[str]
    previous_values: dict[str, Any] | None = Field(
        description="The changed fields before the change; null for a creation."
    )
    new_values: dict[str, Any] = Field(description="The changed fields after the change.")
    changed_by: str | None = Field(description="Staff ID; null for the JSON import.")
    changed_at: AwareDatetime


class FAQRevisionPage(BaseModel):
    """One page of an FAQ's revisions, newest first."""

    model_config = ConfigDict(frozen=True)

    faq_id: str
    revisions: list[FAQRevision]
    total: int
    offset: int
    limit: int
    has_more: bool


class FAQImportReport(BaseModel):
    """What an import did (``scripts/import_faqs.py``, or seeding an empty table)."""

    model_config = ConfigDict(frozen=True)

    created: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    # Already in the database with different content (for example edited by staff):
    # the database version was kept, the file's was not applied.
    kept_database_version: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        return len(self.created) + len(self.unchanged) + len(self.kept_database_version)


class CategoryCount(BaseModel):
    model_config = ConfigDict(frozen=True)

    category: FAQCategory
    active: int = Field(ge=0)
    inactive: int = Field(ge=0)


class FAQCatalogSummary(BaseModel):
    """Counts of the FAQ catalogue, for the dashboard."""

    model_config = ConfigDict(frozen=True)

    total: int = Field(ge=0)
    active: int = Field(ge=0)
    inactive: int = Field(ge=0)
    by_category: tuple[CategoryCount, ...]
    last_updated_at: AwareDatetime | None
