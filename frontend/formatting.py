"""Text shown on the page: safe rendering of messages, and customer-facing wording.

Message text is never trusted as markup. Customers type it, and replies can be an LLM's
rewrite of an FAQ answer, so both are shown with ``plain_markdown``: every Markdown
character is escaped, so text renders exactly as written. A message cannot add links,
images (which would load a URL), headings, colours, LaTeX or HTML. Line breaks are kept.
A bare URL still becomes a link (GitHub-flavoured Markdown's autolinks), but its text is
always the URL itself, so a message cannot disguise where a link goes.

Error wording is chosen here from the ``ErrorKind``, never copied from exceptions, so no
internal detail reaches the customer. The backend's request ID is shown as a reference
for support.
"""

import re
from datetime import UTC, datetime

from frontend.api_client import Decision, ErrorKind
from frontend.state import ErrorNotice

# CommonMark's escapable ASCII punctuation, which also covers Streamlit's extensions
# (":red[...]" colours, ":material/icon:" and "$...$" LaTeX).
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>~:$&\"'=^])")


def plain_markdown(text: str) -> str:
    """Markdown that displays ``text`` literally, keeping its line breaks."""
    escaped = _MARKDOWN_SPECIAL.sub(r"\\\1", text.replace("\r\n", "\n").replace("\r", "\n"))
    # A hard line break per newline (two trailing spaces), blank lines kept as paragraphs.
    return "\n".join(line + "  " if line else "" for line in escaped.split("\n"))


GENERATION_METHOD_LABELS = {
    "approved_faq": "Answer from our help articles",
    "llm_grounded": "AI-written summary of our help articles",
    "clarification": "Clarifying question",
    "fallback": "Automatic message",
}


def generation_label(method: str | None) -> str | None:
    return GENERATION_METHOD_LABELS.get(method or "")


DECISION_HINTS = {
    Decision.NO_MATCH: "I couldn't find this in our help articles.",
    Decision.RETRIEVAL_UNAVAILABLE: "Our help articles can't be searched right now.",
    Decision.AMBIGUOUS: "Pick the question that matches yours, or rephrase it.",
}

_ERROR_TEXT: dict[ErrorKind, str] = {
    ErrorKind.BACKEND_UNREACHABLE: "We can't reach the support service right now. "
    "Please check back in a moment.",
    ErrorKind.TIMEOUT: "This is taking longer than expected. Your message may still be "
    "answered: try again to fetch the reply.",
    ErrorKind.NETWORK_ERROR: "The connection was interrupted. Please try again.",
    ErrorKind.UNAUTHORIZED: "Something went wrong with this conversation. Please start a new one.",
    ErrorKind.FORBIDDEN: "This request was refused. Please start a new conversation.",
    ErrorKind.SESSION_NOT_FOUND: "This conversation is no longer available. Please start a "
    "new one.",
    ErrorKind.SESSION_EXPIRED: "This conversation has expired. Please start a new one.",
    ErrorKind.SESSION_CLOSED: "This conversation has ended. Start a new one to keep chatting.",
    ErrorKind.BUSY: "Your previous message is still being answered. Please try again in a moment.",
    ErrorKind.INVALID_MESSAGE: "That message couldn't be sent. Please check it and try again.",
    ErrorKind.MESSAGE_CONFLICT: "That message couldn't be sent. Please send it again.",
    ErrorKind.RATE_LIMITED: "You're sending messages a little too quickly. Please wait a moment.",
    ErrorKind.SERVICE_UNAVAILABLE: "The support service is temporarily unavailable. Please "
    "try again shortly.",
    ErrorKind.SERVER_ERROR: "Something went wrong on our side. Please try again.",
    ErrorKind.CLIENT_ERROR: "Something went wrong. Please start a new conversation.",
    ErrorKind.INVALID_RESPONSE: "We received an unexpected reply. Please try again.",
}


def error_text(notice: ErrorNotice) -> str:
    """One or two sentences for the customer, without internal details."""
    if notice.kind is ErrorKind.INVALID_MESSAGE:
        types = notice.validation_types
        if any(kind.endswith("too_long") for kind in types):
            limit = f" (at most {notice.max_length:,} characters)" if notice.max_length else ""
            return f"Your message is too long{limit}. Please shorten it."
        if "blank_message" in types or "string_too_short" in types:
            return "Please type a message first."
        if "invalid_characters" in types:
            return "Your message contains characters that can't be sent. Please retype it."
    text = _ERROR_TEXT.get(notice.kind, _ERROR_TEXT[ErrorKind.SERVER_ERROR])
    if notice.retry_after and notice.kind in (ErrorKind.RATE_LIMITED, ErrorKind.BUSY):
        text += f" You can try again in about {round(notice.retry_after)} s."
    return text


def error_reference(notice: ErrorNotice) -> str | None:
    """A reference the customer can quote to support (the backend's request ID)."""
    return f"Reference: {notice.request_id}" if notice.request_id else None


def expiry_text(expires_at: datetime | None, now: datetime | None = None) -> str | None:
    """'Expires in 42 min' for an active conversation, or None."""
    if expires_at is None:
        return None
    minutes = int((expires_at - (now or datetime.now(UTC))).total_seconds() // 60)
    if minutes < 1:
        return "Expires in less than a minute"
    if minutes < 120:
        return f"Expires in {minutes} min"
    return f"Expires in {minutes // 60} h"
