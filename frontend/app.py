"""Streamlit customer support chat (Stage 8). From the project root, with the backend
running (``uvicorn app.main:app --reload --host 127.0.0.1 --port 8000``):

    streamlit run frontend/app.py

The page is a client of the FastAPI backend and nothing more: questions are answered by
``POST /api/v1/conversations/{id}/chat``. Configuration: ``BACKEND_API_URL`` and the
other variables in ``frontend.config``. See frontend/README.md.

Opening the page starts one backend conversation for this browser session (never again
on a rerun, and never replacing an existing one). If the backend is unavailable, the
page says so and offers to try again.

How one message flows through Streamlit's rerun model:

1. Submitting the chat input runs ``queue_input`` (a callback, before the script): the
   message is checked, added to the page as pending with a new ``client_message_id``,
   and the state becomes SENDING. The input is disabled until the run completes, so it
   cannot be submitted twice.
2. The script renders the conversation, then sends the pending message inside a
   spinner, records the reply (or the error) and reruns.
3. If anything interrupts step 2, the message is still pending with the same ID, so
   sending it again cannot produce a second answer. A message the backend refused goes
   back into the input.

Support requests (Stage 10) follow the same pattern: the form's submit callback
validates and queues the request with its ``client_request_id``, the script sends it in
a spinner and reruns, and only the backend's confirmation shows a reference number.
"""

import logging
import sys
import uuid
from pathlib import Path

# `streamlit run frontend/app.py` puts frontend/ on sys.path, not the project root.
_PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import streamlit as st  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from frontend import actions, resources  # noqa: E402
from frontend.api_client import ApiError, ErrorKind  # noqa: E402
from frontend.components.chat import (  # noqa: E402
    render_demo_notice,
    render_examples,
    render_header,
    render_messages,
    render_status,
    render_welcome,
)
from frontend.components.sidebar import render_sidebar  # noqa: E402
from frontend.components.support import (  # noqa: E402
    clear_form_fields,
    form_values,
    render_confirmation,
    render_offer,
    render_support_form,
)
from frontend.config import FrontendSettings, describe_settings_error  # noqa: E402
from frontend.formatting import UNEXPECTED_ERROR  # noqa: E402
from frontend.state import (  # noqa: E402
    STATE_KEY,
    ChatMessage,
    ChatState,
    RetryAction,
    get_chat_state,
)
from frontend.support import SUPPORT_KEY, get_support_state  # noqa: E402

STYLES = Path(__file__).with_name("styles.css")
INPUT_KEY = "chat_input"

logger = logging.getLogger("frontend.app")


def main() -> None:
    st.set_page_config(
        page_title="Customer Support",
        page_icon=":material/support_agent:",
        layout="centered",
    )
    st.html(STYLES)
    try:
        _page()
    except ValidationError as exc:
        if exc.title != FrontendSettings.__name__:
            _unexpected_error()
            return
        # Invalid frontend settings (BACKEND_API_URL, ...): a setup problem for whoever
        # runs the page, so it is named, without values, in the log and on the page.
        message = describe_settings_error(exc)
        logger.critical(message)
        st.error(f"{message}. See frontend/README.md.", icon=":material/settings:")
    except Exception:
        _unexpected_error()


def _unexpected_error() -> None:
    """Called while handling the exception. A bug, not an API failure (those are handled
    in frontend.actions). The details go to the server log only; the customer gets a way
    to carry on. Streamlit's rerun and stop signals are BaseExceptions and pass through.
    """
    logger.exception("unexpected error while drawing the chat page")
    st.error(UNEXPECTED_ERROR, icon=":material/error:")
    st.button("Start over", icon=":material/restart_alt:", on_click=_start_over)


def _start_over() -> None:
    """Forget this browser session's chat state; the next run starts a new conversation.

    The previous conversation is left to expire on the backend.
    """
    st.session_state.pop(STATE_KEY, None)
    st.session_state.pop(SUPPORT_KEY, None)


