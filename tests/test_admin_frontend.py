"""The staff dashboard (Stage 11, Task 2): its API client and the Streamlit page.

The client is pointed at the real backend in-process (``make_admin_app``), so the
contract, roles and errors are the backend's own. The page runs headless with
``AppTest`` (see tests/test_frontend_page.py for what AppTest cannot show).
"""

import ast
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import BaseModel, SecretStr
from streamlit.testing.v1 import AppTest

from app.models.admin_api import AdminOverview, FAQAdminList, StaffIdentity
from app.models.faq import (
    MAX_ANSWER_LENGTH,
    MAX_KEYWORD_LENGTH,
    MAX_KEYWORDS,
    MAX_QUESTION_LENGTH,
    FAQCategory,
)
from app.models.faq_admin import FAQRevision, FAQRevisionPage, ManagedFAQ
from app.models.support import ALLOWED_TRANSITIONS, TicketStatus
from app.models.support_api import StaffTicketDetail, StaffTicketList, TicketEventView
from frontend import admin_api_client as fe
from frontend import admin_resources
from frontend.config import FrontendSettings
from tests.faq_test_data import TEST_FAQ_RECORDS
from tests.test_admin_api import ALEX, MARIA, NEW_FAQ, STAFF_HASHES, AdminApp, make_admin_app  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
ADMIN_PAGE = ROOT / "frontend" / "admin_app.py"
CUSTOMER_PAGE = ROOT / "frontend" / "app.py"


# --- Contract ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("frontend_model", "backend_model"),
    [
        (fe.StaffIdentity, StaffIdentity),
        (fe.FAQRecord, ManagedFAQ),
        (fe.FAQList, FAQAdminList),
        (fe.Overview, AdminOverview),
        (fe.TicketList, StaffTicketList),
        (fe.TicketDetail, StaffTicketDetail),
        (fe.TicketEvent, TicketEventView),
        (fe.FAQRevision, FAQRevision),
        (fe.FAQRevisionList, FAQRevisionPage),
    ],
)
def test_the_client_reads_only_fields_the_backend_sends(
    frontend_model: type[BaseModel], backend_model: type[BaseModel]
) -> None:
    assert set(frontend_model.model_fields) <= set(backend_model.model_fields)


def test_mirrored_rules_match_the_backend() -> None:
    assert fe.FAQ_CATEGORIES == tuple(category.value for category in FAQCategory)
    assert fe.TICKET_STATUSES == tuple(status.value for status in TicketStatus)
    assert {k: set(v) for k, v in fe.STAFF_NEXT_STATUSES.items()} == {
        k.value: {s.value for s in v} for k, v in ALLOWED_TRANSITIONS.items()
    }
    assert (fe.MAX_QUESTION_LENGTH, fe.MAX_ANSWER_LENGTH) == (
        MAX_QUESTION_LENGTH,
        MAX_ANSWER_LENGTH,
    )
    assert (fe.MAX_KEYWORDS, fe.MAX_KEYWORD_LENGTH) == (MAX_KEYWORDS, MAX_KEYWORD_LENGTH)


# --- Client against the real backend ----------------------------------------------------


@pytest.fixture
def backend(make_admin_app: Callable[..., AdminApp]) -> tuple[AdminApp, list[httpx.Request]]:  # noqa: F811
    api = make_admin_app()
    sent: list[httpx.Request] = []
    api.client.event_hooks = {"request": [sent.append], "response": []}
    return api, sent


@pytest.fixture
def client(backend: tuple[AdminApp, list[httpx.Request]]) -> fe.AdminApiClient:
    return fe.AdminApiClient(http_client=backend[0].client)


