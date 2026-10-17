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
from tests.test_admin_api import ALEX, MARIA, NEW_FAQ, AdminApp, make_admin_app  # noqa: F401

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
