"""Audit log section of the staff dashboard (Stage 11, Task 17): read only, admins.

Shows the administrative audit log from ``GET /admin/audit``: who (staff ID, a named
service such as ``faq-import``, or "unauthenticated"), what, on which resource, the
outcome and the request ID to find the matching server log lines. There is nothing to
edit: the API has no way to change or delete an event, and the database refuses it.
"""

import streamlit as st
from pydantic import SecretStr

from frontend.admin_api_client import AUDIT_ACTIONS, AdminApiClient
from frontend.admin_common import call, when

PAGE_SIZE = 50


def render_audit(client: AdminApiClient, token: SecretStr) -> None:
    st.header("Audit log")
    st.caption(
        "Administrative actions and refused staff access, newest first. Read only. "
        "No tokens, customer details or FAQ/ticket text are recorded."
    )
    action_col, outcome_col, actor_col, page_col = st.columns([3, 2, 2, 1])
    action = action_col.selectbox("Action", ["All", *AUDIT_ACTIONS])
    outcome = outcome_col.selectbox("Outcome", ["All", "success", "denied"])
    actor = actor_col.text_input("Staff ID", placeholder="alex")
    page = int(page_col.number_input("Page", min_value=1, value=1, step=1, key="audit-page"))
    events = call(
        lambda: client.audit_events(
            token,
            action=None if action == "All" else action,
            outcome=None if outcome == "All" else outcome,
            actor_id=actor.strip() or None,
            offset=(page - 1) * PAGE_SIZE,
            limit=PAGE_SIZE,
        )
    )
    if events is None:
        return
    if not events.events:
        st.info("No audit events match.")
        return
    st.caption(f"{events.total} matching event(s).")
    st.dataframe(
        [
            {
                "When": when(e.created_at),
                "Actor": e.actor_id or e.actor_type,
                "Action": e.action,
                "Resource": f"{e.resource_type} {e.resource_id or ''}".strip(),
                "Outcome": e.outcome,
                "Details": ", ".join(f"{k}={v}" for k, v in e.details.items()),
                "Request ID": e.request_id or "",
            }
            for e in events.events
        ],
        hide_index=True,
        width="stretch",
    )
