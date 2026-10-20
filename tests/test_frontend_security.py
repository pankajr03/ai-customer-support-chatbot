"""Stage 12 frontend audit (Task 8): static guards over both Streamlit apps.

Behaviour is tested elsewhere (customer page: ``tests/test_frontend_*.py``; staff page:
``tests/test_admin_frontend.py``, including session expiry). These tests read the code
of ``frontend/`` and fail when a change would:

- show a value in Markdown (``st.markdown``, ``st.write``, headings, captions, alerts)
  without escaping it, unless the value is known to be server-controlled (an ID, a
  count, an enum value, a timestamp, a fixed sentence) and listed below;
- cache visitor data in Streamlit's shared caches (``st.cache_data`` and
  ``st.cache_resource`` are shared by every browser session of the server);
- put anything into the page's URL (query parameters end up in history and logs).

Untrusted HTML is guarded by ``tests/test_frontend_styling.py``.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = sorted((ROOT / "frontend").rglob("*.py"))

# Streamlit calls that render their first argument as Markdown.
MARKDOWN_CALLS = frozenset(
    {
        "markdown", "write", "subheader", "header", "title", "caption", "info", "warning",
        "error", "success", "toast",
    }
)  # fmt: skip
# Wrapping a value in one of these escapes it (``literal`` is plain_markdown + rstrip).
ESCAPERS = frozenset({"plain_markdown", "literal"})
# Server-controlled values that may appear in Markdown unescaped, per file. Each is an
# ID or staff ID (restricted patterns), a number, an enum value, a timestamp from
# ``when``, a fixed sentence from this code base, or a setting the operator chose.
TRUSTED: dict[str, frozenset[str]] = {
    "admin_app.py": frozenset(
        {
            "identity.staff_id",
            "identity.role",
            "settings.base_url",
            "settings.backend_api_prefix",
            "identity.auth_notice",  # the backend's fixed notice
            "notice",  # a fixed sign-in notice from admin_common
            "'**Allowed:** ' + ', '.join((p.replace('_', ' ') for p in identity.permissions))",
        }
    ),
    "admin_audit.py": frozenset({"events.total"}),
    "admin_common.py": frozenset(
        {
            "field",  # a field path from the backend's 422
            "error_message(error)",  # fixed sentences, the backend's own, a request ID
            "message",  # a flash message this code base sets after a confirmed save
        }
    ),
    "admin_monitoring.py": frozenset(
        {
            "data.process.started_at",  # a datetime parsed by the response model
            "_duration(data.process.uptime_seconds)",  # formatted from a number
            "created",  # integer counts
            "duplicate",
            "integrations",
            "'enabled' if data.llm.enabled else 'disabled'",  # fixed words
            "', paused after recent failures' if data.llm.paused else ''",
        }
    ),
    "admin_faqs.py": frozenset(
        {
            "listing.offset + 1",
            "last",
            "listing.total",
            "faq.id",
            "status",
            "faq.version",
            "when(faq.created_at)",
            "faq.created_by or 'JSON import'",
            "when(faq.updated_at)",
            "faq.updated_by or 'JSON import'",
            "conflict['version']",
            "revision.version",
            "revision.action",
            "who",
            "when(revision.changed_at)",
            "len(history.revisions)",
            "history.total",
            "field",
            "before",  # ``before`` is escaped where it is built
            "error_message(error)",
            "problem",  # check_fields: fixed validation sentences
            "'No FAQs match these filters.' if listing.total or page == 1 else 'No more pages.'",
        }
    ),  # fmt: skip
    "admin_insights.py": frozenset(
        {
            "when(overview.generated_at)",
            "tickets.created_last_7_days",
            "tickets.open_queue",
            "window",
            "data.since.date().isoformat()",
            "data.min_sample",
            "latency.definition",
            "latency.samples",
            "limitation",
            "hours.definition",  # the backend's fixed metric definitions
        }
    ),  # fmt: skip
    "admin_tickets.py": frozenset(
        {
            "listing.offset + 1",
            "last",
            "listing.total",
            "ticket.status",
            "ticket.ticket_type",
            "lead",
            "assignee",
            "since",
            "note.author_id",
            "when(note.created_at)",
            "ticket.contact.contact_preference",
            "'Requests customers sent from the chat. ' + NOT_NOTIFIED",
            "'Closing is final. ' + NOT_NOTIFIED",
            "'Closed tickets cannot change.' if ticket.status == 'closed' else "
            "'Use the Assignment tab to assign or unassign this ticket.'",
        }
    ),  # fmt: skip
    "app.py": frozenset({"message"}),  # a fixed start-up error sentence
    "chat.py": frozenset(
        {
            "DEMO_NOTICE",
            "mode",
            "state.history_omitted",
            "caption",  # generation labels, decision hints, a request ID
            "error_text(notice)",  # fixed error sentences
            "reference",  # "Reference: <request ID>"
        }
    ),
    "sidebar.py": frozenset({"expiry", "note"}),  # a formatted time; a fixed badge note
    "sources.py": frozenset({"'\\n'.join(lines)"}),  # lines built with plain_markdown
    "support.py": frozenset(
        {
            "problems[name]",
            "problems['consent']",
            "LEAD_CATEGORIES.get(ticket.lead_category, '')",
            "text",  # OFFER_TEXT: fixed offer sentences
            "FORM_TITLES[ticket_type]",
            "ticket.status_message",  # the backend's fixed status explanation
            "ticket.message",  # the backend's fixed confirmation sentence
            "support_error_text(support.last_error)",
            "support_error_text(status_error)",
            "reference",
        }
    ),
}


def _escaped(value: ast.expr) -> bool:
    """``plain_markdown(x)``, ``literal(x)`` or ``plain_markdown(x).rstrip()``."""
    if (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Attribute)
        and value.func.attr == "rstrip"
    ):
        value = value.func.value
    return isinstance(value, ast.Call) and getattr(value.func, "id", None) in ESCAPERS


def _markdown_values(path: Path) -> list[tuple[int, str]]:
    """Every value a Markdown-rendering call interpolates or receives as a variable,
    except escaped ones and upper-case constants (fixed text in this code base)."""
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in MARKDOWN_CALLS
            and node.args
        ):
            continue
        argument = node.args[0]
        if isinstance(argument, ast.JoinedStr):
            values = [
                part.value for part in argument.values if isinstance(part, ast.FormattedValue)
            ]
        elif isinstance(argument, ast.Constant):
            values = []
        else:
            values = [argument]
        for value in values:
            if _escaped(value):
                continue
            if isinstance(value, ast.Name) and value.id.isupper():
                continue
            found.append((node.lineno, ast.unparse(value)))
    return found


def test_markdown_shows_only_escaped_or_server_controlled_values() -> None:
    """A new unescaped value fails here: wrap it in ``plain_markdown`` (customer or
    staff text) or, if it is server-controlled, add it to ``TRUSTED`` with a reason."""
    unexpected = [
        f"{path.relative_to(ROOT).as_posix()}:{line}: {value}"
        for path in FRONTEND
        for line, value in _markdown_values(path)
        if value not in TRUSTED.get(path.name, frozenset())
    ]
    assert unexpected == []


def test_customer_and_ticket_text_is_escaped_on_both_pages() -> None:
    tickets = (ROOT / "frontend" / "admin_tickets.py").read_text(encoding="utf-8")
    for value in ("ticket.subject", "ticket.contact.name", "ticket.contact.email"):
        assert f"literal({value}" in tickets, value
    assert "st.text(ticket.description)" in tickets and "st.text(note.body)" in tickets


def _decorated(path: Path, decorator: str) -> list[ast.FunctionDef]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and any(decorator in ast.unparse(d) for d in node.decorator_list)
    ]


def test_shared_caches_hold_no_visitor_data() -> None:
    """Only the token-less API clients and the backend's readiness (the same for every
    visitor) are cached across sessions; no cached function takes a token or IDs."""
    cached = {
        (path.name, fn.name, tuple(a.arg for a in fn.args.args))
        for path in FRONTEND
        for decorator in ("cache_data", "cache_resource")
        for fn in _decorated(path, decorator)
    }
    assert cached == {
        ("resources.py", "get_api_client", ()),
        ("resources.py", "answer_mode", ("_client",)),
        ("resources.py", "backend_status", ("_client",)),
        ("admin_resources.py", "get_admin_client", ()),
    }


def test_nothing_is_written_to_the_page_url() -> None:
    for path in FRONTEND:
        code = path.read_text(encoding="utf-8")
        for forbidden in ("query_params[", "query_params.update", "query_params.from_dict"):
            assert forbidden not in code, f"{path.name}: {forbidden}"
        assert "experimental_set_query_params" not in code, path.name


def test_tokens_live_only_in_per_session_state_as_secrets() -> None:
    admin_app = (ROOT / "frontend" / "admin_app.py").read_text(encoding="utf-8")
    assert 'type="password"' in admin_app  # the token field is masked
    assert "st.session_state[TOKEN_KEY] = token" in admin_app
    client = (ROOT / "frontend" / "admin_api_client.py").read_text(encoding="utf-8")
    assert "def staff_token(value: str) -> SecretStr | None" in client
