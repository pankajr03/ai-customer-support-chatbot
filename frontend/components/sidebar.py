"""Sidebar: conversation status and controls, service status, help.

- **New conversation** creates a new backend conversation first and only then replaces
  the current one (which is closed, not deleted). If creating fails, the current one
  stays.
- **Delete conversation** deletes it on the backend (after a confirmation) and only then
  clears the page and its credentials. There is deliberately no "clear the screen"
  button: hiding messages locally would look like deleting them while the backend
  still stores them.
"""

from collections.abc import Callable

import streamlit as st

from frontend.api_client import BackendStatus, ConversationStatus
from frontend.components.support import render_request_list
from frontend.formatting import expiry_text
from frontend.state import ChatState
from frontend.support import SALES, SUPPORT, SupportState

_STATUS_BADGES: dict[ConversationStatus | None, tuple[str, str]] = {
    None: ("Not started", "gray"),
    ConversationStatus.ACTIVE: ("Active", "green"),
    ConversationStatus.CLOSED: ("Ended", "gray"),
    ConversationStatus.EXPIRED: ("Expired", "orange"),
}

HELP = (
    "Ask one question at a time; you can follow up on the previous answer. "
    "**New conversation** starts over (the current one is closed, not deleted). "
    "**Delete conversation** removes this conversation and its messages from our "
    "servers."
)
# Badge, icon, colour and an optional explanation for each backend state.
_SERVICE_BADGES: dict[BackendStatus, tuple[str, str, str, str | None]] = {
    BackendStatus.ONLINE: ("Online", ":material/check:", "green", None),
    BackendStatus.WARMING_UP: (
        "Online",
        ":material/check:",
        "green",
        "Search is starting up, so the first answer may take a little longer.",
    ),
    BackendStatus.LIMITED: (
        "Limited",
        ":material/warning:",
        "orange",
        "Search is partly unavailable, so some questions can't be answered right now.",
    ),
    BackendStatus.UNAVAILABLE: ("Unavailable", ":material/cloud_off:", "red", None),
}
PRIVACY = (
    "Answers come from our approved help articles and may be summarised by AI. Your "
    "messages are stored with this conversation until it expires or you delete it. "
    "Please don't share passwords or card numbers."
)


def render_sidebar(
    state: ChatState,
    *,
    backend: BackendStatus,
    on_new: Callable[[], None],
    on_reload: Callable[[], None],
    on_close: Callable[[], None],
    on_delete: Callable[[], None],
    on_check_backend: Callable[[], None],
    support: SupportState | None = None,
    on_contact: Callable[[str], None] | None = None,
    on_check_requests: Callable[[str], None] | None = None,
) -> None:
    busy = state.is_sending
    status = state.conversation_status
    has_session = state.credentials is not None
    with st.sidebar:
        st.subheader("Conversation", anchor=False)
        label, color = _STATUS_BADGES[status]
        st.badge(label, color=color)  # type: ignore[arg-type]
        if status is ConversationStatus.ACTIVE and (expiry := expiry_text(state.expires_at)):
            st.caption(expiry)

        st.button(
            "New conversation",
            icon=":material/add_comment:",
            on_click=on_new,
            # Nothing to replace in an active conversation that has no messages yet.
            disabled=busy or (status is ConversationStatus.ACTIVE and not state.messages),
            width="stretch",
        )
        st.button(
            "Refresh messages",
            icon=":material/sync:",
            on_click=on_reload,
            disabled=busy or not has_session or status is ConversationStatus.EXPIRED,
            help="Reload this conversation from the server.",
            width="stretch",
        )
        st.button(
            "End conversation",
            icon=":material/check_circle:",
            on_click=on_close,
            disabled=busy or status is not ConversationStatus.ACTIVE,
            help="Stop this conversation. Its messages stay visible.",
            width="stretch",
        )
        with st.popover(
            "Delete conversation",
            icon=":material/delete:",
            disabled=busy or not has_session,
            width="stretch",
        ):
            st.markdown(
                "Permanently delete this conversation and all its messages from our "
                "servers? This can't be undone."
            )
            if support is not None and support.tickets:
                st.caption(
                    "Your support requests stay with our team: deleting the chat doesn't "
                    "withdraw them. They're no longer linked to this conversation."
                )
            st.button(
                "Delete permanently",
                type="primary",
                on_click=on_delete,
                icon=":material/delete_forever:",
                key="confirm-delete",
            )

        if support is not None and on_contact is not None:
            st.divider()
            st.subheader("Need a person?", anchor=False)
            # Any conversation the backend still accepts requests for (closed ones too).
            can_contact = has_session and status is not ConversationStatus.EXPIRED
            off = busy or support.is_submitting or not can_contact
            st.button(
                "Contact human support",
                icon=":material/support_agent:",
                on_click=on_contact,
                args=(SUPPORT,),
                disabled=off,
                help="Send a request to our support team. Nobody joins this chat.",
                width="stretch",
            )
            st.button(
                "Sales inquiry",
                icon=":material/storefront:",
                on_click=on_contact,
                args=(SALES,),
                disabled=off,
                help="Bulk orders, business pricing, partnerships or a demonstration.",
                width="stretch",
            )
            if on_check_requests is not None:
                render_request_list(support, on_check=on_check_requests)

        st.divider()
        st.subheader("Service", anchor=False)
        label, icon, color, note = _SERVICE_BADGES[backend]
        st.badge(label, icon=icon, color=color)  # type: ignore[arg-type]
        if note:
            st.caption(note)
        if backend in (BackendStatus.LIMITED, BackendStatus.UNAVAILABLE):
            st.button("Check again", icon=":material/refresh:", on_click=on_check_backend)

        st.divider()
        with st.expander("Help", icon=":material/help:"):
            st.markdown(HELP)
        st.caption(PRIVACY)
