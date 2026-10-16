"""Escalation rules (Stage 10, Task 2): when a chat reply offers human support or a
sales contact form, and when it must not.

Unit tests for ``app.services.escalation_policy``, then the same rules over HTTP: the
offer travels with the chat response and the history, a retry returns the same offer,
and chatting never creates a ticket or stores contact details.
"""

import uuid
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from app.models.conversation import ConversationMessage
from app.models.escalation import (
    ESCALATION_TICKET_TYPES,
    EscalationOffer,
    EscalationReason,
    TicketType,
)
from app.services.answer_decision_service import Decision
from app.services.conversation_history_service import PriorReply
from app.services.escalation_policy import (
    asks_for_human,
    escalation_for,
    is_sales_inquiry,
    needs_staff_access,
)
from tests import test_api_workflows as workflows
from tests.test_api_workflows import BASE, RESET, SHIPPING, UNKNOWN, App, contract

# The workflow tests' running-app fixtures, shared with this module.
make_app = workflows.make_app
chat_app = workflows.chat_app

R = EscalationReason
ANSWERED, AMBIGUOUS, NO_MATCH, UNAVAILABLE = (
    Decision.ANSWERED,
    Decision.AMBIGUOUS,
    Decision.NO_MATCH,
    Decision.RETRIEVAL_UNAVAILABLE,
)


def replies(*decisions: Decision, offer: EscalationReason | None = None) -> list[PriorReply]:
    """Earlier replies; ``offer`` is attached to the last one."""
    prior = [PriorReply(decision) for decision in decisions]
    if offer is not None:
        prior[-1] = PriorReply(prior[-1].decision, offer)
    return prior


# --- Intent detection -----------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "I want to speak with a human.",
        "I want to speak with a human",
        "Can I talk to someone?",
        "Is it possible to speak to a real person?",
        "Please connect me to customer service",
        "human",
        "Agent please",
        "Please escalate this.",
        "I need a human agent",
        "Am I talking to a bot? I want a real person.",
        "I never got my order and want to talk to a person",
        "I WANT TO TALK TO A HUMAN!!!",
        "Can I talk to someone about returns?",
    ],
)
def test_requests_for_a_person_are_recognised(message: str) -> None:
    assert asks_for_human(message)


@pytest.mark.parametrize(
    "message",
    [
        "Are you a human?",
        "Am I talking to a bot?",
        "Is this a real person?",
        "I don't want to talk to anyone, just tell me the return policy",
        "How do I return an item without speaking to an agent?",
        "Can I speak Spanish with you?",
        "Do you have people working at weekends?",
        "The person who delivered my parcel was rude",
        "How many staff work in your shop?",
        "What are your opening hours?",
    ],
)
def test_mentions_of_people_are_not_requests(message: str) -> None:
    assert not asks_for_human(message)


@pytest.mark.parametrize(
    "message",
    [
        "I want to discuss pricing for a business order.",
        "Do you offer bulk orders?",
        "Can I get a quote for 500 units?",
        "Do you have wholesale pricing?",
        "I'd like to become a reseller",
        "Can I talk to your sales team?",
        "We'd like to negotiate terms for a corporate purchase",
    ],
)
def test_sales_inquiries_are_recognised(message: str) -> None:
    assert is_sales_inquiry(message)


@pytest.mark.parametrize(
    "message",
    [
        "What does shipping cost?",
        "How much is this product?",
        "Do you have a discount code?",
        "My business order has not arrived",  # an existing order: support, not a lead
        "Where is my order?",
        "Can I return my order?",
        "Thanks, that helps!",
    ],
)
def test_ordinary_questions_are_not_leads(message: str) -> None:
    assert not is_sales_inquiry(message)


@pytest.mark.parametrize(
    "message",
    [
        "Where is order #48213?",
        "My order number is AB-482133 and it's late",
        "I was charged twice",
        "I'm locked out of my account",
        "My account has been suspended",
        "I can't log in",
        "My parcel never arrived",
        "I received a damaged item",
    ],
)
def test_customer_specific_issues_are_recognised(message: str) -> None:
    assert needs_staff_access(message)


@pytest.mark.parametrize(
    "message",
    [
        "Where is my order?",
        "How do I track my order?",
        "Can I order 5000 items?",  # a quantity, not an order number
        "What is your return policy?",
        "How do I reset my password?",
    ],
)
def test_general_questions_need_no_staff_access(message: str) -> None:
    assert not needs_staff_access(message)


# --- The rules ------------------------------------------------------------------------


@pytest.mark.parametrize("decision", list(Decision))
def test_a_request_for_a_person_is_offered_support_with_any_decision(decision: Decision) -> None:
    assert escalation_for("I want to speak with a human.", decision) is R.CUSTOMER_REQUESTED


