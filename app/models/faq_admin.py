"""FAQ management models (Stage 11): FAQs as staff edit them.

``FAQ`` (``app.models.faq``) stays the record retrieval and customers use. Staff work
with ``ManagedFAQ``: the same content plus what management needs, a ``version`` for
optimistic concurrency, timestamps (aware UTC) and who created and last changed it.

Inputs reuse the knowledge base's field types, so an FAQ staff write is validated and
normalised exactly like one loaded from ``data/faqs.json``: questions trimmed with
repeated spaces collapsed, answers trimmed only (line breaks kept), categories from
``FAQCategory``, keywords (the FAQ's tags) trimmed, lower-cased, de-duplicated, 1-20 of
them. Unknown fields are rejected, so a typo never silently does nothing.

Staff never choose an FAQ's ID: the server allocates the next ``faq_NNN``. IDs are never
reused, because FAQs are deactivated rather than deleted (see
``app.services.faq_management_service``).
"""

from typing import Annotated, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
    model_validator,
)

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

    question: Question
    answer: Answer
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
    question: Question | None = None
    answer: Answer | None = None
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
