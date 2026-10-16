"""Escalation offers on chat replies (Stage 10).

A chat reply may carry an ``EscalationOffer``: an invitation to send a support request
(``ticket_type`` support) or an optional sales contact form (``ticket_type`` sales). It
is only an offer. Nothing is created and no contact details are asked for until the
customer opens the form, fills it in and gives consent; ignoring the offer declines it.

The rules that decide when an offer is made are in ``app.services.escalation_policy``.
This module has no project imports, so both the conversation models and the support
models can use it.
"""

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TicketType(StrEnum):
    """Support requests and sales leads are separate queues."""

    SUPPORT = "support"  # a customer needs help from staff
    SALES = "sales"  # a sales or general business inquiry (a lead)


class EscalationReason(StrEnum):
    """Why the chat offered a support request or contact form."""

    CUSTOMER_REQUESTED = "customer_requested"  # the customer asked for a person
    SALES_INQUIRY = "sales_inquiry"  # a sales or business inquiry: optional contact form
    STAFF_ACCESS_NEEDED = "staff_access_needed"  # about their own order or account
    UNRESOLVED_CLARIFICATION = "unresolved_clarification"  # still unclear after clarifying
    REPEATED_NO_MATCH = "repeated_no_match"  # a second unanswered question in a row


ESCALATION_TICKET_TYPES: dict[EscalationReason, TicketType] = {
    EscalationReason.CUSTOMER_REQUESTED: TicketType.SUPPORT,
    EscalationReason.SALES_INQUIRY: TicketType.SALES,
    EscalationReason.STAFF_ACCESS_NEEDED: TicketType.SUPPORT,
    EscalationReason.UNRESOLVED_CLARIFICATION: TicketType.SUPPORT,
    EscalationReason.REPEATED_NO_MATCH: TicketType.SUPPORT,
}


class EscalationOffer(BaseModel):
    """An offer, attached to a chat reply, to send a support request or contact form.

    It is only an offer: nothing is created until the customer fills in the form and
    gives consent, and ignoring it is a valid way to decline.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "examples": [{"reason": "customer_requested", "ticket_type": "support"}]
        },
    )

    reason: EscalationReason = Field(
        description="customer_requested: the customer asked for a person. sales_inquiry: a "
        "sales or business inquiry. staff_access_needed: about their own order or account, "
        "which needs staff. unresolved_clarification: still unclear after a clarifying "
        "question. repeated_no_match: a second question in a row without an answer."
    )
    ticket_type: TicketType = Field(
        description="support: offer a support request. sales: offer an optional contact form."
    )

    @model_validator(mode="after")
    def _check_type(self) -> Self:
        if ESCALATION_TICKET_TYPES[self.reason] is not self.ticket_type:
            raise ValueError(f"{self.reason} offers are {ESCALATION_TICKET_TYPES[self.reason]}")
        return self

    @classmethod
    def for_reason(cls, reason: EscalationReason | None) -> Self | None:
        """The offer for a reason, or None when no offer was made."""
        if reason is None:
            return None
        return cls(reason=reason, ticket_type=ESCALATION_TICKET_TYPES[reason])