def test_a_request_for_a_person_is_offered_every_time_it_is_made() -> None:
    prior = replies(NO_MATCH, offer=R.CUSTOMER_REQUESTED)
    assert escalation_for("A human, please", NO_MATCH, prior) is R.CUSTOMER_REQUESTED


def test_sales_inquiries_get_the_contact_form_next_to_any_answer() -> None:
    message = "I want to discuss pricing for a business order."
    assert escalation_for(message, NO_MATCH) is R.SALES_INQUIRY
    assert escalation_for(message, ANSWERED) is R.SALES_INQUIRY


def test_a_declined_sales_offer_is_not_repeated() -> None:
    prior = replies(ANSWERED, offer=R.SALES_INQUIRY)
    assert escalation_for("Do you offer bulk orders?", ANSWERED, prior) is None


def test_a_single_unanswered_question_does_not_escalate() -> None:
    assert escalation_for("Do you sell submarines?", NO_MATCH) is None
    assert escalation_for("Do you sell submarines?", NO_MATCH, replies(ANSWERED)) is None


def test_a_second_unanswered_question_in_a_row_offers_support() -> None:
    assert escalation_for("And boats?", NO_MATCH, replies(NO_MATCH)) is R.REPEATED_NO_MATCH


def test_a_temporary_outage_in_between_does_not_hide_the_repeat() -> None:
    prior = replies(NO_MATCH, UNAVAILABLE)
    assert escalation_for("And boats?", NO_MATCH, prior) is R.REPEATED_NO_MATCH


def test_an_answer_in_between_resets_the_count() -> None:
    assert escalation_for("And boats?", NO_MATCH, replies(NO_MATCH, ANSWERED)) is None


@pytest.mark.parametrize("decision", [AMBIGUOUS, NO_MATCH])
def test_still_unresolved_after_a_clarifying_question_offers_support(decision: Decision) -> None:
    assert escalation_for("the other one", decision, replies(AMBIGUOUS)) is (
        R.UNRESOLVED_CLARIFICATION
    )


def test_a_first_clarifying_question_does_not_escalate() -> None:
    assert escalation_for("Tell me about shipping", AMBIGUOUS, replies(ANSWERED)) is None


def test_a_clarification_that_gets_answered_does_not_escalate() -> None:
    assert escalation_for("the first one", ANSWERED, replies(AMBIGUOUS)) is None


def test_customer_specific_issues_offer_support_when_unanswered() -> None:
    assert escalation_for("Where is order #48213?", NO_MATCH) is R.STAFF_ACCESS_NEEDED
    assert escalation_for("I was charged twice", AMBIGUOUS) is R.STAFF_ACCESS_NEEDED


def test_customer_specific_issues_answered_by_an_faq_do_not_escalate() -> None:
    assert escalation_for("My parcel never arrived", ANSWERED) is None


@pytest.mark.parametrize(
    "message", ["Where is order #48213?", "Do you sell submarines?", "the other one"]
)
def test_retrieval_outages_never_escalate_on_their_own(message: str) -> None:
    assert escalation_for(message, UNAVAILABLE, replies(NO_MATCH)) is None


@pytest.mark.parametrize(
    "earlier",
    [R.REPEATED_NO_MATCH, R.STAFF_ACCESS_NEEDED, R.UNRESOLVED_CLARIFICATION, R.CUSTOMER_REQUESTED],
)
def test_a_declined_support_offer_is_not_repeated_automatically(earlier: EscalationReason) -> None:
    prior = replies(NO_MATCH, NO_MATCH, offer=earlier)
    assert escalation_for("Where is order #48213?", NO_MATCH, prior) is None
    assert escalation_for("And boats?", NO_MATCH, prior) is None
    # ...but asking for a person still works.
    assert escalation_for("Let me talk to a person", NO_MATCH, prior) is R.CUSTOMER_REQUESTED


def test_a_sales_offer_does_not_block_a_later_support_offer() -> None:
    prior = replies(NO_MATCH, offer=R.SALES_INQUIRY)
    assert escalation_for("And boats?", NO_MATCH, prior) is R.REPEATED_NO_MATCH


def test_a_request_for_a_person_wins_over_a_sales_inquiry() -> None:
    message = "Can I talk to a person about a bulk order?"
    assert escalation_for(message, NO_MATCH) is R.CUSTOMER_REQUESTED


@pytest.mark.parametrize(
    "message",
    ["How do I reset my password?", "Thanks!", "What is your return policy?", "hello"],
)
@pytest.mark.parametrize("decision", list(Decision))
def test_ordinary_first_messages_never_escalate(message: str, decision: Decision) -> None:
    assert escalation_for(message, decision) is None


def test_the_rules_are_deterministic() -> None:
    prior = replies(AMBIGUOUS)
    results = {escalation_for("the other one", NO_MATCH, prior) for _ in range(20)}
    assert results == {R.UNRESOLVED_CLARIFICATION}


# --- The offer model --------------------------------------------------------------------