def _page() -> None:
    settings = resources.get_settings()
    client = resources.get_api_client()
    state = get_chat_state()
    support = get_support_state()
    state.check_expiry()
    if state.last_error is not None and state.last_error.kind is ErrorKind.BACKEND_UNREACHABLE:
        # Don't let a cached "Online" contradict the error shown on the page.
        resources.backend_status.clear()
    backend = resources.backend_status(client)
    max_chars = state.max_chars(settings.max_message_characters)

    def queue(text: str) -> None:
        state.queue_message(text, max_chars=max_chars)

    def resend(message_id: str) -> None:
        state.resend(message_id)

    def queue_input() -> None:
        queue(st.session_state.get(INPUT_KEY) or "")

    def start_new() -> None:
        actions.start_new_conversation(state, client)

    def retry_action() -> None:
        resources.backend_status.clear()
        if actions.retry_last_action(state, client) is RetryAction.DELETE:
            if state.credentials is None:
                st.toast("Conversation deleted.", icon=":material/delete:")

    def check_backend() -> None:
        resources.backend_status.clear()

    def delete() -> None:
        if actions.delete_conversation(state, client):
            st.toast("Conversation deleted.", icon=":material/delete:")

    def contact(ticket_type: str) -> None:
        support.open_form(ticket_type)

    def submit_request() -> None:
        if support.form_type is not None:
            support.queue_submission(form_values(support.form_type))

    def check_request(ticket_id: str) -> None:
        actions.check_ticket_status(state, support, client, uuid.UUID(ticket_id))

    def offer(message: ChatMessage) -> None:
        if message.escalation_offer is None:  # pragma: no cover - checked by the caller
            return
        if support.offer_visible(message.message_id, message.escalation_offer.ticket_type):
            render_offer(
                message.message_id,
                message.escalation_offer,
                on_open=contact,
                on_dismiss=support.dismiss,
            )

    render_header()
    if settings.demo_mode:
        render_demo_notice(resources.answer_mode(client) if backend.accepts_chat else None)
    if state.should_auto_start:
        if backend.accepts_chat:
            with st.spinner("Connecting you to support..."):
                actions.start_conversation(state, client)
        else:  # known to be down or not ready: don't wait for a connection timeout
            state.auto_start_done = True
            state.record_start_failure(ApiError(ErrorKind.BACKEND_UNREACHABLE))

    render_sidebar(
        state,
        backend=backend,
        on_new=start_new,
        on_reload=lambda: actions.reload_history(state, client),
        on_close=lambda: actions.close_conversation(state, client),
        on_delete=delete,
        on_check_backend=check_backend,
        support=support,
        on_contact=contact,
        on_check_requests=check_request,
    )

    render_welcome()
    support_url = str(settings.support_contact_url) if settings.support_contact_url else None
    # Requests belong to the conversation on the page; a new one starts without them.
    support.follow(state.credentials.session_id if state.credentials else None)
    render_messages(
        state, on_choose=queue, on_resend=resend, support_url=support_url, on_offer=offer
    )
    if not state.messages and state.can_send:
        render_examples(settings.example_questions, on_choose=queue)
    render_status(
        state, on_retry=state.retry_pending, on_retry_action=retry_action, on_new=start_new
    )
    if support.clear_form:
        clear_form_fields()
        support.clear_form = False
    if support.form_open:
        render_support_form(
            support, on_submit=submit_request, on_cancel=support.close_form, on_retry=support.retry
        )
    elif (latest := support.latest) is not None:
        render_confirmation(latest, on_check=check_request, status_error=support.status_error)

    # A message that could not be sent goes back into the input (before it is drawn).
    if (draft := state.take_draft()) is not None:
        st.session_state[INPUT_KEY] = draft
    st.chat_input(
        _placeholder(state),
        key=INPUT_KEY,
        max_chars=max_chars,
        disabled=not state.can_send,
        submit_mode="disable",
        on_submit=queue_input,
    )

    if state.is_sending:
        with st.chat_message("assistant"):
            with st.spinner("Looking through our help articles..."):
                actions.send_pending(state, client)
        st.rerun()
    if support.is_submitting:
        with st.spinner("Sending your request..."):
            actions.submit_support_request(state, support, client)
        st.rerun()


def _placeholder(state: ChatState) -> str:
    if state.has_ended:
        return "This conversation has ended"
    if state.is_sending:
        return "Waiting for the reply..."
    return "Type your question..."


main()
