"""Human support and sales contact (Stage 10): the offer, the form, the confirmation.

Wording rules, for everything on this page:

- A request is *recorded*; nobody "joins the chat". The chat stays with the virtual
  assistant, and any reply from the team comes by the contact method the customer
  chose.
- No names of staff, no response times unless the business configured one (the
  backend's ``expected_response``), no claim that anyone was notified.
- The status says plainly whether the request is only recorded or being handled (the
  backend's ``status_message``).

Native Streamlit components only: ``st.form`` with text inputs, a radio, a select box
and an unticked consent checkbox.
"""

from collections.abc import Callable

import streamlit as st

from frontend.api_client import ErrorKind, EscalationOffer
from frontend.formatting import error_reference, plain_markdown
from frontend.state import ErrorNotice
from frontend.support import (
    CONTACT_PREFERENCES,
    LEAD_CATEGORIES,
    MAX_DESCRIPTION_LENGTH,
    MAX_NAME_LENGTH,
    MAX_PHONE_LENGTH,
    MAX_SUBJECT_LENGTH,
    SALES,
    SUPPORT,
    SupportForm,
    SupportState,
    TicketRecord,
)

# Widget keys of the form's fields (read by ``form_values``, cleared after a success).
FIELD_KEYS = {
    "lead_category": "support_lead_category",
    "name": "support_name",
    "email": "support_email",
    "phone": "support_phone",
    "contact_preference": "support_contact_preference",
    "subject": "support_subject",
    "description": "support_description",
    "consent": "support_consent",
}

OFFER_TEXT = {
    "customer_requested": "Would you like to send a request to our support team?",
    "staff_access_needed": "Questions about your own order or account need our support "
    "team. You can send them a request.",
    "unresolved_clarification": "I couldn't find the right answer for you. You can send a "
    "request to our support team instead.",
    "repeated_no_match": "I couldn't answer that from our help articles. You can send a "
    "request to our support team instead.",
    "sales_inquiry": "Would you like our sales team to get in touch? Leaving your details "
    "is optional.",
}
FORM_TITLES = {SUPPORT: "Contact human support", SALES: "Contact our sales team"}
FORM_NOTE = (
    "Your request goes to our team. This chat stays with the virtual assistant: nobody "
    "joins it, and any reply comes by the contact method you choose."
)
CONSENT_LABEL = (
    "I agree that my contact details are stored so the team can answer this request. "
    "They are not used for marketing."
)
REQUEST_NOTE = (
    "Nobody joins this chat. Any reply from our team comes by your preferred contact method."
)
# Customer-facing status names: recorded only, or actively handled.
STATUS_LABELS: dict[str, tuple[str, str]] = {
    "open": ("Recorded", "gray"),
    "assigned": ("Assigned to our team", "blue"),
    "in_progress": ("Being handled", "blue"),
    "waiting_for_customer": ("Waiting for your reply", "orange"),
    "resolved": ("Resolved", "green"),
    "closed": ("Closed", "gray"),
}

_SUPPORT_ERRORS: dict[ErrorKind, str] = {
    ErrorKind.BACKEND_UNREACHABLE: "We can't reach the support service right now. Select "
    "Try again in a moment; your request won't be sent twice.",
    ErrorKind.TIMEOUT: "This is taking longer than expected. Select Try again; your request "
    "won't be sent twice.",
    ErrorKind.NETWORK_ERROR: "The connection was interrupted. Select Try again; your request "
    "won't be sent twice.",
    ErrorKind.BUSY: "Please select Try again in a moment.",
    ErrorKind.RATE_LIMITED: "You've sent several requests in a short time. Please wait a "
    "moment, then select Try again.",
    ErrorKind.SERVICE_UNAVAILABLE: "Support requests are temporarily unavailable. Select Try "
    "again shortly; your request won't be sent twice.",
    ErrorKind.INVALID_RESPONSE: "We received an unexpected reply. Select Try again; your "
    "request won't be sent twice.",
    ErrorKind.INVALID_MESSAGE: "Some details couldn't be accepted. Please check them and send "
    "the request again.",
    ErrorKind.REQUEST_CONFLICT: "Please send your request again.",
    ErrorKind.SESSION_EXPIRED: "This conversation has expired, so the request couldn't be "
    "sent. Start a new conversation to contact support.",
    ErrorKind.SESSION_NOT_FOUND: "This conversation is no longer available, so the request "
    "couldn't be sent. Start a new conversation to contact support.",
    ErrorKind.TICKET_NOT_FOUND: "We couldn't find that request in this conversation.",
}
# Server-side validation rule -> what to check (the page validates first; this is rare).
_RULE_HINTS = {
    "value_error": "the email address",
    "consent_required": "the consent box",
    "phone_required": "the phone number",
    "phone_format": "the phone number",
    "phone_digits": "the phone number",
    "phone_length": "the phone number",
    "blank_text": "the required fields",
    "string_too_long": "the length of your text",
    "invalid_characters": "for special characters",
}
_GENERIC_ERROR = "Something went wrong and your request couldn't be sent. Please try again later."