def test_every_reason_has_a_ticket_type_and_only_sales_leads_are_sales() -> None:
    assert set(ESCALATION_TICKET_TYPES) == set(EscalationReason)
    sales = {reason for reason, kind in ESCALATION_TICKET_TYPES.items() if kind is TicketType.SALES}
    assert sales == {R.SALES_INQUIRY}


def test_an_offer_must_name_the_ticket_type_of_its_reason() -> None:
    with pytest.raises(ValueError, match="sales"):
        EscalationOffer(reason=R.SALES_INQUIRY, ticket_type=TicketType.SUPPORT)
    assert EscalationOffer.for_reason(None) is None
    offer = EscalationOffer.for_reason(R.CUSTOMER_REQUESTED)
    assert offer is not None and offer.model_dump() == {
        "reason": "customer_requested",
        "ticket_type": "support",
    }


# --- Over HTTP ------------------------------------------------------------------------


def support_rows(app: App) -> tuple[int, int]:
    with app.database.read() as db:
        tickets = db.execute(text("SELECT count(*) FROM support_tickets")).scalar()
        contacts = db.execute(text("SELECT count(*) FROM customer_contacts")).scalar()
    return int(tickets or 0), int(contacts or 0)


def offer(body: dict[str, Any]) -> dict[str, Any] | None:
    return body["escalation_offer"]


def test_asking_for_a_human_offers_a_support_request_without_creating_one(chat_app: App) -> None:
    sid, headers = chat_app.start()

    body = contract(chat_app.chat(sid, headers, "I want to speak with a human."))

    assert body["decision"] == "no_match"  # the reply itself is unchanged
    assert offer(body) == {"reason": "customer_requested", "ticket_type": "support"}
    assert support_rows(chat_app) == (0, 0)


def test_answered_questions_carry_no_offer(chat_app: App) -> None:
    sid, headers = chat_app.start()

    body = contract(chat_app.chat(sid, headers, RESET))

    assert body["decision"] == "answered" and offer(body) is None


def test_only_the_second_unanswered_question_offers_support(chat_app: App) -> None:
    sid, headers = chat_app.start()

    first = contract(chat_app.chat(sid, headers, UNKNOWN))
    second = contract(chat_app.chat(sid, headers, "Do you sell rockets?"))
    third = contract(chat_app.chat(sid, headers, "Do you sell trains?"))

    assert offer(first) is None
    assert offer(second) == {"reason": "repeated_no_match", "ticket_type": "support"}
    assert offer(third) is None  # not repeated after being ignored
    assert support_rows(chat_app) == (0, 0)


def test_unresolved_after_clarification_offers_support(chat_app: App) -> None:
    sid, headers = chat_app.start()

    clarify = contract(chat_app.chat(sid, headers, SHIPPING))
    still = contract(chat_app.chat(sid, headers, "Neither of those, something else"))

    assert clarify["decision"] == "ambiguous" and offer(clarify) is None
    assert still["decision"] in ("ambiguous", "no_match")
    assert offer(still) == {"reason": "unresolved_clarification", "ticket_type": "support"}


def test_a_sales_inquiry_offers_the_sales_contact_form(chat_app: App) -> None:
    sid, headers = chat_app.start()

    body = contract(chat_app.chat(sid, headers, "I want to discuss pricing for a business order."))

    assert offer(body) == {"reason": "sales_inquiry", "ticket_type": "sales"}
    assert support_rows(chat_app) == (0, 0)


def test_the_offer_is_kept_in_history_and_replayed_on_retry(chat_app: App) -> None:
    sid, headers = chat_app.start()
    client_id = "4c981eb1-7847-41c0-bf6b-0a0e1b9d6d91"
    message = "Can I talk to an agent?"

    first = contract(chat_app.chat(sid, headers, message, client_message_id=client_id))
    retry = chat_app.chat(sid, headers, message, client_message_id=client_id)
    history = chat_app.client.get(f"{BASE}/{sid}/messages", headers=headers).json()

    assert retry.headers.get("Idempotent-Replayed") == "true"
    assert (
        offer(contract(retry))
        == offer(first)
        == {
            "reason": "customer_requested",
            "ticket_type": "support",
        }
    )
    assert [m["escalation_offer"] for m in history["messages"]] == [None, offer(first)]


def test_only_replies_can_carry_an_offer() -> None:
    session_id = uuid.uuid4()
    reply = ConversationMessage.assistant_message(
        session_id, 2, "Reply", NO_MATCH, escalation_reason=R.REPEATED_NO_MATCH
    )

    assert reply.escalation_reason is R.REPEATED_NO_MATCH
    with pytest.raises(ValidationError, match="escalation_reason"):
        ConversationMessage.model_validate(
            {
                **ConversationMessage.user_message(session_id, 1, "Hi").model_dump(),
                "escalation_reason": R.CUSTOMER_REQUESTED,
            }
        )
