"""Streamlit staff dashboard (Stage 11). From the project root, with the backend running:

    streamlit run frontend/admin_app.py --server.port 8502

A separate page from the customer chat (``frontend/app.py``), which has no staff
controls and never calls a staff endpoint. Staff sign in with their personal staff token
(``python -m scripts.create_staff_token <id> --role admin|support``).

Sections, shown according to the role the backend reports (``GET /admin/me``):

- **Dashboard overview** and **Analytics** (admin): counts of FAQs and tickets.
- **FAQ management** (admin): search, filter, create, edit, deactivate and reactivate.
- **Support tickets** (admin, support): queue, details with contact data, status changes.
- **Administration information** (everyone): who is signed in, what the role allows,
  how staff access works.

Hiding a section is only a convenience: the backend checks the token and role on every
request, so a support member who reached an admin call anyway gets ``403``.

Security:

- The token is typed into a password field, checked with ``GET /admin/me`` and kept in
  ``st.session_state`` (server memory for this browser session) as a ``SecretStr``. It
  is never put in the URL, a cookie, browser storage, a log line or an error message,
  and signing out (or a ``401``) removes it. Reloading the browser tab starts a new
  session, which must sign in again.
- Staff data is never cached in Streamlit's shared caches (see ``admin_resources``).
- Edits name the version (FAQs) or status (tickets) they were based on, so a change
  someone else made meanwhile is reported instead of overwritten.
"""

import logging
import sys
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

# `streamlit run frontend/admin_app.py` puts frontend/ on sys.path, not the project root.
_PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import streamlit as st  # noqa: E402
from pydantic import SecretStr  # noqa: E402

from frontend import admin_resources  # noqa: E402
from frontend.admin_api_client import (  # noqa: E402
    FAQ_CATEGORIES,
    MAX_ANSWER_LENGTH,
    MAX_QUESTION_LENGTH,
    STAFF_NEXT_STATUSES,
    TICKET_STATUSES,
    AdminApiClient,
    AdminApiError,
    AdminErrorKind,
    FAQRecord,
    StaffIdentity,
    parse_keywords,
    staff_token,
)

logger = logging.getLogger(__name__)

TOKEN_KEY = "admin_staff_token"  # noqa: S105 - a session-state key, not a secret
IDENTITY_KEY = "admin_identity"
NOTICE_KEY = "admin_notice"
SELECTED_FAQ_KEY = "admin_selected_faq"
FAQ_PAGE_SIZE = 20
TICKET_PAGE_SIZE = 50

SECTION_OVERVIEW = "Dashboard overview"
SECTION_FAQS = "FAQ management"
SECTION_TICKETS = "Support tickets"
SECTION_ANALYTICS = "Analytics"
SECTION_INFO = "Administration information"
# Section -> the permission it needs (None: every staff member).
SECTIONS: dict[str, str | None] = {
    SECTION_OVERVIEW: "view_analytics",
    SECTION_FAQS: "manage_faqs",
    SECTION_TICKETS: "manage_tickets",
    SECTION_ANALYTICS: "view_analytics",
    SECTION_INFO: None,
}

MESSAGES: dict[AdminErrorKind, str] = {
    AdminErrorKind.UNREACHABLE: "The backend cannot be reached. Check that the API is running.",
    AdminErrorKind.TIMEOUT: "The backend did not answer in time. Please try again.",
    AdminErrorKind.SIGNED_OUT: "Your staff token is not valid (any more). Please sign in again.",
    AdminErrorKind.STAFF_DISABLED: "Staff access is disabled on this server: no staff tokens "
    "are configured (STAFF_TOKEN_HASHES).",
    AdminErrorKind.FORBIDDEN: "Your staff role does not allow this.",
    AdminErrorKind.NOT_FOUND: "Not found. It may have been removed or the ID is wrong.",
    AdminErrorKind.CONFLICT: "This was changed by someone else meanwhile, or the change is "
    "not allowed from its current state. Reload and try again.",
    AdminErrorKind.INVALID: "Some fields are not valid.",
    AdminErrorKind.UNAVAILABLE: "This is temporarily unavailable. Please try again shortly.",
    AdminErrorKind.SERVER_ERROR: "Something went wrong on the server.",
    AdminErrorKind.INVALID_RESPONSE: "The backend sent an unexpected response. Check that the "
    "dashboard and the API are the same version.",
}


