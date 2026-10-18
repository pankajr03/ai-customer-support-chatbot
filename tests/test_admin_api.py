"""Staff admin API (Stage 11, Tasks 3-6), over HTTP against the real app.

The app is built the way production builds it: FAQs in the database, seeded from a
(test) JSON file into a temporary SQLite file. The embedding model is the workflow
tests' scripted encoder, reading the app's own catalogue, so index rebuilds can be seen.
"""

import logging
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.config import Settings
from app.core.staff_auth import StaffMember, StaffRole, generate_staff_token, hash_staff_token
from app.database.connection import Database
from app.main import create_app
from app.models.errors import ErrorResponse
from app.models.faq_admin import FAQChanges
from app.services.embedding_service import EmbeddingService
from app.services.semantic_search_service import SemanticSearchService
from tests.fakes import FAKE_MODEL_NAME
from tests.test_api_workflows import ScriptedEncoder

ADMIN = "/api/v1/admin"
FAQS = f"{ADMIN}/faqs"
TICKETS = "/api/v1/support/tickets"
ALEX = generate_staff_token()  # admin
MARIA = generate_staff_token()  # support
LEGACY = generate_staff_token()  # a Stage 10 entry without a role
STAFF_HASHES = [
    f"alex:admin:{hash_staff_token(ALEX)}",
    f"maria:support:{hash_staff_token(MARIA)}",
    f"sam:{hash_staff_token(LEGACY)}",
]
NEW_FAQ = {
    "question": "  Do you ship   to Norway? ",
    "answer": "Yes. Orders to Norway usually arrive within 5-7 working days.\n\nCustoms may apply.",
    "category": "Shipping",
    "keywords": ["Norway", "international shipping", "norway"],
}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def error_code(response: Any, status: int) -> str:
    assert response.status_code == status, response.text
    ErrorResponse.model_validate(response.json())
    return str(response.json()["error"]["code"])


class HookedScriptedEncoder(ScriptedEncoder):
    """The workflow tests' encoder, plus hooks run when a query (one text that is not an
    FAQ question) is embedded: a way to commit an FAQ change mid-request."""

    def __init__(self) -> None:
        super().__init__()
        self.query_hooks: list[Callable[[], None]] = []

    def encode(self, sentences: list[str], **kwargs: Any) -> Any:
        if len(sentences) == 1 and sentences[0] not in self.questions and self.query_hooks:
            self.query_hooks.pop(0)()
        return super().encode(sentences, **kwargs)


class AdminApp:
    def __init__(
        self,
        client: TestClient,
        app: FastAPI,
        database: Database,
        encoder: HookedScriptedEncoder | None = None,
    ) -> None:
        self.client, self.app, self.database = client, app, database
        self.encoder = encoder or HookedScriptedEncoder()

    def chat(self, message: str) -> dict[str, Any]:
        created = self.client.post("/api/v1/conversations").json()
        response = self.client.post(
            f"/api/v1/conversations/{created['session_id']}/chat",
            json={"message": message},
            headers=bearer(created["access_token"]),
        )
        assert response.status_code == 200, response.text
        return dict(response.json())

    def get(self, path: str, token: str = ALEX, **kwargs: Any) -> Any:
        return self.client.get(path, headers=bearer(token), **kwargs)

    def post(self, path: str, json: Any, token: str = ALEX) -> Any:
        return self.client.post(path, json=json, headers=bearer(token))

    def patch(self, path: str, json: Any, token: str = ALEX) -> Any:
        return self.client.patch(path, json=json, headers=bearer(token))

    def faq(self, faq_id: str) -> dict[str, Any]:
        response = self.get(f"{FAQS}/{faq_id}")
        assert response.status_code == 200, response.text
        return dict(response.json())


