"""What the page's controls do: API calls plus the matching ``ChatState`` changes.

No Streamlit here, so every flow can be tested with a fake or in-process backend.
Each function catches ``ApiError`` and records it in the state; nothing else is caught,
so a bug still surfaces (as a generic error, see .streamlit/config.toml) instead of
being hidden.

Sending a message is idempotent end to end: the ``client_message_id`` is chosen when the
message is queued and reused for every attempt, including a retry after the page was
interrupted mid-request. The backend then answers it at most once.
"""

import logging

from frontend.api_client import ApiError, ChatApiClient, ErrorKind
from frontend.state import ChatState, RequestStatus

logger = logging.getLogger(__name__)


def send_pending(state: ChatState, client: ChatApiClient) -> None:
    """Send the queued message, starting a backend conversation first if needed.

    Always leaves the state out of SENDING, so the page never loops on a rerun.
    """
    pending = state.pending
    if not state.is_sending:
        return
    if pending is None:  # pragma: no cover - queue_message always sets both
        state.request_status = RequestStatus.IDLE
        return
    try:
        if state.credentials is None:
            if state.conversation_status is not None:  # ended; a new one must be started
                state.fail_without_request(ErrorKind.SESSION_EXPIRED)
                return
            state.start(client.create_conversation())
        assert state.credentials is not None  # noqa: S101 - set just above
        reply = client.send_message(state.credentials, pending.text, pending.client_message_id)
    except ApiError as error:
        state.record_failure(error)
        return
    if state.pending == pending:  # still the same message (not reset meanwhile)
        state.record_reply(reply)


def close_conversation(state: ChatState, client: ChatApiClient) -> None:
    """End the conversation on the backend; its history stays on the page."""
    if state.credentials is None:
        return
    try:
        state.mark_closed(client.close_conversation(state.credentials))
    except ApiError as error:
        state.record_failure(error)


def delete_conversation(state: ChatState, client: ChatApiClient) -> bool:
    """Delete the conversation and its messages on the backend, then forget everything.

    Returns False (and keeps the state, to allow another try) if the backend could not
    be reached. A conversation the backend no longer knows counts as deleted.
    """
    if state.credentials is not None:
        try:
            client.delete_conversation(state.credentials)
        except ApiError as error:
            if not error.ends_conversation:
                state.record_failure(error)
                return False
    state.reset()
    return True


def start_new_conversation(state: ChatState, client: ChatApiClient) -> None:
    """Clear the page for a new conversation. The old one is closed on the backend
    (best effort) so it stops accepting messages; it expires there on its own."""
    if state.credentials is not None and not state.has_ended:
        try:
            client.close_conversation(state.credentials)
        except ApiError as error:
            logger.info("closing the previous conversation failed: %s", error.kind)
    state.reset()


def reload_history(state: ChatState, client: ChatApiClient) -> None:
    """Replace the page's messages with the backend's stored history."""
    if state.credentials is None:
        return
    try:
        state.replace_history(client.get_history(state.credentials))
    except ApiError as error:
        state.record_failure(error)
