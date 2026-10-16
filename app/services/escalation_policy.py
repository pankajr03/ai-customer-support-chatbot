"""When a chat reply offers human support or a sales contact form (Stage 10).

``escalation_for`` decides, for one chat turn, whether the reply carries an
``EscalationOffer`` and why. It is a small set of fixed rules over what the chat already
knows: the customer's message, the decision of this turn (Stage 4-7: ``answered``,
``ambiguous``, ``no_match``, ``retrieval_unavailable``) and the decisions and offers of
the recent replies. There is no model, score or randomness: the same conversation always
gets the same offers, and every offer names the rule that made it (``reason``).

An offer is only an offer. Nothing is created and no contact details are requested
until the customer opens the form, fills it in and gives explicit consent. Ignoring an
offer declines it; the rules below then avoid repeating it.

Rules, checked in this order (the first that applies wins):

1. ``customer_requested``: the message asks for a person ("I want to speak with a
   human", "can I talk to an agent?", "escalate this"). Offered with any decision,
   every time the customer asks: it is their explicit wish. Questions about the
   assistant ("am I talking to a human?") and refusals ("I don't want to talk to
   anyone") are not requests.
2. ``sales_inquiry``: a sales or business inquiry ("pricing for a business order",
   "bulk order", "can I get a quote?", "become a reseller"). Offers the optional
   contact form (``ticket_type`` sales) next to whatever answer the turn has. Not
   repeated while an earlier sales offer is in the recent replies.

   The remaining rules apply only when this turn did not resolve the question
   (``no_match`` or ``ambiguous``). An answered question needs no handoff, and
   ``retrieval_unavailable`` is a temporary failure whose remedy is to try again.
   They are not repeated while any earlier support offer is in the recent replies.

3. ``staff_access_needed``: the message is about the customer's own order or account
   in a way only staff can check: an order number ("order #48213") or a specific
   problem ("charged twice", "locked out of my account", "my parcel never arrived").
4. ``unresolved_clarification``: the previous reply was a clarifying question and the
   customer's answer still did not resolve it (``ambiguous`` or ``no_match`` again).
5. ``repeated_no_match``: the second ``no_match`` in a row. A single unanswered
   question never escalates: the no-match message already invites rephrasing, and an
   unsupported policy question shows up here once rephrasing has not helped.

"Recent replies" are the assistant replies within the conversation history window
(``CONVERSATION_HISTORY_MAX_MESSAGES`` messages, ``retrieval_unavailable`` replies
skipped when looking for "the previous reply"). Matching is on the customer's message,
lowercased, with simple phrase patterns; it is deliberately narrow, so a message is
treated as a lead only when it plainly is one. Messages are never logged here.
"""

import re
from collections.abc import Sequence

from app.models.escalation import ESCALATION_TICKET_TYPES, EscalationReason, TicketType
from app.services.answer_decision_service import Decision
from app.services.conversation_history_service import PriorReply

# Clauses are judged one at a time, so "Am I talking to a bot? I want a person." is
# still a request even though its first clause is a question about the assistant.
_CLAUSE_BREAK = re.compile(r"[.!?;,\n]+")


def _patterns(*patterns: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(pattern) for pattern in patterns)


_PERSON = (
    r"(?:(?:a|an|the|some|one|your|a real|a live|an actual|real|live)\s+)?"
    r"(?:human|humans|person|people|agent|representative|rep|operator|staff|someone|"
    r"somebody|support team|customer service|customer support|team member|manager)"
)
_HUMAN_REQUEST = _patterns(
    rf"\b(?:speak|speaking|talk|talking|chat|chatting|connect|transfer|put)\s+"
    rf"(?:me\s+)?(?:through\s+)?(?:to|with)\s+{_PERSON}\b",
    r"\b(?:need|want|get|request|prefer|give me)\s+(?:a\s+|an\s+|to\s+)?"
    r"(?:human|real person|live person|live agent|live support|human agent|human support|"
    r"representative|operator|person)\b",
    r"^(?:a\s+)?(?:human|agent|operator|representative|real person|live agent|person)"
    r"(?:\s+please)?$",
    r"\bescalat(?:e|ion)\b",
    rf"\b(?:contact|reach|call)\s+{_PERSON}\b",
)
_BOT_OR_PERSON = r"(?:a\s+|an\s+)?(?:real\s+)?(?:human|person|bot|robot|ai|machine|real)\b"
_NOT_A_REQUEST = _patterns(
    # Refusals: "I don't want to talk to anyone", "without speaking to an agent".
    r"\b(?:don'?t|do not|no need to|never|without|rather not)\s+(?:\w+\s+){0,3}?"
    r"(?:speak|speaking|talk|talking|chat|contact|reach|call|want|need|human|agent|"
    r"person|anyone|someone|escalat)",
    # Questions about the assistant itself: "are you a human?", "am I talking to a bot?"
    rf"\bare\s+you\s+{_BOT_OR_PERSON}",
    rf"\bam\s+i\s+(?:talking|speaking|chatting)\s+(?:to|with)\s+{_BOT_OR_PERSON}",
    rf"\bis\s+this\s+{_BOT_OR_PERSON}",
)

