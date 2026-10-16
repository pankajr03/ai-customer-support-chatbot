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
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime

from frontend.api_client import Decision, ErrorKind, SourceReference
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


# Notes under a reply, by the backend's decision. They describe what happened in plain
# words; the frontend never re-judges a reply (no scores or confidence figures).
DECISION_HINTS = {
    # The backend's no-match message already says what to do; this only adds a tip.
    Decision.NO_MATCH: "Not found in our help articles. Try rephrasing your question.",
    Decision.RETRIEVAL_UNAVAILABLE: "Our help articles are briefly unavailable. "
    "You can ask again in a moment.",
    Decision.AMBIGUOUS: "Choose the question closest to yours, or type a reply.",
}

SOURCES_LABEL = "Sources used"
SOURCES_NOTE = "The help articles this reply was based on."


def unique_sources(sources: Sequence[SourceReference]) -> list[SourceReference]:
    """The sources in the backend's order, each FAQ once."""
    seen: set[str] = set()
    unique = []
    for source in sources:
        if source.faq_id not in seen:
            seen.add(source.faq_id)
            unique.append(source)
    return unique


def source_labels(sources: Sequence[SourceReference]) -> list[tuple[str, str | None]]:
    """``(question, faq_id or None)`` per unique source. The ID is only included when
    two sources share a question title, so the customer can tell them apart."""
    unique = unique_sources(sources)
    titles = Counter(source.question.strip().casefold() for source in unique)
    return [
        (source.question, source.faq_id if titles[source.question.strip().casefold()] > 1 else None)
        for source in unique
    ]


# What to tell the customer, and what to do next. Each matches the control the page
# shows for that failure: "Try again" for failures where resending the same message is
# safe (it keeps its client_message_id, so it is never answered twice), "Start a new
# conversation" when the conversation can no longer be used, and the message back in
# the input when it simply needs sending again.
_ENDED = "Start a new conversation to continue; your message will be ready to send."
_ERROR_TEXT: dict[ErrorKind, str] = {
    ErrorKind.BACKEND_UNREACHABLE: "We can't reach the support service right now. "
    "Select Try again in a moment.",
    ErrorKind.TIMEOUT: "This is taking longer than expected. Select Try again to get the "
    "reply; your message won't be sent twice.",
    ErrorKind.NETWORK_ERROR: "The connection was interrupted. Select Try again; your "
    "message won't be sent twice.",
    ErrorKind.UNAUTHORIZED: f"This conversation can no longer be used. {_ENDED}",
    ErrorKind.FORBIDDEN: f"This conversation can no longer be used. {_ENDED}",
    ErrorKind.SESSION_NOT_FOUND: f"This conversation is no longer available. {_ENDED}",
    ErrorKind.SESSION_EXPIRED: f"This conversation has expired. {_ENDED}",
    ErrorKind.SESSION_CLOSED: f"This conversation has ended. {_ENDED}",
    ErrorKind.BUSY: "Your previous message is still being answered. Select Try again in a moment.",
    ErrorKind.INVALID_MESSAGE: "That message couldn't be sent. Please check it and send it again.",
    ErrorKind.MESSAGE_CONFLICT: "That message couldn't be sent. It's back in the message box: "
    "please send it again.",
    ErrorKind.RATE_LIMITED: "You're sending messages a little too quickly. Please wait a "
    "moment, then select Try again.",
    ErrorKind.SERVICE_UNAVAILABLE: "The support service is temporarily unavailable. Select "
    "Try again shortly.",
    ErrorKind.SERVER_ERROR: "Something went wrong on our side. Your message is back in the "
    "message box: please send it again.",
    ErrorKind.CLIENT_ERROR: "Something went wrong and your message couldn't be sent. Please "
    "try again later.",
    ErrorKind.INVALID_RESPONSE: "We received an unexpected reply. Select Try again; your "
    "message won't be sent twice.",
}

# Shown when the page itself fails unexpectedly (a bug): no details, a way forward.
UNEXPECTED_ERROR = (
    "Something went wrong on this page. Please reload it to continue; if you were in a "
    "conversation, it will start again."
)


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
    waits = (ErrorKind.RATE_LIMITED, ErrorKind.BUSY, ErrorKind.SERVICE_UNAVAILABLE)
    if notice.retry_after and notice.kind in waits:
        text += f" You can try again in about {max(1, round(notice.retry_after))} s."
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
