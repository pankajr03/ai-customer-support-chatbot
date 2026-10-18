"""Streamlit staff dashboard (Stage 11). From the project root, with the backend running:

    streamlit run frontend/admin_app.py --server.port 8502

A separate page from the customer chat (``frontend/app.py``), which has no staff
controls and never calls a staff endpoint. Staff sign in with their personal staff token
(``python -m scripts.create_staff_token <id> --role admin|support``).

Sections, shown according to the role the backend reports (``GET /admin/me``):

- **Dashboard overview** and **Analytics** (admin): live counts and chatbot decision
  analytics (``frontend.admin_insights``).
- **FAQ management** (admin): search, filter, create, edit, (de)activate, history
  (``frontend.admin_faqs``).
- **Support tickets** (admin, support): queue, details with contact data, status,
  assignment, internal notes, history (``frontend.admin_tickets``).
- **Audit log** (admin): administrative actions and refused access, read only
  (``frontend.admin_audit``).
- **Administration information** (everyone): who is signed in, what the role allows,
  how staff access works.

Hiding a section is only a convenience: the backend checks the token and role on every
request, so a support member who reached an admin call anyway gets ``403``.

Security:

- The token is typed into a password field, checked with ``GET /admin/me`` and kept in
  ``st.session_state`` (server memory for this browser session) as a ``SecretStr``. It
  is never put in the URL, a cookie, browser storage, a log line or an error message,
  and signing out (or a ``401``) removes it with all the page's state. Reloading the
  browser tab starts a new session, which must sign in again.
- Staff data is never cached in Streamlit's shared caches (see ``admin_resources``).
- All data comes through the protected API (``AdminApiClient``); the page has no
  database access.
"""

import logging
import sys
from pathlib import Path

# `streamlit run frontend/admin_app.py` puts frontend/ on sys.path, not the project root.
_PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import streamlit as st  # noqa: E402

from frontend import admin_resources  # noqa: E402
from frontend.admin_api_client import (  # noqa: E402
    AdminApiClient,
    AdminApiError,
    StaffIdentity,
    staff_token,
)
from frontend.admin_audit import render_audit  # noqa: E402
from frontend.admin_common import (  # noqa: E402
    IDENTITY_KEY,
    NOTICE_KEY,
    TOKEN_KEY,
    current_identity,
    current_token,
    show_error,
    sign_out,
)
from frontend.admin_faqs import render_faqs  # noqa: E402
from frontend.admin_insights import render_analytics, render_overview  # noqa: E402
from frontend.admin_tickets import render_tickets  # noqa: E402

logger = logging.getLogger(__name__)

SECTION_OVERVIEW = "Dashboard overview"
SECTION_FAQS = "FAQ management"
SECTION_TICKETS = "Support tickets"
SECTION_ANALYTICS = "Analytics"
SECTION_AUDIT = "Audit log"
SECTION_INFO = "Administration information"
# Section -> the permission it needs (None: every staff member).
SECTIONS: dict[str, str | None] = {
    SECTION_OVERVIEW: "view_analytics",
    SECTION_FAQS: "manage_faqs",
    SECTION_TICKETS: "manage_tickets",
    SECTION_ANALYTICS: "view_analytics",
    SECTION_AUDIT: "view_audit_log",
    SECTION_INFO: None,
}


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
        "- **Notifications:** none are sent. Ticket changes and assignments are recorded "
        "only; reply to customers yourself.\n"
        "- **Enforcement:** the API checks your token and role on every request; this page "
        "only hides what your role cannot use."
    )


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
    elif section == SECTION_AUDIT:
        render_audit(client, token)
    else:
        render_info(identity)


main()
