"""Support ticket section of the staff dashboard (Stage 11, Tasks 12 and 13).

Lists tickets (status, type and reference filters, pagination), and for one ticket: the
customer's request and contact details, status changes along the Stage 10 workflow,
assignment to a configured staff member, the event history and internal notes.

What the page never does: change anything automatically (no ticket is resolved by
itself), claim that the customer or an assignee was notified (nothing sends email or
SMS), or claim an assignee has seen a ticket. Every change is a call to the protected
API, which checks the role, the workflow and that the ticket did not change meanwhile.
"""

import uuid

import streamlit as st
from pydantic import SecretStr

from frontend.admin_api_client import (
    MAX_NOTE_LENGTH,
    STAFF_NEXT_STATUSES,
    TICKET_STATUSES,
    AdminApiClient,
    TicketDetail,
    TicketEvent,
)
from frontend.admin_common import (
    SESSION_PREFIX,
    call,
    flash,
    forget,
    form_generation,
    next_form_generation,
    opened,
    show_error,
    show_flash,
    try_call,
    when,
)
from frontend.formatting import plain_markdown

PAGE_SIZE = 50
OPENED_KEY = f"{SESSION_PREFIX}opened_ticket"
UNASSIGNED = "Unassigned"
DEFAULT_STATUSES = ["open", "assigned", "in_progress", "waiting_for_customer"]
# Statuses set through the assignment form instead (they need or clear an assignee).
ASSIGNMENT_STATUSES = frozenset({"assigned", "open"})
NOT_NOTIFIED = (
    "Nobody is notified by this page: reply to the customer yourself, by their chosen "
    "contact method."
)


def render_tickets(client: AdminApiClient, token: SecretStr) -> None:
    st.header("Support tickets")
    st.caption("Requests customers sent from the chat. " + NOT_NOTIFIED)
    show_flash()
    status_col, type_col, reference_col, page_col = st.columns([4, 2, 3, 1])
    statuses = status_col.multiselect("Status", TICKET_STATUSES, default=DEFAULT_STATUSES)
    kind = type_col.selectbox("Type", ["All", "support", "sales"])
    reference = reference_col.text_input("Reference", placeholder="SUP-7K3M-9QX2")
    page = int(page_col.number_input("Page", min_value=1, value=1, step=1, key="ticket-page"))
    listing = call(
        lambda: client.list_tickets(
            token,
            statuses=[] if reference.strip() else statuses,  # a reference finds any status
            ticket_type=None if kind == "All" else kind,
            reference=reference.strip() or None,
            offset=(page - 1) * PAGE_SIZE,
            limit=PAGE_SIZE,
        )
    )
    if listing is None:
        return
    if not listing.tickets:
        st.info("No tickets match these filters.")
        return
    last = min(listing.offset + len(listing.tickets), listing.total)
    st.caption(f"Tickets {listing.offset + 1}-{last} of {listing.total}, oldest first.")
    st.dataframe(
        [
            {
                "Reference": t.reference_number,
                "Type": t.ticket_type,
                "Status": t.status,
                "Priority": t.priority,
                "Assigned to": t.assigned_to or "-",
                "Subject": t.subject,
                "Created": when(t.created_at),
            }
            for t in listing.tickets
        ],
        hide_index=True,
        width="stretch",
    )
    by_reference = {t.reference_number: t.ticket_id for t in listing.tickets}
    selected = st.selectbox("Open ticket", list(by_reference), key=f"{SESSION_PREFIX}ticket")
    if selected:
        ticket_id = by_reference[selected]
        ticket = opened(OPENED_KEY, ticket_id, lambda: client.get_ticket(token, ticket_id))
        if ticket is not None:
            render_ticket(client, token, ticket)


def literal(text: str) -> str:
    """Customer-written text as Markdown that shows it as typed: a subject or name such
    as ``[Verify your account](https://...)`` or ``![](https://...)`` must not become a
    link or a remote image (a tracking beacon) on the staff page."""
    return plain_markdown(text).rstrip()


def render_ticket(client: AdminApiClient, token: SecretStr, ticket: TicketDetail) -> None:
    st.subheader(f"{literal(ticket.reference_number)}: {literal(ticket.subject)}")
    if st.button("Reload ticket", key=f"{SESSION_PREFIX}reload_ticket"):
        forget(OPENED_KEY)
        st.rerun()
    lead = f" ({ticket.lead_category})" if ticket.lead_category else ""
    assignee = ticket.assigned_to or "nobody"
    since = f" since {when(ticket.assigned_at)}" if ticket.assigned_at else ""
    st.write(
        f"**Status:** {ticket.status} · **Type:** {ticket.ticket_type}{lead} · "
        f"**Assigned to:** {assignee}{since}"
    )
    st.text(ticket.description)
    if ticket.contact is not None:
        with st.expander("Contact details (personal data: use only to reply to this request)"):
            st.markdown(f"**Name:** {literal(ticket.contact.name)}")
            st.markdown(f"**Email:** {literal(ticket.contact.email)}")
            st.markdown(f"**Phone:** {literal(ticket.contact.phone or '-')}")
            st.write(f"**Preferred contact:** {ticket.contact.contact_preference}")
    else:
        st.caption("No contact details (removed when the ticket was anonymized).")
    status_tab, assign_tab, notes_tab, history_tab = st.tabs(
        ["Status", "Assignment", "Internal notes", "History"]
    )
    with status_tab:
        render_status_form(client, token, ticket)
    with assign_tab:
        render_assignment_form(client, token, ticket)
    with notes_tab:
        render_notes(client, token, ticket)
    with history_tab:
        events = call(lambda: client.ticket_events(token, ticket.ticket_id))
        if events:
            st.dataframe([event_row(e) for e in events], hide_index=True, width="stretch")