def test_the_client_signs_in_and_manages_faqs(client: fe.AdminApiClient) -> None:
    token = SecretStr(ALEX)
    assert client.who_am_i(token).can("manage_faqs")

    created = client.create_faq(token, **{**NEW_FAQ, "is_active": True})
    listed = client.list_faqs(token, search="norway", category="shipping", is_active=True)
    assert [faq.id for faq in listed.faqs] == [created.id]

    edited = client.update_faq(token, created.id, created.version, answer="Changed.")
    assert edited.version == 2
    with pytest.raises(fe.AdminApiError) as caught:
        client.update_faq(token, created.id, created.version, answer="Stale.")
    assert caught.value.kind is fe.AdminErrorKind.CONFLICT
    assert caught.value.code == "FAQ_VERSION_CONFLICT" and caught.value.request_id

    hidden = client.set_faq_active(token, created.id, edited.version, active=False)
    assert hidden.is_active is False
    assert client.overview(token).faqs is not None


def test_errors_say_what_happened_without_secrets(client: fe.AdminApiClient) -> None:
    cases = {
        "stf_" + "x" * 43: fe.AdminErrorKind.SIGNED_OUT,
        MARIA: fe.AdminErrorKind.FORBIDDEN,
    }
    for token, kind in cases.items():
        with pytest.raises(fe.AdminApiError) as caught:
            client.list_faqs(SecretStr(token))
        assert caught.value.kind is kind
        assert token not in str(caught.value)

    with pytest.raises(fe.AdminApiError) as caught:
        client.create_faq(
            SecretStr(ALEX),
            question="",
            answer="a",
            category="orders",
            keywords=["x"],
            is_active=True,
        )
    assert caught.value.kind is fe.AdminErrorKind.INVALID
    assert any(field.startswith("question") for field in caught.value.fields)


def test_staff_disabled_is_its_own_kind(make_admin_app: Callable[..., AdminApp]) -> None:  # noqa: F811
    api = make_admin_app("nostaff", staff_token_hashes=[])
    with pytest.raises(fe.AdminApiError) as caught:
        fe.AdminApiClient(http_client=api.client).who_am_i(SecretStr(ALEX))
    assert caught.value.kind is fe.AdminErrorKind.STAFF_DISABLED


def test_the_token_travels_only_in_the_authorization_header(
    client: fe.AdminApiClient, backend: tuple[AdminApp, list[httpx.Request]]
) -> None:
    token = SecretStr(ALEX)
    client.who_am_i(token)
    client.list_faqs(token, search="order")
    client.list_tickets(token, statuses=["open"])
    sent = backend[1]
    assert sent
    for request in sent:
        assert request.headers["Authorization"] == f"Bearer {ALEX}"
        assert ALEX not in str(request.url)
        assert "cookie" not in request.headers


def test_the_client_handles_an_unreachable_backend() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    http = httpx.Client(base_url="http://backend.test", transport=httpx.MockTransport(refuse))
    with pytest.raises(fe.AdminApiError) as caught:
        fe.AdminApiClient(http_client=http).who_am_i(SecretStr(ALEX))
    assert caught.value.kind is fe.AdminErrorKind.UNREACHABLE


def test_keywords_and_tokens_are_parsed() -> None:
    assert fe.parse_keywords("track, order status\nshipping,, ") == [
        "track",
        "order status",
        "shipping",
    ]
    assert fe.staff_token("   ") is None
    assert fe.staff_token(f" {ALEX} ").get_secret_value() == ALEX  # type: ignore[union-attr]


# --- The page ---------------------------------------------------------------------------


@pytest.fixture
def open_admin(monkeypatch: pytest.MonkeyPatch, client: fe.AdminApiClient) -> Callable[[], AppTest]:
    def _open() -> AppTest:
        monkeypatch.setattr(admin_resources, "get_admin_client", lambda: client)
        monkeypatch.setattr(
            admin_resources, "get_settings", lambda: FrontendSettings(_env_file=None)
        )
        page = AppTest.from_file(str(ADMIN_PAGE), default_timeout=30)
        page.run()
        assert not page.exception
        return page

    return _open