@pytest.fixture
def make_admin_app(
    make_settings: Callable[..., Settings], faq_file: Path, tmp_path: Path
) -> Iterator[Callable[..., AdminApp]]:
    """A running app with database FAQs. Same ``name`` = same database file (a restart)."""
    clients: list[TestClient] = []

    def _make(name: str = "admin", **settings: Any) -> AdminApp:
        database = Database(f"sqlite:///{(tmp_path / f'{name}.db').as_posix()}")
        holder: list[FastAPI] = []
        encoder = HookedScriptedEncoder()
        semantic = SemanticSearchService(
            lambda: holder[0].state.faq_service.list_active_faqs(),
            EmbeddingService(FAKE_MODEL_NAME, loader=lambda model: encoder),
            catalog_revision=lambda: holder[0].state.faq_service.catalog_revision(),
        )
        settings = {"staff_token_hashes": STAFF_HASHES, **settings}
        app = create_app(
            make_settings(faq_data_path=faq_file, query_rewrite_use_llm=False, **settings),
            semantic_search_service=semantic,
            database=database,
        )
        holder.append(app)
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return AdminApp(client, app, database, encoder)

    yield _make
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def admin(make_admin_app: Callable[..., AdminApp]) -> AdminApp:
    return make_admin_app()


# --- Authentication and roles -----------------------------------------------------------


def test_me_reports_the_role_and_its_permissions(admin: AdminApp) -> None:
    me = admin.get(f"{ADMIN}/me").json()
    assert me["staff_id"] == "alex" and me["role"] == "admin"
    assert set(me["permissions"]) == {
        "manage_faqs",
        "manage_tickets",
        "view_analytics",
        "view_audit_log",
        "view_customer_contacts",
    }
    assert "prototype" in me["auth_notice"]

    support = admin.get(f"{ADMIN}/me", token=MARIA).json()
    assert support["role"] == "support"
    assert set(support["permissions"]) == {"manage_tickets", "view_customer_contacts"}


def test_a_stage_10_entry_without_a_role_is_support(admin: AdminApp) -> None:
    assert admin.get(f"{ADMIN}/me", token=LEGACY).json()["role"] == "support"
    assert error_code(admin.get(FAQS, token=LEGACY), 403) == "STAFF_FORBIDDEN"


ADMIN_CALLS = [
    ("GET", f"{ADMIN}/overview", None),
    ("GET", FAQS, None),
    ("GET", f"{FAQS}/faq_101", None),
    ("POST", FAQS, NEW_FAQ),
    ("PATCH", f"{FAQS}/faq_101", {"expected_version": 1, "answer": "Changed."}),
    ("POST", f"{FAQS}/faq_101/deactivate", {"expected_version": 1}),
    ("POST", f"{FAQS}/faq_101/activate", {"expected_version": 1}),
]


@pytest.mark.parametrize(("method", "path", "body"), ADMIN_CALLS)
def test_admin_endpoints_refuse_everyone_but_admins(
    admin: AdminApp, method: str, path: str, body: Any
) -> None:
    def send(headers: dict[str, str]) -> Any:
        return admin.client.request(method, path, json=body, headers=headers)

    assert error_code(send({}), 401) == "STAFF_AUTH_REQUIRED"
    assert error_code(send(bearer("not-a-token")), 401) == "STAFF_AUTH_REQUIRED"
    assert error_code(send(bearer(generate_staff_token())), 401) == "STAFF_AUTH_REQUIRED"
    assert error_code(send(bearer(MARIA)), 403) == "STAFF_FORBIDDEN"
    assert admin.faq("faq_101")["version"] == 1  # nothing was changed by the refusals


def test_a_customer_conversation_token_is_never_a_staff_token(admin: AdminApp) -> None:
    created = admin.client.post("/api/v1/conversations").json()
    customer = created["access_token"]

    assert error_code(admin.get(f"{ADMIN}/me", token=customer), 401) == "STAFF_AUTH_REQUIRED"
    assert error_code(admin.get(FAQS, token=customer), 401) == "STAFF_AUTH_REQUIRED"
    assert error_code(admin.get(TICKETS, token=customer), 401) == "STAFF_AUTH_REQUIRED"


def test_a_token_in_the_url_is_refused(admin: AdminApp) -> None:
    for name in ("access_token", "token", "authorization"):
        response = admin.client.get(FAQS, params={name: ALEX})
        assert error_code(response, 400) == "VALIDATION_ERROR"
        assert ALEX not in response.text