# --- Session --------------------------------------------------------------------------


def current_token() -> SecretStr | None:
    return st.session_state.get(TOKEN_KEY)


def current_identity() -> StaffIdentity | None:
    return st.session_state.get(IDENTITY_KEY)


def sign_out(notice: str | None = None) -> None:
    for key in (TOKEN_KEY, IDENTITY_KEY, SELECTED_FAQ_KEY):
        st.session_state.pop(key, None)
    if notice:
        st.session_state[NOTICE_KEY] = notice


def show_error(error: AdminApiError) -> None:
    """The backend's safe message (or ours), the request ID and invalid fields."""
    message = MESSAGES[error.kind]
    if error.kind in (AdminErrorKind.CONFLICT, AdminErrorKind.INVALID) and error.message:
        message = error.message
    reference = f" (request ID {error.request_id})" if error.request_id else ""
    st.error(message + reference)
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


def _when(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC") if value is not None else "-"


# --- Sign-in --------------------------------------------------------------------------


def render_sign_in(client: AdminApiClient) -> None:
    st.title("Support admin")
    st.caption("Staff only. The customer chat is a separate page.")
    if notice := st.session_state.pop(NOTICE_KEY, None):
        st.info(notice)
    with st.form("sign-in", clear_on_submit=True):
        typed = st.text_input(
            "Staff token",
            type="password",
            help="Your personal token (stf_...). It is checked by the server and never "
            "shown, stored in the browser or put in the address bar.",
        )
        submitted = st.form_submit_button("Sign in", type="primary")
    if not submitted:
        return
    token = staff_token(typed)
    if token is None:
        st.error("Enter your staff token.")
        return
    try:
        identity = client.who_am_i(token)
    except AdminApiError as error:
        show_error(error)
        return
    st.session_state[TOKEN_KEY] = token
    st.session_state[IDENTITY_KEY] = identity
    logger.info("Staff signed in to the dashboard (role %s)", identity.role)
    st.rerun()


# --- Sections -------------------------------------------------------------------------


def render_overview(client: AdminApiClient, token: SecretStr) -> None:
    st.header(SECTION_OVERVIEW)
    overview = call(lambda: client.overview(token))
    if overview is None:
        return
    tickets = overview.tickets
    columns = st.columns(4)
    columns[0].metric("Tickets waiting on staff", tickets.open_queue)
    columns[1].metric("Waiting for customer", tickets.by_status.get("waiting_for_customer", 0))
    columns[2].metric("New in the last 7 days", tickets.created_last_7_days)
    columns[3].metric("Tickets in total", tickets.total)
    if overview.faqs is not None:
        faqs = overview.faqs
        columns = st.columns(4)
        columns[0].metric("Active FAQs", faqs.active)
        columns[1].metric("Inactive FAQs", faqs.inactive)
        columns[2].metric("Categories in use", len(faqs.by_category))
        columns[3].metric("FAQs last changed", _when(faqs.last_updated_at))
    else:
        st.info("FAQs on this server are not stored in the database, so they cannot be managed.")
    st.caption(f"As of {_when(overview.generated_at)}.")


def render_analytics(client: AdminApiClient, token: SecretStr) -> None:
    st.header(SECTION_ANALYTICS)
    st.caption(
        "Counts from the database at this moment. Conversation and answer-quality "
        "analytics are not collected yet."
    )
    overview = call(lambda: client.overview(token))
    if overview is None:
        return
    tickets = overview.tickets
    left, right = st.columns(2)
    with left:
        st.subheader("Tickets by status")
        st.bar_chart({"tickets": tickets.by_status}, horizontal=True)
        st.subheader("Tickets by type")
        st.bar_chart({"tickets": tickets.by_type}, horizontal=True)
    with right:
        st.subheader("Sales leads by category")
        st.bar_chart({"leads": tickets.by_lead_category}, horizontal=True)
        if overview.faqs is not None:
            st.subheader("FAQs by category")
            st.bar_chart(
                {
                    "active": {c.category: c.active for c in overview.faqs.by_category},
                    "inactive": {c.category: c.inactive for c in overview.faqs.by_category},
                },
                horizontal=True,
                stack=True,
            )


def render_faqs(client: AdminApiClient, token: SecretStr) -> None:
    st.header(SECTION_FAQS)
    st.caption(
        "Active FAQs are what the assistant answers with. Deactivated FAQs are kept (never "
        "deleted) and can be activated again."
    )
    with st.expander("Add a new FAQ"):
        render_new_faq_form(client, token)

    search_col, category_col, status_col = st.columns([3, 2, 2])
    search = search_col.text_input("Search", placeholder="Text, keyword or faq_001")
    category = category_col.selectbox("Category", ["All", *FAQ_CATEGORIES])
    state = status_col.selectbox("Status", ["All", "Active", "Inactive"])
    page_number = st.number_input("Page", min_value=1, value=1, step=1)
    listing = call(
        lambda: client.list_faqs(
            token,
            search=search.strip() or None,
            category=None if category == "All" else category,
            is_active=None if state == "All" else state == "Active",
            offset=(int(page_number) - 1) * FAQ_PAGE_SIZE,
            limit=FAQ_PAGE_SIZE,
        )
    )
    if listing is None:
        return
    st.caption(f"{listing.total} matching FAQ(s).")
    if not listing.faqs:
        st.info("No FAQs match.")
        return
    st.dataframe(
        [
            {
                "ID": faq.id,
                "Question": faq.question,
                "Category": faq.category,
                "Active": faq.is_active,
                "Version": faq.version,
                "Updated": _when(faq.updated_at),
                "By": faq.updated_by or "import",
            }
            for faq in listing.faqs
        ],
        hide_index=True,
        width="stretch",
    )
    ids = [faq.id for faq in listing.faqs]
    selected = st.selectbox("Open FAQ", ids, key=SELECTED_FAQ_KEY)
    if selected:
        faq = call(lambda: client.get_faq(token, selected))
        if faq is not None:
            render_faq_editor(client, token, faq)


def render_new_faq_form(client: AdminApiClient, token: SecretStr) -> None:
    with st.form("new-faq", clear_on_submit=False):
        question = st.text_input("Question", max_chars=MAX_QUESTION_LENGTH)
        answer = st.text_area("Answer", max_chars=MAX_ANSWER_LENGTH, height=150)
        category = st.selectbox("Category", FAQ_CATEGORIES)
        keywords = st.text_input(
            "Tags (search keywords, comma-separated)", placeholder="track, order status"
        )
        active = st.checkbox("Active (customers can be answered with it right away)", True)
        submitted = st.form_submit_button("Create FAQ", type="primary")
    if not submitted:
        return
    if not question.strip() or not answer.strip() or not parse_keywords(keywords):
        st.error("Question, answer and at least one tag are required.")
        return
    created = call(
        lambda: client.create_faq(
            token,
            question=question,
            answer=answer,
            category=category,
            keywords=parse_keywords(keywords),
            is_active=active,
        )
    )
    if created is not None:
        st.success(f"Created {created.id}.")


def render_faq_editor(client: AdminApiClient, token: SecretStr, faq: FAQRecord) -> None:
    st.subheader(f"{faq.id} ({'active' if faq.is_active else 'inactive'}, version {faq.version})")
    st.caption(
        f"Created {_when(faq.created_at)} by {faq.created_by or 'JSON import'}; last changed "
        f"{_when(faq.updated_at)} by {faq.updated_by or 'JSON import'}."
    )
    # Keys include the version, so a reloaded FAQ shows its new content.
    prefix = f"faq-{faq.id}-{faq.version}"
    with st.form(f"{prefix}-edit"):
        question = st.text_input("Question", faq.question, max_chars=MAX_QUESTION_LENGTH)
        answer = st.text_area("Answer", faq.answer, max_chars=MAX_ANSWER_LENGTH, height=200)
        category = st.selectbox(
            "Category", FAQ_CATEGORIES, index=FAQ_CATEGORIES.index(faq.category)
        )
        keywords = st.text_input("Tags (comma-separated)", ", ".join(faq.keywords))
        saved = st.form_submit_button("Save changes", type="primary")
    if saved:
        changes = _faq_changes(faq, question, answer, category, parse_keywords(keywords))
        if not changes:
            st.info("Nothing changed.")
        else:
            updated = call(lambda: client.update_faq(token, faq.id, faq.version, **changes))
            if updated is not None:
                st.success(f"Saved {updated.id} (version {updated.version}).")
                st.rerun()
    label = "Deactivate" if faq.is_active else "Activate"
    if st.button(f"{label} {faq.id}", key=f"{prefix}-toggle"):
        changed = call(
            lambda: client.set_faq_active(token, faq.id, faq.version, active=not faq.is_active)
        )
        if changed is not None:
            st.rerun()


def _faq_changes(
    faq: FAQRecord, question: str, answer: str, category: str, keywords: list[str]
) -> dict[str, Any]:
    """Only the fields that differ from the loaded version."""
    changes: dict[str, Any] = {}
    if question.strip() != faq.question:
        changes["question"] = question
    if answer.strip() != faq.answer:
        changes["answer"] = answer
    if category != faq.category:
        changes["category"] = category
    if [k.lower() for k in keywords] != list(faq.keywords):
        changes["keywords"] = keywords
    return changes


def render_tickets(client: AdminApiClient, token: SecretStr) -> None:
    st.header(SECTION_TICKETS)
    st.caption(
        "Requests customers sent from the chat. Replies go by the customer's chosen "
        "contact method; nothing here messages the customer."
    )
    status_col, type_col = st.columns([3, 1])
    statuses = status_col.multiselect(
        "Status", TICKET_STATUSES, default=["open", "assigned", "in_progress"]
    )
    kind = type_col.selectbox("Type", ["All", "support", "sales"])
    page_number = st.number_input("Page", min_value=1, value=1, step=1, key="ticket-page")
    listing = call(
        lambda: client.list_tickets(
            token,
            statuses=statuses,
            ticket_type=None if kind == "All" else kind,
            offset=(int(page_number) - 1) * TICKET_PAGE_SIZE,
            limit=TICKET_PAGE_SIZE,
        )
    )
    if listing is None:
        return
    st.caption(f"{listing.total} matching ticket(s), oldest first.")
    if not listing.tickets:
        st.info("No tickets match.")
        return
    st.dataframe(
        [
            {
                "Reference": t.reference_number,
                "Type": t.ticket_type,
                "Status": t.status,
                "Priority": t.priority,
                "Assigned to": t.assigned_to or "-",
                "Subject": t.subject,
                "Created": _when(t.created_at),
            }
            for t in listing.tickets
        ],
        hide_index=True,
        width="stretch",
    )
    by_reference = {t.reference_number: t.ticket_id for t in listing.tickets}
    reference = st.selectbox("Open ticket", list(by_reference))
    if reference:
        render_ticket(client, token, by_reference[reference])


def render_ticket(client: AdminApiClient, token: SecretStr, ticket_id: uuid.UUID) -> None:
    ticket = call(lambda: client.get_ticket(token, ticket_id))
    if ticket is None:
        return
    st.subheader(f"{ticket.reference_number}: {ticket.subject}")
    st.write(
        f"**Status:** {ticket.status} · **Type:** {ticket.ticket_type}"
        + (f" ({ticket.lead_category})" if ticket.lead_category else "")
        + f" · **Assigned to:** {ticket.assigned_to or '-'}"
    )
    st.text(ticket.description)
    if ticket.contact is not None:
        with st.expander("Contact details (personal data: use only to reply)"):
            st.write(f"**Name:** {ticket.contact.name}")
            st.write(f"**Email:** {ticket.contact.email}")
            st.write(f"**Phone:** {ticket.contact.phone or '-'}")
            st.write(f"**Preferred contact:** {ticket.contact.contact_preference}")
    else:
        st.caption("Contact details were removed (anonymized).")
    events = call(lambda: client.ticket_events(token, ticket_id))
    if events:
        st.dataframe(
            [
                {
                    "#": e.sequence,
                    "Change": f"{e.previous_status or '-'} → {e.new_status or '-'}",
                    "By": e.actor_id or e.actor_type,
                    "Assigned to": e.assigned_to or "",
                    "When": _when(e.created_at),
                }
                for e in events
            ],
            hide_index=True,
            width="stretch",
        )
    next_statuses = STAFF_NEXT_STATUSES.get(ticket.status, ())
    if not next_statuses:
        st.caption("Closed tickets cannot change.")
        return
    with st.form(f"ticket-{ticket.ticket_id}-{ticket.status}"):
        new_status = st.selectbox("Change status to", next_statuses)
        assignee = st.text_input("Assign to (staff ID, only when assigning)")
        submitted = st.form_submit_button("Change status")
    if submitted:
        changed = call(
            lambda: client.change_ticket_status(
                token,
                ticket.ticket_id,
                new_status,
                expected_status=ticket.status,
                assigned_to=assignee.strip() or None if new_status == "assigned" else None,
            )
        )
        if changed is not None:
            st.success(f"{changed.reference_number} is now {changed.status}.")
            st.rerun()


def render_info(identity: StaffIdentity) -> None:
    settings = admin_resources.get_settings()
    st.header(SECTION_INFO)
    st.write(f"**Signed in as:** {identity.staff_id}")
    st.write(f"**Role:** {identity.role}")
    st.write("**Allowed:** " + ", ".join(p.replace("_", " ") for p in identity.permissions))
    st.write(f"**Backend:** {settings.base_url}{settings.backend_api_prefix}")
    st.warning(identity.auth_notice)
    st.markdown(
        "- **Roles.** *admin*: FAQ management, tickets, analytics. *support*: tickets only.\n"
        "- **New staff token:** `python -m scripts.create_staff_token <id> --role "
        "admin|support`, add the printed entry to `STAFF_TOKEN_HASHES`, restart the API.\n"
        "- **Revoke:** remove the entry and restart the API.\n"
        "- **FAQ import:** `python -m scripts.import_faqs --backup` adds FAQs from "
        "`data/faqs.json` that are not in the database yet; it never overwrites edits.\n"
        "- **Enforcement:** the API checks your token and role on every request; this page "
        "only hides what your role cannot use."
    )


# --- Page -----------------------------------------------------------------------------


def main() -> None:
    st.set_page_config(page_title="Support admin", page_icon="🛠️", layout="wide")
    client = admin_resources.get_admin_client()
    token, identity = current_token(), current_identity()
    if token is None or identity is None:
        render_sign_in(client)
        return
    allowed = [name for name, needs in SECTIONS.items() if needs is None or identity.can(needs)]
    with st.sidebar:
        st.write(f"Signed in as **{identity.staff_id}** ({identity.role})")
        section = st.radio("Section", allowed)
        if st.button("Sign out"):
            sign_out("You are signed out.")
            st.rerun()
        st.caption("Prototype staff access, see Administration information.")
    if section == SECTION_OVERVIEW:
        render_overview(client, token)
    elif section == SECTION_FAQS:
        render_faqs(client, token)
    elif section == SECTION_TICKETS:
        render_tickets(client, token)
    elif section == SECTION_ANALYTICS:
        render_analytics(client, token)
    else:
        render_info(identity)


main()
