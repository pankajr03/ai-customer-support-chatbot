"""Streamlit customer support chat (Stage 8). From the project root, with the backend
running (``uvicorn app.main:app``):

    python -m streamlit run frontend/app.py

The page is a client of the FastAPI backend and nothing more: questions are answered by
``POST /api/v1/conversations/{id}/chat``. Configuration: ``BACKEND_API_URL`` and the
other variables in ``frontend.config``. See frontend/README.md.

How one message flows through Streamlit's rerun model:

1. Submitting the chat input runs ``_queue`` (a callback, before the script): the
   message is added to the page as pending, with a new ``client_message_id``, and the
   state becomes SENDING.
2. The script renders the conversation with the input disabled, then sends the pending
   message inside a spinner, records the reply (or the error) and reruns.
3. If anything interrupts step 2, the message is still pending with the same ID, so
   sending it again cannot produce a second answer.
"""

import sys
from pathlib import Path

# `streamlit run frontend/app.py` puts frontend/ on sys.path, not the project root.
_PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import streamlit as st  # noqa: E402

from frontend import actions, resources  # noqa: E402
from frontend.components.chat import (  # noqa: E402
    render_examples,
    render_header,
    render_messages,
    render_status,
    render_welcome,
)
from frontend.components.sidebar import render_sidebar  # noqa: E402
from frontend.state import ChatState, get_chat_state  # noqa: E402

STYLES = Path(__file__).with_name("styles.css")
INPUT_KEY = "chat_input"


def main() -> None:
    st.set_page_config(
        page_title="Customer Support",
        page_icon=":material/support_agent:",
        layout="centered",
    )
    st.html(STYLES)

    settings = resources.get_settings()
    client = resources.get_api_client()
    state = get_chat_state()
    backend_online = resources.backend_is_healthy(client)

    def queue(text: str) -> None:
        state.queue_message(text)

    def queue_input() -> None:
        queue(st.session_state.get(INPUT_KEY) or "")

    def start_new() -> None:
        actions.start_new_conversation(state, client)

    def check_backend() -> None:
        resources.backend_is_healthy.clear()

    def delete() -> None:
        if actions.delete_conversation(state, client):
            st.toast("Conversation deleted.", icon=":material/delete:")

    render_sidebar(
        state,
        backend_online=backend_online,
        on_new=start_new,
        on_reload=lambda: actions.reload_history(state, client),
        on_close=lambda: actions.close_conversation(state, client),
        on_delete=delete,
        on_check_backend=check_backend,
    )

    render_header()
    if not backend_online and not state.messages:
        st.warning(
            "The support assistant is offline at the moment. Please try again shortly.",
            icon=":material/cloud_off:",
        )
    render_welcome()
    support_url = str(settings.support_contact_url) if settings.support_contact_url else None
    render_messages(state, on_choose=queue, support_url=support_url)
    if not state.messages:
        render_examples(settings.example_questions, on_choose=queue)
    render_status(state, on_retry=state.retry_pending, on_new=start_new)

    st.chat_input(
        _placeholder(state),
        key=INPUT_KEY,
        max_chars=settings.max_message_characters,
        disabled=not state.can_send,
        on_submit=queue_input,
    )

    if state.is_sending:
        with st.chat_message("assistant"):
            with st.spinner("Looking through our help articles..."):
                actions.send_pending(state, client)
        st.rerun()


def _placeholder(state: ChatState) -> str:
    if state.has_ended:
        return "This conversation has ended"
    if state.is_sending:
        return "Waiting for the reply..."
    return "Type your question..."


main()