def test_without_configured_staff_every_admin_endpoint_is_disabled(
    make_admin_app: Callable[..., AdminApp],
) -> None:
    api = make_admin_app("nostaff", staff_token_hashes=[])
    for method, path, body in ADMIN_CALLS:
        response = api.client.request(method, path, json=body, headers=bearer(ALEX))
        assert error_code(response, 403) == "STAFF_ACCESS_DISABLED"
    assert error_code(api.get(f"{ADMIN}/me"), 403) == "STAFF_ACCESS_DISABLED"


def test_support_staff_keep_ticket_access(admin: AdminApp) -> None:
    assert admin.get(TICKETS, token=MARIA).status_code == 200
    assert admin.get(TICKETS, token=ALEX).status_code == 200


def test_staff_tokens_are_never_logged(admin: AdminApp, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    admin.get(FAQS)
    admin.get(FAQS, token=MARIA)
    admin.post(FAQS, NEW_FAQ)
    admin.client.get(FAQS, params={"token": ALEX})
    for token in (ALEX, MARIA):
        assert token not in caplog.text
    assert NEW_FAQ["answer"] not in caplog.text  # FAQ text is not logged either


def test_admin_responses_are_not_cached(admin: AdminApp) -> None:
    assert admin.get(FAQS).headers["Cache-Control"] == "no-store"
    assert admin.get(f"{ADMIN}/me").headers["Cache-Control"] == "no-store"
    assert admin.get(FAQS, token=MARIA).headers["Cache-Control"] == "no-store"


# --- FAQ management ---------------------------------------------------------------------


def test_the_catalogue_is_seeded_from_the_file_with_its_ids(
    admin: AdminApp, faq_records: list[dict[str, Any]]
) -> None:
    listing = admin.get(FAQS, params={"limit": 100}).json()
    assert [faq["id"] for faq in listing["faqs"]] == [r["id"] for r in faq_records]
    by_id = {faq["id"]: faq for faq in listing["faqs"]}
    for record in faq_records:
        stored = by_id[record["id"]]
        assert stored["category"] == record["category"]
        assert stored["is_active"] == record.get("is_active", True)
        assert stored["version"] == 1 and stored["created_by"] is None
        assert stored["created_at"].endswith("Z") or stored["created_at"].endswith("+00:00")


def test_listing_filters_searches_and_pages(admin: AdminApp) -> None:
    inactive = admin.get(FAQS, params={"is_active": "false"}).json()
    assert inactive["total"] >= 1 and all(not f["is_active"] for f in inactive["faqs"])

    shipping = admin.get(FAQS, params={"category": "shipping"}).json()
    assert shipping["total"] >= 1 and all(f["category"] == "shipping" for f in shipping["faqs"])

    first = admin.get(FAQS, params={"limit": 2}).json()
    second = admin.get(FAQS, params={"limit": 2, "offset": 2}).json()
    assert first["has_more"] and len(first["faqs"]) == 2
    assert not {f["id"] for f in first["faqs"]} & {f["id"] for f in second["faqs"]}

    by_id = admin.get(FAQS, params={"q": "FAQ_101"}).json()
    assert [f["id"] for f in by_id["faqs"]] == ["faq_101"]
    assert admin.get(FAQS, params={"q": "%"}).json()["total"] == 0  # LIKE wildcards escaped


def test_invalid_list_parameters_are_validation_errors(admin: AdminApp) -> None:
    assert error_code(admin.get(FAQS, params={"limit": 0}), 422) == "VALIDATION_ERROR"
    assert error_code(admin.get(FAQS, params={"category": "pets"}), 422) == "VALIDATION_ERROR"
    assert error_code(admin.get(FAQS, params={"q": "x" * 201}), 422) == "VALIDATION_ERROR"


def test_creating_an_faq_normalises_it_and_serves_it_to_customers(admin: AdminApp) -> None:
    before = {f["id"] for f in admin.get(FAQS, params={"limit": 100}).json()["faqs"]}

    response = admin.post(FAQS, NEW_FAQ)

    assert response.status_code == 201, response.text
    created = response.json()
    assert response.headers["Location"] == f"{FAQS}/{created['id']}"
    assert created["question"] == "Do you ship to Norway?"
    assert created["answer"] == NEW_FAQ["answer"].strip()  # line breaks kept
    assert created["category"] == "shipping"
    assert created["keywords"] == ["norway", "international shipping"]
    assert created["version"] == 1
    assert created["created_by"] == created["updated_by"] == "alex"
    highest = max(int(faq_id[4:]) for faq_id in before)
    assert created["id"] == f"faq_{highest + 1:03d}"
    public = admin.client.get(f"/api/v1/faqs/{created['id']}")
    assert public.status_code == 200 and public.json()["question"] == "Do you ship to Norway?"
    found = admin.client.get("/api/v1/faqs/search", params={"q": "norway"}).json()["results"]
    assert created["id"] in [r["id"] for r in found]


@pytest.mark.parametrize(
    "change",
    [
        {"question": "   "},
        {"answer": ""},
        {"question": "x" * 501},
        {"answer": "x" * 5001},
        {"category": "pets"},
        {"keywords": []},
        {"keywords": ["ok", "<script>"]},
        {"keywords": [f"k{n}" for n in range(21)]},
        {"is_active": "yes"},
        {"id": "faq_999"},  # IDs are allocated by the server
        {"unknown": 1},
    ],
)
def test_invalid_new_faqs_are_refused_without_echoing_values(
    admin: AdminApp, change: dict[str, Any]
) -> None:
    response = admin.post(FAQS, {**NEW_FAQ, **change})
    assert error_code(response, 422) == "VALIDATION_ERROR"
    assert "<script>" not in response.text
    assert admin.get(FAQS, params={"q": "norway"}).json()["total"] == 0


def test_editing_bumps_the_version_and_records_who(admin: AdminApp) -> None:
    response = admin.patch(f"{FAQS}/faq_101", {"expected_version": 1, "answer": "New answer."})

    assert response.status_code == 200, response.text
    edited = response.json()
    assert edited["answer"] == "New answer." and edited["version"] == 2
    assert edited["updated_by"] == "alex" and edited["created_by"] is None
    assert edited["updated_at"] >= edited["created_at"]
    assert admin.client.get("/api/v1/faqs/faq_101").json()["answer"] == "New answer."


def test_a_stale_edit_is_refused_and_overwrites_nothing(admin: AdminApp) -> None:
    admin.patch(f"{FAQS}/faq_101", {"expected_version": 1, "answer": "First editor."})

    stale = admin.patch(f"{FAQS}/faq_101", {"expected_version": 1, "answer": "Second editor."})

    assert error_code(stale, 409) == "FAQ_VERSION_CONFLICT"
    assert admin.faq("faq_101")["answer"] == "First editor."
    assert admin.faq("faq_101")["version"] == 2


def test_an_edit_that_changes_nothing_keeps_the_version(admin: AdminApp) -> None:
    current = admin.faq("faq_101")
    same = admin.patch(
        f"{FAQS}/faq_101", {"expected_version": 1, "question": current["question"]}
    ).json()
    assert same["version"] == 1


def test_an_edit_needs_a_version_and_a_change(admin: AdminApp) -> None:
    assert error_code(admin.patch(f"{FAQS}/faq_101", {"answer": "x"}), 422) == "VALIDATION_ERROR"
    response = admin.patch(f"{FAQS}/faq_101", {"expected_version": 1})
    assert error_code(response, 422) == "VALIDATION_ERROR"


def test_unknown_faqs_are_not_found(admin: AdminApp) -> None:
    assert error_code(admin.get(f"{FAQS}/faq_999999"), 404) == "FAQ_NOT_FOUND"
    assert error_code(admin.get(f"{FAQS}/not-an-id"), 404) == "FAQ_NOT_FOUND"
    response = admin.patch(f"{FAQS}/faq_999999", {"expected_version": 1, "answer": "x"})
    assert error_code(response, 404) == "FAQ_NOT_FOUND"


def test_deactivating_hides_an_faq_from_customers_and_keeps_it(admin: AdminApp) -> None:
    response = admin.post(f"{FAQS}/faq_101/deactivate", {"expected_version": 1})

    assert response.status_code == 200 and response.json()["is_active"] is False
    assert admin.client.get("/api/v1/faqs/faq_101").status_code == 404
    public_ids = {
        f["id"] for f in admin.client.get("/api/v1/faqs", params={"limit": 100}).json()["faqs"]
    }
    assert "faq_101" not in public_ids
    assert admin.faq("faq_101")["is_active"] is False  # still stored

    again = admin.post(f"{FAQS}/faq_101/activate", {"expected_version": 2})
    assert again.json()["is_active"] is True and again.json()["version"] == 3
    assert admin.client.get("/api/v1/faqs/faq_101").status_code == 200


def test_the_last_active_faq_cannot_be_deactivated(admin: AdminApp) -> None:
    active = admin.get(FAQS, params={"is_active": "true", "limit": 100}).json()["faqs"]
    *others, last = active
    for faq in others:
        deactivated = admin.post(
            f"{FAQS}/{faq['id']}/deactivate", {"expected_version": faq["version"]}
        )
        assert deactivated.status_code == 200

    response = admin.post(f"{FAQS}/{last['id']}/deactivate", {"expected_version": last["version"]})

    assert error_code(response, 409) == "FAQ_LAST_ACTIVE"
    assert admin.faq(last["id"])["is_active"] is True


def test_there_is_no_way_to_delete_an_faq(admin: AdminApp) -> None:
    response = admin.client.delete(f"{FAQS}/faq_101", headers=bearer(ALEX))
    assert error_code(response, 405) == "METHOD_NOT_ALLOWED"
    assert admin.faq("faq_101")["id"] == "faq_101"


def test_changes_drop_the_semantic_index_so_it_is_rebuilt(admin: AdminApp) -> None:
    semantic: SemanticSearchService = admin.app.state.semantic_search_service
    assert "faq_101" in semantic.get_index().faq_ids

    created = admin.post(FAQS, NEW_FAQ).json()
    admin.post(f"{FAQS}/faq_101/deactivate", {"expected_version": 1})

    rebuilt = semantic.get_index().faq_ids
    assert created["id"] in rebuilt and "faq_101" not in rebuilt


def test_staff_edits_survive_a_restart(make_admin_app: Callable[..., AdminApp]) -> None:
    first = make_admin_app("persist")
    first.patch(f"{FAQS}/faq_101", {"expected_version": 1, "answer": "Edited by staff."})
    created = first.post(FAQS, NEW_FAQ).json()

    restarted = make_admin_app("persist")  # same database file, same JSON seed file

    assert restarted.faq("faq_101")["answer"] == "Edited by staff."
    assert restarted.faq(created["id"])["question"] == "Do you ship to Norway?"
    assert restarted.client.get("/api/v1/faqs/faq_101").json()["answer"] == "Edited by staff."


def test_the_database_rejects_invalid_rows_written_around_the_app(admin: AdminApp) -> None:
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError), admin.database.write() as db:
        db.execute(text("UPDATE faqs SET category = 'pets' WHERE id = 'faq_101'"))
    with pytest.raises(IntegrityError), admin.database.write() as db:
        db.execute(text("UPDATE faqs SET answer = '   ' WHERE id = 'faq_101'"))
    with pytest.raises(IntegrityError), admin.database.write() as db:
        db.execute(text("UPDATE faqs SET version = 0 WHERE id = 'faq_101'"))
    with pytest.raises(IntegrityError), admin.database.write() as db:
        db.execute(text("UPDATE faqs SET id = 'faq_1x1' WHERE id = 'faq_101'"))


