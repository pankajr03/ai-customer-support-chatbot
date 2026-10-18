"""Session, error and message helpers shared by the staff dashboard's sections.

Everything here keeps to two rules:

- **Only the backend's confirmation counts.** A success message is set (``flash``) only
  after the API call returned the saved record, then shown once after the rerun that
  reloads the list, so the page never claims a save that did not happen.
- **No raw errors.** API failures are ``AdminApiError``; ``call`` shows a fixed
  sentence (or the backend's short, safe message), the request ID and the invalid fields,
  never a stack trace or the submitted values. A ``401`` signs the user out.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Any

import streamlit as st
from pydantic import SecretStr

from frontend.admin_api_client import AdminApiError, AdminErrorKind, StaffIdentity

TOKEN_KEY = "admin_staff_token"  # noqa: S105 - a session-state key, not a secret
IDENTITY_KEY = "admin_identity"
NOTICE_KEY = "admin_notice"
FLASH_KEY = "admin_flash"
SELECTED_FAQ_KEY = "admin_selected_faq"
# Session keys owned by sections; all removed on sign-out.
SESSION_PREFIX = "admin_"

MESSAGES: dict[AdminErrorKind, str] = {
    AdminErrorKind.UNREACHABLE: "The backend cannot be reached. Check that the API is running.",
    AdminErrorKind.TIMEOUT: "The backend did not answer in time. Nothing is known to be saved: "
    "reload to check before trying again.",
    AdminErrorKind.SIGNED_OUT: "Your staff token is not valid (any more). Please sign in again.",
    AdminErrorKind.STAFF_DISABLED: "Staff access is disabled on this server: no staff tokens "
    "are configured (STAFF_TOKEN_HASHES).",
    AdminErrorKind.FORBIDDEN: "Your staff role does not allow this.",
    AdminErrorKind.NOT_FOUND: "Not found. It may have been removed or the ID is wrong.",
    AdminErrorKind.CONFLICT: "This was changed by someone else meanwhile, or the change is "
    "not allowed from its current state. Reload and try again.",
    AdminErrorKind.INVALID: "Some fields are not valid.",
    AdminErrorKind.UNAVAILABLE: "This is temporarily unavailable. Please try again shortly.",
    AdminErrorKind.SERVER_ERROR: "Something went wrong on the server. Nothing was changed by "
    "this page; please try again.",
    AdminErrorKind.INVALID_RESPONSE: "The backend sent an unexpected response. Check that the "
    "dashboard and the API are the same version.",
}


def current_token() -> SecretStr | None:
    return st.session_state.get(TOKEN_KEY)


def current_identity() -> StaffIdentity | None:
    return st.session_state.get(IDENTITY_KEY)


def sign_out(notice: str | None = None) -> None:
    """Forget the token, the identity and every section's state."""
    for key in [k for k in st.session_state if str(k).startswith(SESSION_PREFIX)]:
        del st.session_state[key]
    if notice:
        st.session_state[NOTICE_KEY] = notice


def error_message(error: AdminApiError) -> str:
    message = MESSAGES[error.kind]
    if error.kind in (AdminErrorKind.CONFLICT, AdminErrorKind.INVALID) and error.message:
        message = error.message  # the backend's short, specific sentence
    reference = f" (request ID {error.request_id})" if error.request_id else ""
    return message + reference


def show_error(error: AdminApiError) -> None:
    st.error(error_message(error))
    for field in error.fields:
        st.caption(f"• {field}")


def call(action: Callable[[], Any]) -> Any:
    """Run an API call; on failure show why and return None. A 401 signs out."""
    try:
        return action()
    except AdminApiError as error:
        if error.kind is AdminErrorKind.SIGNED_OUT:
            sign_out(MESSAGES[error.kind])
            st.rerun()
        show_error(error)
        return None


def try_call(action: Callable[[], Any]) -> tuple[Any, AdminApiError | None]:
    """Like ``call`` but hands the error back (for conflicts the caller handles)."""
    try:
        return action(), None
    except AdminApiError as error:
        if error.kind is AdminErrorKind.SIGNED_OUT:
            sign_out(MESSAGES[error.kind])
            st.rerun()
        return None, error


def flash(message: str) -> None:
    """A confirmation to show once, after the rerun (call only after the backend
    confirmed the change)."""
    st.session_state[FLASH_KEY] = message


def show_flash() -> None:
    if message := st.session_state.pop(FLASH_KEY, None):
        st.success(message)


def when(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC") if value is not None else "-"


def opened(key: str, ident: object, load: Callable[[], Any]) -> Any:
    """The record the user opened, kept until it is deliberately reloaded.

    Forms are built from this copy, not re-read on every rerun: if someone else changes
    the record meanwhile, the user's submission still reaches the backend with the
    version (or status) it was based on, and is refused there with an explanation,
    instead of the form silently disappearing. ``forget`` reloads it.
    """
    stored = st.session_state.get(key)
    if stored is not None and stored[0] == ident:
        return stored[1]
    record = call(load)
    if record is not None:
        st.session_state[key] = (ident, record)
    return record


def forget(key: str) -> None:
    st.session_state.pop(key, None)


def form_generation(name: str) -> int:
    """A counter in a form's key: increasing it after a confirmed submit gives the user a
    fresh, empty form (and a stale double-click submits nothing)."""
    return int(st.session_state.get(f"{SESSION_PREFIX}form_{name}", 0))


def next_form_generation(name: str) -> None:
    st.session_state[f"{SESSION_PREFIX}form_{name}"] = form_generation(name) + 1