def sign_in(page: AppTest, token: str) -> AppTest:
    page.text_input[0].input(token)
    page.button[0].click().run()
    assert not page.exception
    return page


def texts(page: AppTest) -> str:
    parts: list[Any] = [*page.error, *page.info, *page.warning, *page.success, *page.header]
    return " ".join(str(element.value) for element in parts)


def test_the_page_asks_for_a_staff_token_first(open_admin: Callable[[], AppTest]) -> None:
    page = open_admin()
    assert page.text_input[0].label == "Staff token"
    assert page.text_input[0].proto.type == page.text_input[0].proto.PASSWORD
    assert not page.sidebar.radio  # no sections before signing in


def test_a_wrong_token_is_refused(open_admin: Callable[[], AppTest]) -> None:
    page = sign_in(open_admin(), "stf_" + "y" * 43)
    assert "not valid" in texts(page)
    assert "admin_staff_token" not in page.session_state
    assert not page.query_params  # nothing ever goes into the URL


def test_an_admin_sees_every_section(open_admin: Callable[[], AppTest]) -> None:
    page = sign_in(open_admin(), ALEX)

    sections = page.sidebar.radio[0].options
    assert sections == [
        "Dashboard overview",
        "FAQ management",
        "Support tickets",
        "Analytics",
        "Audit log",
        "Administration information",
    ]
    assert "Dashboard overview" in texts(page)
    assert not page.query_params
    for name in sections:
        page.sidebar.radio[0].set_value(name).run()
        assert not page.exception, name
        assert name in texts(page)
        assert ALEX not in str(page._tree)  # the token is never rendered


def test_support_staff_see_tickets_only(open_admin: Callable[[], AppTest]) -> None:
    page = sign_in(open_admin(), MARIA)
    assert page.sidebar.radio[0].options == ["Support tickets", "Administration information"]


def test_signing_out_forgets_the_token(open_admin: Callable[[], AppTest]) -> None:
    page = sign_in(open_admin(), ALEX)
    page.sidebar.button[0].click().run()
    assert "admin_staff_token" not in page.session_state
    assert "signed out" in texts(page)
    assert page.text_input[0].label == "Staff token"


def test_an_admin_edits_an_faq_from_the_page(
    open_admin: Callable[[], AppTest], backend: tuple[AdminApp, list[httpx.Request]]
) -> None:
    page = sign_in(open_admin(), ALEX)
    page.sidebar.radio[0].set_value("FAQ management").run()
    assert page.selectbox(key="admin_selected_faq").value == "faq_101"

    answer = next(area for area in page.text_area if area.label == "Answer" and area.value)
    answer.input("Edited on the dashboard.")
    next(b for b in page.button if b.label == "Save changes").click().run()

    assert not page.exception
    stored = backend[0].faq("faq_101")
    assert stored["answer"] == "Edited on the dashboard." and stored["updated_by"] == "alex"


# --- Separation from the customer page --------------------------------------------------


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


def test_the_customer_page_has_no_admin_code() -> None:
    for path in [CUSTOMER_PAGE, *(ROOT / "frontend").glob("components/*.py")]:
        imported = _imports(path)
        assert not {name for name in imported if "admin" in name}, path
        source = path.read_text(encoding="utf-8")
        assert "/admin" not in source and "stf_" not in source, path


def test_the_client_reads_an_faqs_history(client: fe.AdminApiClient) -> None:
    token = SecretStr(ALEX)
    client.update_faq(token, "faq_101", 1, answer="Changed for history.")
    history = client.faq_revisions(token, "faq_101")
    assert [(r.version, r.action) for r in history.revisions] == [(2, "updated"), (1, "created")]
    assert history.revisions[0].new_values == {"answer": "Changed for history."}