def test_an_app_with_an_in_memory_faq_service_cannot_edit_faqs(
    make_faq_client: Callable[..., TestClient], faq_repository: Any
) -> None:
    with make_faq_client(faq_repository.list_all(), staff_token_hashes=STAFF_HASHES) as client:
        response = client.get(FAQS, headers=bearer(ALEX))
        assert error_code(response, 503) == "SERVICE_UNAVAILABLE"
        assert "cannot be edited" in response.json()["error"]["message"]
        overview = client.get(f"{ADMIN}/overview", headers=bearer(ALEX)).json()
        assert overview["faqs"] is None


# --- Overview ---------------------------------------------------------------------------


def test_the_overview_counts_faqs_and_tickets(admin: AdminApp) -> None:
    created = admin.client.post("/api/v1/conversations").json()
    request = {
        "session_id": created["session_id"],
        "client_request_id": "0d8e6a3c-8f7b-4a3e-9c1d-2b4f6a8c0e1f",
        "ticket_type": "support",
        "subject": "Refund assistance",
        "description": "I need help with a refund request.",
        "contact": {
            "name": "Example Customer",
            "email": "customer@example.com",
            "phone": None,
            "contact_preference": "email",
            "consent_given": True,
        },
        "confirmed": True,
    }
    ticket = admin.client.post(TICKETS, json=request, headers=bearer(created["access_token"]))
    assert ticket.status_code == 201

    overview = admin.get(f"{ADMIN}/overview").json()

    faqs = overview["faqs"]
    assert faqs["total"] == faqs["active"] + faqs["inactive"]
    assert sum(c["active"] + c["inactive"] for c in faqs["by_category"]) == faqs["total"]
    tickets = overview["tickets"]
    assert tickets["total"] == 1 and tickets["by_status"]["open"] == 1
    assert tickets["by_type"] == {"support": 1, "sales": 0}
    assert tickets["created_last_7_days"] == 1 and tickets["open_queue"] == 1
    assert "customer@example.com" not in str(overview)
    assert "refund" not in str(overview).lower()