_SALES = _patterns(
    r"(?<!\bmy )(?<!\bour )\b(?:bulk|wholesale|business|corporate|b2b|volume|enterprise|"
    r"trade|reseller)\s+(?:order|orders|ordering|purchase|purchases|purchasing|pricing|"
    r"prices|discount|discounts|quote|quotes|rates?|deal|deals|inquiry|enquiry|account)\b",
    r"\b(?:request|get|need|want|like|send me|ask for)\s+(?:a\s+|an\s+)?(?:price\s+)?"
    r"(?:quote|quotation)\b",
    r"\b(?:quote|quotation)\s+for\b",
    r"\b(?:partnership|partner with you|distribution deal|sponsorship)\b",
    r"\bbecome\s+(?:a|an|your)\s+(?:reseller|distributor|stockist|supplier|affiliate|partner)\b",
    r"\bsales\s+(?:team|department|rep|representative|person)\b",
    r"\b(?:talk|speak|discuss)\w*\s+(?:\w+\s+){0,3}?(?:to|with)\s+(?:your\s+|the\s+|a\s+)?sales\b",
    r"\b(?:discuss|negotiate)\w*\s+(?:\w+\s+){0,2}?(?:pricing|rates|terms|discounts?)\b",
)

_STAFF_ACCESS = _patterns(
    # An order reference with an explicit marker: "order #48213", "order number AB-482133",
    # "#48213". A bare number is not enough ("can I order 5000 items?" is a quantity).
    r"\border\s*(?:number|no\.?|num|id|ref(?:erence)?|#)\s*(?:is\s+)?[:#]?\s*"
    r"[a-z]{0,4}-?\d{4,}\b",
    r"#\s?\d{4,}\b",
    # Problems with the customer's own account, payment or delivery.
    r"\b(?:charged twice|double[- ]charged|charged me twice|overcharged)\b",
    r"\b(?:locked out|been hacked|hacked)\b",
    r"\baccount\s+(?:is\s+|was\s+|has been\s+|got\s+)?"
    r"(?:locked|suspended|disabled|blocked|closed)\b",
    r"\b(?:can'?t|cannot|unable to)\s+(?:log|sign)\s*in\b",
    r"\bmy\s+(?:order|parcel|package|delivery|refund|item|items)\b.*\b(?:never|still not|"
    r"hasn'?t|has not|didn'?t|did not|not)\s+(?:arrived|come|came|received|been)\b",
    r"\b(?:wrong|damaged|broken|missing)\s+(?:item|items|product|parcel|package|order)\b",
    r"\bmy\s+(?:order|parcel|package|item)\s+(?:arrived|came)\s+(?:damaged|broken)\b",
)

# Decisions where the turn did not resolve the customer's question.
_UNRESOLVED = frozenset({Decision.NO_MATCH, Decision.AMBIGUOUS})
_SUPPORT_REASONS = frozenset(
    reason for reason, kind in ESCALATION_TICKET_TYPES.items() if kind is TicketType.SUPPORT
)


def normalise(message: str) -> str:
    """Lowercase, typographic apostrophes made plain, whitespace collapsed."""
    return " ".join(message.lower().replace("’", "'").replace("‘", "'").split())


def _clauses(message: str) -> list[str]:
    return [clause.strip() for clause in _CLAUSE_BREAK.split(normalise(message)) if clause.strip()]


def asks_for_human(message: str) -> bool:
    """Whether a clause of the message asks to be put in touch with a person."""
    return any(
        any(p.search(clause) for p in _HUMAN_REQUEST)
        and not any(p.search(clause) for p in _NOT_A_REQUEST)
        for clause in _clauses(message)
    )


def is_sales_inquiry(message: str) -> bool:
    """Whether the message is plainly a sales or business inquiry."""
    text = normalise(message)
    return any(p.search(text) for p in _SALES)


def needs_staff_access(message: str) -> bool:
    """Whether the message concerns the customer's own order or account in a way only
    staff can look into."""
    text = normalise(message)
    return any(p.search(text) for p in _STAFF_ACCESS)


def _previous_decision(prior: Sequence[PriorReply]) -> Decision | None:
    """The decision of the last reply that answered a question (temporary
    ``retrieval_unavailable`` replies are skipped: the customer just tried again)."""
    for reply in reversed(prior):
        if reply.decision is not Decision.RETRIEVAL_UNAVAILABLE:
            return reply.decision
    return None


def escalation_for(
    message: str, decision: Decision, prior_replies: Sequence[PriorReply] = ()
) -> EscalationReason | None:
    """The escalation offer for this turn, or None (see the module docstring).

    Args:
        message: The customer's message (as searched; see ``search_text``).
        decision: The decision describing the reply the customer receives.
        prior_replies: Earlier replies in the history window, oldest first.
    """
    if asks_for_human(message):
        return EscalationReason.CUSTOMER_REQUESTED
    offered = {reply.escalation_reason for reply in prior_replies} - {None}
    if is_sales_inquiry(message) and EscalationReason.SALES_INQUIRY not in offered:
        return EscalationReason.SALES_INQUIRY
    if decision not in _UNRESOLVED or offered & _SUPPORT_REASONS:
        return None
    if needs_staff_access(message):
        return EscalationReason.STAFF_ACCESS_NEEDED
    previous = _previous_decision(prior_replies)
    if previous is Decision.AMBIGUOUS:
        return EscalationReason.UNRESOLVED_CLARIFICATION
    if decision is Decision.NO_MATCH and previous is Decision.NO_MATCH:
        return EscalationReason.REPEATED_NO_MATCH
    return None