def support_error_text(notice: ErrorNotice) -> str:
    text = _SUPPORT_ERRORS.get(notice.kind, _GENERIC_ERROR)
    if notice.kind is ErrorKind.INVALID_MESSAGE:
        hints = sorted({_RULE_HINTS[t] for t in notice.validation_types if t in _RULE_HINTS})
        if hints:
            text = f"Some details couldn't be accepted. Please check {', '.join(hints)}."
    if notice.retry_after and notice.kind in (ErrorKind.RATE_LIMITED, ErrorKind.BUSY):
        text += f" You can try again in about {max(1, round(notice.retry_after))} s."
    return text


def status_label(status: str) -> tuple[str, str]:
    return STATUS_LABELS.get(status, (status.replace("_", " ").capitalize(), "gray"))


# --- The offer under a reply ------------------------------------------------------------


def render_offer(
    message_id: str,
    offer: EscalationOffer,
    *,
    on_open: Callable[[str], None],
    on_dismiss: Callable[[str], None],
) -> None:
    """The reply's offer, with a way to accept and a way to say no."""
    sales = offer.ticket_type == SALES
    fallback = "sales_inquiry" if sales else "customer_requested"
    text = OFFER_TEXT.get(offer.reason, OFFER_TEXT[fallback])
    st.info(text, icon=":material/storefront:" if sales else ":material/support_agent:")
    with st.container(horizontal=True, gap="small"):
        st.button(
            FORM_TITLES[SALES if sales else SUPPORT],
            key=f"{message_id}-offer-open",
            on_click=on_open,
            args=(SALES if sales else SUPPORT,),
            type="primary",
        )
        st.button(
            "No thanks",
            key=f"{message_id}-offer-dismiss",
            on_click=on_dismiss,
            args=(message_id,),
        )


# --- The form ---------------------------------------------------------------------------


def clear_form_fields() -> None:
    """Empty the form after a successful request (before its widgets are drawn)."""
    for key in FIELD_KEYS.values():
        st.session_state.pop(key, None)


def form_values(ticket_type: str) -> SupportForm:
    """What the customer entered, from the form's widgets."""
    state = st.session_state
    return SupportForm(
        ticket_type=ticket_type,
        name=state.get(FIELD_KEYS["name"]) or "",
        email=state.get(FIELD_KEYS["email"]) or "",
        phone=state.get(FIELD_KEYS["phone"]) or "",
        contact_preference=state.get(FIELD_KEYS["contact_preference"]) or "email",
        subject=state.get(FIELD_KEYS["subject"]) or "",
        description=state.get(FIELD_KEYS["description"]) or "",
        consent=bool(state.get(FIELD_KEYS["consent"])),
        lead_category=state.get(FIELD_KEYS["lead_category"]) if ticket_type == SALES else None,
    )