def test_the_page_shows_an_faqs_history(
    open_admin: Callable[[], AppTest], client: fe.AdminApiClient
) -> None:
    client.update_faq(SecretStr(ALEX), "faq_101", 1, answer="Shown in the history.")
    page = sign_in(open_admin(), ALEX)
    page.sidebar.radio[0].set_value("FAQ management").run()

    shown = " ".join(str(c.value) for c in page.caption) + " ".join(
        str(m.value) for m in page.markdown
    )
    assert "Version 2" in shown and "updated by alex" in shown
    assert "Shown in the history." in shown


# --- FAQ management page (Task 11) --------------------------------------------------------


def button(page: AppTest, label: str) -> Any:
    return next(b for b in page.button if b.label == label)


def open_section(open_admin: Callable[[], AppTest], name: str, token: str = ALEX) -> AppTest:
    page = sign_in(open_admin(), token)
    page.sidebar.radio[0].set_value(name).run()
    assert not page.exception
    return page


def fill_new_faq(page: AppTest, question: str, answer: str, tags: str, form: int = 0) -> AppTest:
    page.text_input(key=f"new-faq-{form}-q").input(question)
    page.text_area(key=f"new-faq-{form}-a").input(answer)
    page.text_input(key=f"new-faq-{form}-t").input(tags)
    button(page, "Create FAQ").click().run()
    assert not page.exception
    return page


def test_creating_an_faq_confirms_only_after_the_backend_saved_it(
    open_admin: Callable[[], AppTest], client: fe.AdminApiClient
) -> None:
    page = open_section(open_admin, "FAQ management")
    fill_new_faq(page, "Do you ship to Norway?", "Yes, in 5-7 days.", "norway, shipping")

    saved = client.list_faqs(SecretStr(ALEX), search="Norway").faqs
    assert len(saved) == 1
    assert [s.value for s in page.success] == [f"Created {saved[0].id} (active)."]
    assert page.text_input(key="new-faq-1-q").value == ""  # a fresh, empty form


def test_a_duplicate_submission_is_refused(
    open_admin: Callable[[], AppTest], client: fe.AdminApiClient
) -> None:
    page = open_section(open_admin, "FAQ management")
    fill_new_faq(page, "Do you ship to Norway?", "Yes.", "norway")
    fill_new_faq(page, "do you ship to norway?", "Yes.", "norway", form=1)

    assert any("already has this question" in e.value for e in page.error)
    assert not page.success
    assert client.list_faqs(SecretStr(ALEX), search="Norway").total == 1


def test_invalid_input_is_explained_and_not_sent(
    open_admin: Callable[[], AppTest], backend: tuple[AdminApp, list[httpx.Request]]
) -> None:
    page = open_section(open_admin, "FAQ management")
    sent = len(backend[1])
    fill_new_faq(page, "  ", "", "")
    messages = [e.value for e in page.error]
    assert "Enter a question." in messages and "Enter at least one tag." in messages
    assert not [r for r in backend[1][sent:] if r.method == "POST"]


def test_deactivation_needs_confirmation(
    open_admin: Callable[[], AppTest], client: fe.AdminApiClient
) -> None:
    page = open_section(open_admin, "FAQ management")
    button(page, "Deactivate faq_101…").click().run()
    assert any("Deactivate faq_101?" in w.value for w in page.warning)
    assert client.get_faq(SecretStr(ALEX), "faq_101").is_active  # nothing happened yet

    button(page, "Cancel").click().run()
    assert client.get_faq(SecretStr(ALEX), "faq_101").is_active

    button(page, "Deactivate faq_101…").click().run()
    button(page, "Yes, deactivate").click().run()
    assert not client.get_faq(SecretStr(ALEX), "faq_101").is_active
    assert [s.value for s in page.success] == ["faq_101 is now inactive."]