# --- Revision history (Task 7) ------------------------------------------------------------


def revisions(admin: AdminApp, faq_id: str, token: str = ALEX, **params: Any) -> Any:
    return admin.get(f"{FAQS}/{faq_id}/revisions", token=token, params=params)


def test_every_change_is_recorded_with_only_the_changed_fields(admin: AdminApp) -> None:
    created = admin.post(FAQS, NEW_FAQ).json()
    faq_id = created["id"]
    admin.patch(f"{FAQS}/{faq_id}", {"expected_version": 1, "answer": "Second answer."})
    admin.patch(
        f"{FAQS}/{faq_id}",
        {"expected_version": 2, "question": "Do you deliver to Norway?", "keywords": ["oslo"]},
    )
    admin.patch(f"{FAQS}/{faq_id}", {"expected_version": 3, "category": "orders"})
    admin.post(f"{FAQS}/{faq_id}/deactivate", {"expected_version": 4})
    admin.post(f"{FAQS}/{faq_id}/activate", {"expected_version": 5})

    history = revisions(admin, faq_id).json()

    assert history["total"] == 6 and history["faq_id"] == faq_id
    entries = history["revisions"]
    assert [r["version"] for r in entries] == [6, 5, 4, 3, 2, 1]  # newest first
    assert [r["action"] for r in entries] == [
        "activated",
        "deactivated",
        "updated",
        "updated",
        "updated",
        "created",
    ]
    assert all(r["changed_by"] == "alex" for r in entries)
    created_entry = entries[-1]
    assert created_entry["previous_values"] is None
    assert created_entry["new_values"]["question"] == "Do you ship to Norway?"
    answer_entry = entries[-2]
    assert answer_entry["changed_fields"] == ["answer"]
    assert answer_entry["previous_values"] == {"answer": created["answer"]}
    assert answer_entry["new_values"] == {"answer": "Second answer."}
    assert entries[-3]["changed_fields"] == ["question", "keywords"]
    assert entries[-4]["new_values"] == {"category": "orders"}
    assert entries[1]["new_values"] == {"is_active": False}


