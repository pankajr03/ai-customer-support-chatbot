"""HTTP models of the staff admin API (Stage 11): ``/api/v1/admin/...``.

FAQ records are returned as ``ManagedFAQ`` (``app.models.faq_admin``) and accepted as
``FAQDraft`` / ``FAQChanges`` / ``FAQActivation``. This module adds the list envelope,
the signed-in staff member's identity and the dashboard overview.

Nothing here contains customer contact details or ticket text: the overview is counts
only. Contact details stay on ``GET /support/tickets/{id}`` for staff whose role allows
them.
"""

from datetime import date
from typing import Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.core.staff_auth import StaffMember, StaffPermission, StaffRole
from app.models.escalation import TicketType
from app.models.faq_admin import FAQCatalogSummary, FAQRecordPage, ManagedFAQ
from app.models.support import LeadCategory, TicketStatus

PROTOTYPE_AUTH_NOTICE = (
    "Staff access uses personal tokens configured as hashes on the server. It is a "
    "development and small-team prototype, not a production identity system: there is "
    "no password login, token expiry or multi-factor authentication."
)


class StaffIdentity(BaseModel):
    """Who the presented staff token belongs to and what their role allows."""

    model_config = ConfigDict(frozen=True)

    staff_id: str
    role: StaffRole
    permissions: list[StaffPermission] = Field(
        description="What this role may do; the server enforces it on every request."
    )
    auth_notice: str = Field(description="What kind of authentication this is.")

    @classmethod
    def of(cls, member: StaffMember) -> Self:
        return cls(
            staff_id=member.staff_id,
            role=member.role,
            permissions=sorted(member.permissions),
            auth_notice=PROTOTYPE_AUTH_NOTICE,
        )


class StaffDirectoryEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    staff_id: str
    role: StaffRole


class StaffDirectory(BaseModel):
    """Configured staff who can handle (and be assigned) tickets."""

    model_config = ConfigDict(frozen=True)

    staff: list[StaffDirectoryEntry]


class FAQAdminList(BaseModel):
    """One page of FAQs, active and inactive, in ID order."""

    model_config = ConfigDict(frozen=True)

    faqs: list[ManagedFAQ]
    total: int
    offset: int
    limit: int
    has_more: bool

    @classmethod
    def of(cls, page: FAQRecordPage) -> Self:
        return cls(
            faqs=list(page.items),
            total=page.total,
            offset=page.offset,
            limit=page.limit,
            has_more=page.has_more,
        )


class ConversationCounts(BaseModel):
    model_config = ConfigDict(frozen=True)

    stored: int = Field(
        description="Conversations currently stored. Expired ones are deleted by retention "
        "cleanup, so this is not a lifetime total."
    )
    started_last_7_days: int = Field(description="Of those, started in the last 7 days.")


class TicketCountsView(BaseModel):
    model_config = ConfigDict(frozen=True)

    total: int
    open: int = Field(
        default=0, description="Not resolved or closed: open, assigned, in progress, waiting."
    )
    resolved: int = Field(default=0, description="Status resolved (may still be reopened).")
    closed: int = Field(default=0, description="Status closed (final).")
    leads: int = Field(default=0, description="Sales inquiries (all statuses).")
    by_status: dict[TicketStatus, int]
    by_type: dict[TicketType, int]
    by_lead_category: dict[LeadCategory, int]
    created_last_7_days: int
    open_queue: int = Field(description="Tickets waiting on staff: open, assigned or in progress.")


class AdminOverview(BaseModel):
    """Counts for the dashboard overview and analytics pages."""

    model_config = ConfigDict(frozen=True)

    generated_at: AwareDatetime
    faqs: FAQCatalogSummary | None = Field(
        description="Null when this server's FAQs are not stored in the database."
    )
    tickets: TicketCountsView
    conversations: ConversationCounts | None = None


# --- Chat analytics (Stage 11, Task 15) --------------------------------------------------


class RateView(BaseModel):
    """A rate with its formula and inputs; ``percent`` is null (with ``note``) when the
    denominator is zero or too small to be meaningful."""

    model_config = ConfigDict(frozen=True)

    name: str
    numerator: int
    denominator: int
    percent: float | None
    formula: str
    note: str | None


class LatencyView(BaseModel):
    model_config = ConfigDict(frozen=True)

    samples: int
    average_ms: float | None
    median_ms: float | None
    p90_ms: float | None
    max_ms: float | None
    definition: str


class TicketRequestCounts(BaseModel):
    model_config = ConfigDict(frozen=True)

    support: int
    sales: int
    after_chat_offer: int = Field(description="Of those, sent after the chat offered it.")


class DailyPoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    day: date
    conversations_started: int
    chatbot_responses: int
    answered: int
    fallbacks: int
    tickets_created: int


class ChatAnalytics(BaseModel):
    """Chatbot decisions and requests over a window of whole UTC days. Counts only: no
    message text, ticket text or contact details."""

    model_config = ConfigDict(frozen=True)

    generated_at: AwareDatetime
    days: int
    since: AwareDatetime
    stored_conversations: int
    conversations_started: int
    customer_messages: int
    chatbot_responses: int
    decisions: dict[str, int]
    escalation_offers: int
    escalation_offers_by_reason: dict[str, int]
    tickets: TicketRequestCounts
    rates: list[RateView]
    latency: LatencyView
    daily: list[DailyPoint]
    min_sample: int
    limitations: list[str]