def test_a_stale_edit_is_not_saved_and_the_text_is_kept(
    open_admin: Callable[[], AppTest], client: fe.AdminApiClient
) -> None:
    page = open_section(open_admin, "FAQ management")
    client.update_faq(SecretStr(ALEX), "faq_101", 1, answer="Changed by a colleague.")

    answer = next(a for a in page.text_area if a.label == "Answer" and a.value)
    answer.input("My edit, based on version 1.")
    button(page, "Save changes").click().run()

    assert client.get_faq(SecretStr(ALEX), "faq_101").answer == "Changed by a colleague."
    assert any("Not saved" in w.value for w in page.warning)
    assert not page.success
    values = [a.value for a in page.text_area]
    assert "My edit, based on version 1." in values  # kept for copying
    assert "Changed by a colleague." in values  # the latest version is loaded


def test_saving_an_edit_shows_the_new_version(open_admin: Callable[[], AppTest]) -> None:
    page = open_section(open_admin, "FAQ management")
    answer = next(a for a in page.text_area if a.label == "Answer" and a.value)
    answer.input("A better answer.")
    button(page, "Save changes").click().run()
    assert [s.value for s in page.success] == ["Saved faq_101: now version 2."]
    assert "faq_101 · active · version 2" in [s.value for s in page.subheader]


# --- Tickets page (Tasks 12 and 13) ---------------------------------------------------------


def test_staff_assign_note_and_progress_a_ticket(
    open_admin: Callable[[], AppTest], backend: tuple[AdminApp, list[httpx.Request]]
) -> None:
    from tests.test_admin_tickets_analytics import create_ticket

    api = backend[0]
    ticket, _ = create_ticket(api)
    page = open_section(open_admin, "Support tickets", token=MARIA)
    assert ticket["reference_number"] in list(page.dataframe[0].value["Reference"])

    next(s for s in page.selectbox if s.label == "Assigned to").set_value("maria")
    button(page, "Save assignment").click().run()
    assert any("assigned to maria" in s.value for s in page.success)

    next(a for a in page.text_area if a.label == "Add a note").input("Waiting for the courier.")
    button(page, "Add note").click().run()
    assert any("Note added" in s.value for s in page.success)

    next(s for s in page.selectbox if s.label == "Change status to").set_value("in_progress")
    button(page, "Change status").click().run()
    stored = api.get(f"/api/v1/support/tickets/{ticket['ticket_id']}").json()
    assert (stored["status"], stored["assigned_to"]) == ("in_progress", "maria")
    notes = api.get(f"/api/v1/support/tickets/{ticket['ticket_id']}/notes").json()["notes"]
    assert [n["body"] for n in notes] == ["Waiting for the courier."]


def test_tickets_can_be_found_by_reference(
    open_admin: Callable[[], AppTest], backend: tuple[AdminApp, list[httpx.Request]]
) -> None:
    from tests.test_admin_tickets_analytics import create_ticket

    create_ticket(backend[0])
    wanted, _ = create_ticket(backend[0], "sales")
    page = open_section(open_admin, "Support tickets")
    reference = next(t for t in page.text_input if t.label == "Reference")
    reference.input(wanted["reference_number"]).run()
    assert list(page.dataframe[0].value["Reference"]) == [wanted["reference_number"]]


# --- Overview and analytics (Tasks 14 and 15) -----------------------------------------------


def metrics(page: AppTest) -> dict[str, str]:
    return {m.label: m.value for m in page.metric}


def test_the_overview_shows_real_counts(
    open_admin: Callable[[], AppTest], backend: tuple[AdminApp, list[httpx.Request]]
) -> None:
    from tests.test_admin_tickets_analytics import create_ticket

    create_ticket(backend[0], "sales")
    shown = metrics(open_section(open_admin, "Dashboard overview"))
    assert shown["Total tickets"] == "1" and shown["Sales leads (all time)"] == "1"
    assert shown["Open (not resolved or closed)"] == "1"
    active = sum(1 for record in TEST_FAQ_RECORDS if record.get("is_active", True))
    assert shown["Active FAQs"] == str(active)