def test_refused_or_empty_edits_record_nothing(admin: AdminApp) -> None:
    before = revisions(admin, "faq_101").json()["total"]
    admin.patch(f"{FAQS}/faq_101", {"expected_version": 9, "answer": "Stale."})  # 409
    current = admin.faq("faq_101")
    admin.patch(f"{FAQS}/faq_101", {"expected_version": 1, "answer": current["answer"]})  # no-op
    admin.patch(f"{FAQS}/faq_101", {"expected_version": 1, "answer": ""})  # 422
    assert revisions(admin, "faq_101").json()["total"] == before


def test_imported_faqs_start_with_a_creation_by_the_import(admin: AdminApp) -> None:
    entries = revisions(admin, "faq_101").json()["revisions"]
    assert [(r["action"], r["changed_by"], r["version"]) for r in entries] == [("created", None, 1)]


def test_history_is_paginated(admin: AdminApp) -> None:
    for version in range(1, 4):
        admin.patch(f"{FAQS}/faq_101", {"expected_version": version, "answer": f"V{version}."})
    page = revisions(admin, "faq_101", limit=2, offset=1).json()
    assert [r["version"] for r in page["revisions"]] == [3, 2]
    assert page["has_more"] and page["total"] == 4


def test_history_is_for_admins_only_and_never_public(admin: AdminApp) -> None:
    assert error_code(revisions(admin, "faq_101", token=MARIA), 403) == "STAFF_FORBIDDEN"
    response = admin.client.get(f"{FAQS}/faq_101/revisions")
    assert error_code(response, 401) == "STAFF_AUTH_REQUIRED"
    assert error_code(revisions(admin, "faq_999999"), 404) == "FAQ_NOT_FOUND"
    assert error_code(revisions(admin, "faq_101", limit=0), 422) == "VALIDATION_ERROR"
    public = admin.client.get("/api/v1/faqs/faq_101").json()
    assert set(public) == {"id", "question", "answer", "category"}
    assert admin.client.get("/api/v1/faqs/faq_101/revisions").status_code == 404