def render_support_form(
    support: SupportState,
    *,
    on_submit: Callable[[], None],
    on_cancel: Callable[[], None],
    on_retry: Callable[[], None],
) -> None:
    ticket_type = support.form_type or SUPPORT
    busy = support.is_submitting
    with st.container(border=True):
        st.subheader(FORM_TITLES[ticket_type], anchor=False)
        st.caption(FORM_NOTE)
        problems = support.form_problems
        with st.form("support-form", border=False, enter_to_submit=False):
            if ticket_type == SALES:
                st.selectbox(
                    "What is your inquiry about?",
                    options=list(LEAD_CATEGORIES),
                    format_func=LEAD_CATEGORIES.__getitem__,
                    key=FIELD_KEYS["lead_category"],
                )
            _field(
                st.text_input,
                "Name",
                "name",
                problems,
                max_chars=MAX_NAME_LENGTH,
                autocomplete="name",
            )
            _field(st.text_input, "Email", "email", problems, autocomplete="email")
            _field(
                st.text_input,
                "Phone (optional)",
                "phone",
                problems,
                max_chars=MAX_PHONE_LENGTH,
                autocomplete="tel",
            )
            st.radio(
                "Preferred contact method",
                options=list(CONTACT_PREFERENCES),
                format_func=CONTACT_PREFERENCES.__getitem__,
                horizontal=True,
                key=FIELD_KEYS["contact_preference"],
            )
            _field(
                st.text_input,
                "Summary" if ticket_type == SUPPORT else "Subject",
                "subject",
                problems,
                max_chars=MAX_SUBJECT_LENGTH,
            )
            _field(
                st.text_area,
                "How can we help?" if ticket_type == SUPPORT else "Tell us about your inquiry",
                "description",
                problems,
                max_chars=MAX_DESCRIPTION_LENGTH,
            )
            st.checkbox(CONSENT_LABEL, value=False, key=FIELD_KEYS["consent"])
            if "consent" in problems:
                st.caption(f":red[{problems['consent']}]")
            st.form_submit_button(
                "Sending..." if busy else "Send request",
                type="primary",
                icon=":material/send:",
                on_click=on_submit,
                disabled=busy,
            )
        if problems:
            st.error("Please check the highlighted fields.", icon=":material/error:")
        if support.last_error is not None:
            st.error(support_error_text(support.last_error), icon=":material/error:")
            if reference := error_reference(support.last_error):
                st.caption(reference)
        with st.container(horizontal=True, gap="small"):
            if support.can_retry:
                st.button(
                    "Try again",
                    icon=":material/refresh:",
                    on_click=on_retry,
                    type="primary",
                    key="support-retry",
                    help="Sends the same request again. It won't be created twice.",
                )
            st.button("Cancel", key="support-cancel", on_click=on_cancel, disabled=busy)


def _field(
    widget: Callable[..., object],
    label: str,
    name: str,
    problems: dict[str, str],
    **options: object,
) -> None:
    widget(label, key=FIELD_KEYS[name], **options)
    if name in problems:
        st.caption(f":red[{problems[name]}]")


# --- Confirmation and tracking ----------------------------------------------------------


def render_confirmation(
    ticket: TicketRecord, *, on_check: Callable[[str], None], status_error: ErrorNotice | None
) -> None:
    """After a confirmed success: what was recorded, and its current status."""
    with st.container(border=True):
        if ticket.already_sent:
            st.info(ticket.message, icon=":material/info:")
        else:
            st.success(ticket.message, icon=":material/check_circle:")
        label, color = status_label(ticket.status)
        st.markdown(f"**Reference:** {plain_markdown(ticket.reference_number)}")
        st.markdown(f"**Subject:** {plain_markdown(ticket.subject)}")
        if ticket.lead_category:
            st.markdown(f"**Topic:** {LEAD_CATEGORIES.get(ticket.lead_category, '')}")
        st.badge(label, color=color)  # type: ignore[arg-type]
        st.caption(ticket.status_message)
        if ticket.expected_response:
            st.caption(plain_markdown(ticket.expected_response))
        st.caption(REQUEST_NOTE)
        st.button(
            "Check status",
            icon=":material/sync:",
            key=f"check-{ticket.ticket_id}",
            on_click=on_check,
            args=(str(ticket.ticket_id),),
        )
        if status_error is not None:
            st.caption(support_error_text(status_error))


def render_request_list(support: SupportState, *, on_check: Callable[[str], None]) -> None:
    """The sidebar's list of this conversation's requests and their status."""
    if not support.tickets:
        return
    st.caption("Your requests")
    for ticket in support.tickets:
        label, color = status_label(ticket.status)
        st.markdown(f"{plain_markdown(ticket.reference_number)}")
        st.badge(label, color=color)  # type: ignore[arg-type]
    st.button(
        "Check status",
        icon=":material/sync:",
        key="check-all-requests",
        on_click=lambda: [on_check(str(t.ticket_id)) for t in support.tickets],
        width="stretch",
    )
