"""The conversation: welcome message, message bubbles, errors and suggestions."""

from collections.abc import Callable, Sequence

import streamlit as st

from frontend.api_client import Decision
from frontend.components.sources import render_choices, render_sources
from frontend.formatting import (
    DECISION_HINTS,
    error_reference,
    error_text,
    generation_label,
    plain_markdown,
)
from frontend.state import ChatMessage, ChatState, Delivery

WELCOME = (
    "Hi there! I'm the virtual support assistant. Ask me about orders, shipping, "
    "returns, refunds, payments or your account, and I'll answer from our help articles."
)


def render_header() -> None:
    st.title("Customer Support", anchor=False)
    st.caption("Instant answers from our approved help articles, around the clock.")


def render_welcome() -> None:
    with st.chat_message("assistant"):
        st.markdown(WELCOME)


def render_examples(questions: Sequence[str], on_choose: Callable[[str], None]) -> None:
    if not questions:
        return
    st.caption("Popular questions")
    with st.container(horizontal=True, gap="small"):
        for index, question in enumerate(questions):
            st.button(
                plain_markdown(question).rstrip(),
                key=f"example-{index}",
                on_click=on_choose,
                args=(question,),
                type="secondary",
            )


def render_messages(
    state: ChatState,
    *,
    on_choose: Callable[[str], None],
    support_url: str | None,
) -> None:
    last_assistant = max(
        (i for i, message in enumerate(state.messages) if message.role == "assistant"),
        default=None,
    )
    for index, message in enumerate(state.messages):
        if message.role == "user":
            _render_user(message, sending=state.is_sending)
        else:
            _render_assistant(
                message,
                offer_choices=index == last_assistant and state.can_send,
                on_choose=on_choose,
                support_url=support_url,
            )


def _render_user(message: ChatMessage, *, sending: bool) -> None:
    with st.chat_message("user"):
        st.markdown(plain_markdown(message.content))
        if message.delivery is Delivery.NOT_SENT:
            st.caption(":material/error: Not sent")
        elif message.delivery is Delivery.PENDING and not sending:
            st.caption(":material/schedule: Not answered yet")


def _render_assistant(
    message: ChatMessage,
    *,
    offer_choices: bool,
    on_choose: Callable[[str], None],
    support_url: str | None,
) -> None:
    with st.chat_message("assistant"):
        st.markdown(plain_markdown(message.content))
        if message.decision is Decision.AMBIGUOUS and message.sources and offer_choices:
            render_choices(message.sources, key_prefix=message.message_id, on_choose=on_choose)
        elif message.decision is Decision.ANSWERED:
            render_sources(message.sources)
        if message.decision is Decision.NO_MATCH and support_url:
            st.link_button("Contact support", support_url, icon=":material/mail:")
        notes = [generation_label(message.generation_method)]
        if message.decision in DECISION_HINTS and message.decision is not Decision.AMBIGUOUS:
            notes.append(DECISION_HINTS[message.decision])
        caption = " · ".join(note for note in notes if note)
        if caption:
            st.caption(caption)


def render_status(
    state: ChatState, *, on_retry: Callable[[], None], on_new: Callable[[], None]
) -> None:
    """The last error, with a retry button when retrying can help, and the way out of
    an ended conversation."""
    notice = state.last_error
    if notice is not None:
        st.error(error_text(notice), icon=":material/error:")
        if reference := error_reference(notice):
            st.caption(reference)
    if state.can_retry:
        st.button("Try again", icon=":material/refresh:", on_click=on_retry, type="primary")
    if state.has_ended:
        if notice is None:
            st.info("This conversation has ended.", icon=":material/info:")
        st.button(
            "Start a new conversation",
            icon=":material/add_comment:",
            on_click=on_new,
            type="primary",
            key="start-new-main",
        )
