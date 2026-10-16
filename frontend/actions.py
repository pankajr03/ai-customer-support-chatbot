"""What the page's controls do: API calls plus the matching ``ChatState`` changes.

No Streamlit here, so every flow can be tested with a fake or in-process backend.
Each function catches ``ApiError`` and records it in the state; nothing else is caught,
so a bug still surfaces (as a generic error, see .streamlit/config.toml) instead of
being hidden.

Sending a message is idempotent end to end: the ``client_message_id`` is chosen when the
message is queued and reused for every attempt, including a retry after the page was
interrupted mid-request. The backend then answers it at most once. Creating a
conversation is not idempotent, so it is never retried automatically: only when the
customer asks (a button).
"""

import logging

from frontend.api_client import ApiError, ChatApiClient, ConversationStatus, ErrorKind
from frontend.state import ChatState, RequestStatus, RetryAction

logger = logging.getLogger(__name__)


def start_conversation(state: ChatState, client: ChatApiClient) -> None:
    """Create a backend conversation for an empty page (on open, or "try again").

    Does nothing if the page already has a conversation: one is never replaced here.
    """
    state.auto_start_done = True
    if state.credentials is not None:
        return
    try:
        created = client.create_conversation()
    except ApiError as error:
        state.record_start_failure(error)
        return
    state.start(created)


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
        if state.needs_sync:  # an earlier message may have been stored after all
            reload_history(state, client)


def start_new_conversation(state: ChatState, client: ChatApiClient) -> bool:
    """Replace the current conversation with a new backend conversation.

    The new one is created first: if that fails, the current conversation and its
    messages are kept and the error is shown. On success the previous conversation is
    closed on the backend (best effort) so it stops accepting messages; it is not
    deleted, and expires there on its own.
    """
    try:
        created = client.create_conversation()
    except ApiError as error:
        state.record_start_failure(error, RetryAction.NEW_CONVERSATION)
        return False
    previous = state.credentials
    still_open = state.conversation_status is ConversationStatus.ACTIVE
    state.replace_conversation(created)
    if previous is not None and still_open:
        try:
            client.close_conversation(previous)
        except ApiError as error:
            logger.info("closing the previous conversation failed: %s", error.kind)
    return True


def close_conversation(state: ChatState, client: ChatApiClient) -> None:
    """End the conversation on the backend; its history stays on the page."""
    if state.credentials is None:
        return
    try:
        state.mark_closed(client.close_conversation(state.credentials))
    except ApiError as error:
        _record(state, error, RetryAction.CLOSE)


def delete_conversation(state: ChatState, client: ChatApiClient) -> bool:
    """Delete the conversation and its messages on the backend, then forget them here.

    Returns False (and keeps everything, to allow another try) if the backend did not
    confirm the deletion. A conversation the backend no longer knows counts as
    deleted. No new conversation is started afterwards until the customer asks.
    """
    if state.credentials is not None:
        try:
            client.delete_conversation(state.credentials)
        except ApiError as error:
            if error.kind is not ErrorKind.SESSION_NOT_FOUND:
                state.note_error(error, retry=RetryAction.DELETE)
                return False
    state.reset()
    return True


def reload_history(state: ChatState, client: ChatApiClient) -> None:
    """Replace the page's messages with the backend's stored history.

    The backend is the authoritative copy; the page's list is only a view of it. A
    conversation that has expired (410) or is unknown (404) is handled like any other
    request for it; a temporary failure leaves the page as it was, with the error.
    """
    credentials = state.credentials
    if credentials is None:
        return
    try:
        history = client.get_history(credentials)
    except ApiError as error:
        _record(state, error, RetryAction.RELOAD)
        return
    state.replace_history(history, credentials)


def retry_last_action(state: ChatState, client: ChatApiClient) -> RetryAction | None:
    """Repeat the action behind the last error ("Try again"). Returns what was retried."""
    action = state.action_to_retry
    if action is RetryAction.START:
        start_conversation(state, client)
    elif action is RetryAction.NEW_CONVERSATION:
        start_new_conversation(state, client)
    elif action is RetryAction.CLOSE:
        close_conversation(state, client)
    elif action is RetryAction.DELETE:
        delete_conversation(state, client)
    elif action is RetryAction.RELOAD:
        reload_history(state, client)
    return action


def _record(state: ChatState, error: ApiError, action: RetryAction) -> None:
    """A failed sidebar action: a conversation that can no longer be used is handled
    as such; anything else is shown, with "Try again" if it was temporary."""
    if error.ends_conversation or error.kind is ErrorKind.SESSION_CLOSED:
        state.record_failure(error)
    else:
        state.note_error(error, retry=action)
