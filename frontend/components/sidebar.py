"""Sidebar: conversation status and controls, service status, privacy note."""

from collections.abc import Callable

import streamlit as st

from frontend.api_client import ConversationStatus
from frontend.formatting import expiry_text
from frontend.state import ChatState

_STATUS_BADGES: dict[ConversationStatus | None, tuple[str, str]] = {
    None: ("Not started", "gray"),
    ConversationStatus.ACTIVE: ("Active", "green"),
    ConversationStatus.CLOSED: ("Ended", "gray"),
    ConversationStatus.EXPIRED: ("Expired", "orange"),
}


def render_sidebar(
    state: ChatState,
    *,
    backend_online: bool,
    on_new: Callable[[], None],
    on_reload: Callable[[], None],
    on_close: Callable[[], None],
    on_delete: Callable[[], None],
    on_check_backend: Callable[[], None],
) -> None:
    busy = state.is_sending
    with st.sidebar:
        st.subheader("Conversation", anchor=False)
        label, color = _STATUS_BADGES[state.conversation_status]
        st.badge(label, color=color)  # type: ignore[arg-type]
        if state.conversation_status is ConversationStatus.ACTIVE and (
            expiry := expiry_text(state.expires_at)
        ):
            st.caption(expiry)

        st.button(
            "New conversation",
            icon=":material/add_comment:",
            on_click=on_new,
            disabled=busy or not state.messages,
            width="stretch",
        )
        has_session = state.credentials is not None
        st.button(
            "Refresh messages",
            icon=":material/sync:",
            on_click=on_reload,
            disabled=busy or not has_session,
            help="Reload this conversation from the server.",
            width="stretch",
        )
        st.button(
            "End conversation",
            icon=":material/check_circle:",
            on_click=on_close,
            disabled=busy or state.conversation_status is not ConversationStatus.ACTIVE,
            help="Stop this conversation. Its messages stay visible.",
            width="stretch",
        )
        with st.popover(
            "Delete conversation",
            icon=":material/delete:",
            disabled=busy or not has_session,
            width="stretch",
        ):
            st.markdown("Permanently delete this conversation and all its messages?")
            st.button(
                "Delete permanently",
                type="primary",
                on_click=on_delete,
                icon=":material/delete_forever:",
                key="confirm-delete",
            )

        st.divider()
        st.subheader("Service", anchor=False)
        if backend_online:
            st.badge("Online", icon=":material/check:", color="green")
        else:
            st.badge("Unavailable", icon=":material/cloud_off:", color="red")
            st.button("Check again", icon=":material/refresh:", on_click=on_check_backend)

        st.divider()
        st.caption(
            "Answers come from our approved help articles and may be summarised by AI. "
            "Your messages are stored with this conversation until it expires or you "
            "delete it. Please don't share passwords or card numbers."
        )