def test_analytics_show_counts_and_hide_small_sample_rates(
    open_admin: Callable[[], AppTest], backend: tuple[AdminApp, list[httpx.Request]]
) -> None:
    backend[0].chat("I want to track my parcel")
    page = open_section(open_admin, "Analytics")
    shown = metrics(page)
    assert shown["Chatbot responses"] == "1" and shown["Customer messages"] == "1"
    assert set(page.dataframe[0].value["Value"]) == {"—"}  # too few responses for a rate
    assert any("not that the customer" in m.value for m in page.markdown)


def test_analytics_on_an_empty_database_say_so(open_admin: Callable[[], AppTest]) -> None:
    page = open_section(open_admin, "Analytics")
    assert metrics(page)["Chatbot responses"] == "0"
    infos = [i.value for i in page.info]
    assert "No answered exchanges in this period." in infos
    assert "No activity recorded in this period." in infos


def test_a_stale_ticket_change_is_refused_and_explained(
    open_admin: Callable[[], AppTest], backend: tuple[AdminApp, list[httpx.Request]]
) -> None:
    from tests.test_admin_tickets_analytics import assign, create_ticket

    api = backend[0]
    ticket, _ = create_ticket(api)
    page = open_section(open_admin, "Support tickets")
    assign(api, ticket["ticket_id"], "sam", None)  # a colleague, after the page loaded

    next(s for s in page.selectbox if s.label == "Assigned to").set_value("maria")
    button(page, "Save assignment").click().run()

    assert any("changed since it was read" in e.value for e in page.error)
    assert not page.success
    assert api.get(f"/api/v1/support/tickets/{ticket['ticket_id']}").json()["assigned_to"] == "sam"


def test_the_audit_log_page_lists_recorded_actions(
    open_admin: Callable[[], AppTest], client: fe.AdminApiClient
) -> None:
    client.update_faq(SecretStr(ALEX), "faq_101", 1, answer="Audited change.")
    page = open_section(open_admin, "Audit log")
    rows = page.dataframe[0].value
    assert "faq.updated" in list(rows["Action"])
    assert "Audited change." not in str(rows)


def test_a_revoked_token_signs_the_page_out(
    open_admin: Callable[[], AppTest], backend: tuple[AdminApp, list[httpx.Request]]
) -> None:
    from app.core.staff_auth import StaffAuthenticator

    page = open_section(open_admin, "FAQ management")
    authenticator = backend[0].app.state.staff_authenticator
    remaining = [entry for entry in STAFF_HASHES if not entry.startswith("alex:")]
    backend[0].app.state.staff_authenticator = StaffAuthenticator(remaining)  # alex revoked
    try:
        page.run()
        assert not page.exception
        assert "admin_staff_token" not in page.session_state
        assert any("not valid" in i.value for i in page.info)
        assert page.text_input[0].label == "Staff token"
    finally:
        backend[0].app.state.staff_authenticator = authenticator


def test_ticket_analytics_appear_on_the_analytics_page(
    open_admin: Callable[[], AppTest], backend: tuple[AdminApp, list[httpx.Request]]
) -> None:
    from tests.test_admin_tickets_analytics import create_ticket

    create_ticket(backend[0])
    shown = metrics(open_section(open_admin, "Analytics"))
    assert shown["Created"] == "1" and shown["Backlog now"] == "1"
    assert shown["Unassigned in backlog"] == "1"


def test_switching_staff_access_off_also_signs_the_page_out(
    open_admin: Callable[[], AppTest], backend: tuple[AdminApp, list[httpx.Request]]
) -> None:
    from app.core.staff_auth import StaffAuthenticator

    page = open_section(open_admin, "FAQ management")
    authenticator = backend[0].app.state.staff_authenticator
    backend[0].app.state.staff_authenticator = StaffAuthenticator([])
    try:
        page.run()
        assert "admin_staff_token" not in page.session_state
        assert any("disabled" in i.value for i in page.info)
    finally:
        backend[0].app.state.staff_authenticator = authenticator