def event_row(event: TicketEvent) -> dict[str, object]:
    if event.event_type == "assignment_changed":
        change = (
            f"assignee {event.previous_assigned_to or 'nobody'} → {event.assigned_to or 'nobody'}"
        )
    elif event.event_type == "anonymized":
        change = "personal data removed"
    else:
        change = f"{event.previous_status or 'new'} → {event.new_status}"
        if event.assigned_to:
            change += f" ({event.assigned_to})"
    return {
        "#": event.sequence,
        "Change": change,
        "By": event.actor_id or event.actor_type,
        "When": when(event.created_at),
    }


def render_status_form(client: AdminApiClient, token: SecretStr, ticket: TicketDetail) -> None:
    choices = [
        s for s in STAFF_NEXT_STATUSES.get(ticket.status, ()) if s not in ASSIGNMENT_STATUSES
    ]
    if not choices:
        st.caption(
            "Closed tickets cannot change."
            if ticket.status == "closed"
            else "Use the Assignment tab to assign or unassign this ticket."
        )
        return
    with st.form(f"status-{ticket.ticket_id}-{ticket.status}"):
        new_status = st.selectbox("Change status to", choices)
        st.caption("Closing is final. " + NOT_NOTIFIED)
        submitted = st.form_submit_button("Change status", type="primary")
    if not submitted:
        return
    changed, error = try_call(
        lambda: client.change_ticket_status(
            token, ticket.ticket_id, new_status, expected_status=ticket.status
        )
    )
    if error is not None:
        show_error(error)
        forget(OPENED_KEY)  # the next run shows the ticket as it is now
        return
    forget(OPENED_KEY)
    flash(f"{changed.reference_number} is now {changed.status}.")
    st.rerun()


def render_assignment_form(client: AdminApiClient, token: SecretStr, ticket: TicketDetail) -> None:
    if ticket.status in ("resolved", "closed"):
        st.caption("Resolved and closed tickets keep their assignee.")
        return
    staff = call(lambda: client.staff_directory(token))
    if staff is None:
        return
    options = [UNASSIGNED, *(member.staff_id for member in staff)]
    current = ticket.assigned_to or UNASSIGNED
    with st.form(f"assign-{ticket.ticket_id}-{ticket.assigned_to}-{ticket.status}"):
        choice = st.selectbox(
            "Assigned to",
            options,
            index=options.index(current) if current in options else 0,
            help="Only configured staff who handle tickets are listed.",
        )
        st.caption(
            "Assigning records who owns the ticket. It does not notify them or mean they "
            "have seen it."
        )
        submitted = st.form_submit_button("Save assignment", type="primary")
    if not submitted:
        return
    wanted = None if choice == UNASSIGNED else choice
    if wanted == ticket.assigned_to:
        st.info("The assignment is unchanged.")
        return
    changed, error = try_call(
        lambda: client.change_assignment(
            token,
            ticket.ticket_id,
            wanted,
            expected_assigned_to=ticket.assigned_to,
            expected_status=ticket.status,
        )
    )
    if error is not None:
        show_error(error)
        forget(OPENED_KEY)  # the next run shows the ticket as it is now
        return
    who = changed.assigned_to or "nobody"
    forget(OPENED_KEY)
    flash(f"{changed.reference_number} is now assigned to {who} (status {changed.status}).")
    st.rerun()


def render_notes(client: AdminApiClient, token: SecretStr, ticket: TicketDetail) -> None:
    st.caption("Internal: visible to staff only, never to the customer.")
    notes = call(lambda: client.ticket_notes(token, ticket.ticket_id))
    for note in notes or ():
        st.markdown(f"**{note.author_id}** · {when(note.created_at)}")
        st.text(note.body)
    form = f"note_{ticket.ticket_id}"
    # One ID per note being written: a retry (same ID) never adds the note twice.
    id_key = f"{SESSION_PREFIX}note_id_{ticket.ticket_id}"
    client_note_id = st.session_state.setdefault(id_key, uuid.uuid4())
    with st.form(f"note-{ticket.ticket_id}-{form_generation(form)}"):
        body = st.text_area("Add a note", max_chars=MAX_NOTE_LENGTH, height=100)
        submitted = st.form_submit_button("Add note")
    if not submitted:
        return
    if not body.strip():
        st.error("Write the note first.")
        return
    note, error = try_call(
        lambda: client.add_ticket_note(token, ticket.ticket_id, body, client_note_id=client_note_id)
    )
    if error is not None:
        show_error(error)
        return
    del st.session_state[id_key]  # the next note gets a new ID
    next_form_generation(form)
    flash(f"Note added to {ticket.reference_number}.")
    st.rerun()