def test_history_rows_hold_no_credentials(admin: AdminApp) -> None:
    admin.patch(f"{FAQS}/faq_101", {"expected_version": 1, "answer": "Changed."})
    with admin.database.read() as db:
        rows = db.execute(text("SELECT * FROM faq_revisions")).fetchall()
    dump = str(rows)
    assert ALEX not in dump and MARIA not in dump and "stf_" not in dump
    assert "alex" in dump  # the staff ID is recorded


def test_the_database_keeps_one_revision_per_version(admin: AdminApp) -> None:
    from sqlalchemy.exc import IntegrityError

    statement = text(
        "INSERT INTO faq_revisions (revision_id, faq_id, version, action, changed_fields, "
        "previous_values, new_values, changed_by, changed_at) VALUES (:id, 'faq_101', 1, "
        "'created', :fields, NULL, '{}', NULL, '2026-10-17 00:00:00')"
    )
    with pytest.raises(IntegrityError), admin.database.write() as db:
        db.execute(statement, {"id": "0" * 31 + "1", "fields": '["answer"]'})


# --- Chat while FAQs change (Tasks 8 and 9) ----------------------------------------------


TRACK_QUESTION = "I want to track my parcel"  # routed to faq_101 by the scripted encoder


def test_chat_uses_the_latest_committed_faqs(admin: AdminApp) -> None:
    first = admin.chat(TRACK_QUESTION)
    assert first["decision"] == "answered" and first["sources"][0]["faq_id"] == "faq_101"

    admin.patch(f"{FAQS}/faq_101", {"expected_version": 1, "answer": "Use the new tracker."})
    assert admin.chat(TRACK_QUESTION)["answer"] == "Use the new tracker."

    admin.post(f"{FAQS}/faq_101/deactivate", {"expected_version": 2})
    after = admin.chat(TRACK_QUESTION)
    assert "faq_101" not in [source["faq_id"] for source in after["sources"]]
    assert "Use the new tracker." not in after["answer"]


def test_a_deactivation_during_a_chat_request_is_never_answered(admin: AdminApp) -> None:
    service = admin.app.state.faq_management_service
    admin.encoder.query_hooks.append(
        lambda: service.deactivate_faq(StaffMember("alex", StaffRole.ADMIN), "faq_101", 1)
    )

    reply = admin.chat(TRACK_QUESTION)

    assert admin.faq("faq_101")["is_active"] is False  # the change really happened mid-turn
    assert "faq_101" not in [source["faq_id"] for source in reply["sources"]]
    assert reply["answer"] != admin.faq("faq_101")["answer"]


def test_an_edit_during_a_chat_request_gives_the_new_answer_not_a_mix(admin: AdminApp) -> None:
    service = admin.app.state.faq_management_service
    admin.encoder.query_hooks.append(
        lambda: service.update_faq(
            StaffMember("alex", StaffRole.ADMIN),
            "faq_101",
            FAQChanges(expected_version=1, answer="Edited during the request."),
        )
    )

    reply = admin.chat(TRACK_QUESTION)

    assert reply["decision"] == "answered"
    assert reply["answer"] == "Edited during the request."
