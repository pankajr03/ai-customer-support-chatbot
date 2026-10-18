"""Audit log, analytics endpoints and FAQ edge cases (Stage 11, Tasks 16-18), over HTTP
against the real app (fixtures from tests/test_admin_api.py)."""

import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError

from app.core.config import Settings
from app.core.staff_auth import StaffAuthenticator
from app.database.connection import Database
from app.database.faq_repository import FAQDataError
from app.main import create_app
from app.models.audit import AuditAction, AuditOutcome, AuditResource
from app.repositories.audit_repository import AuditActor, AuditRepository
from app.repositories.faq_repository import FAQStoreError
from tests.test_admin_api import (  # noqa: F401 - fixtures
    ADMIN,
    ALEX,
    FAQS,
    MARIA,
    NEW_FAQ,
    STAFF_HASHES,
    AdminApp,
    admin,
    bearer,
    error_code,
    make_admin_app,
)
from tests.test_admin_tickets_analytics import assign, create_ticket, set_status

AUDIT = f"{ADMIN}/audit"
TICKETS = "/api/v1/support/tickets"


def audit_events(api: AdminApp, **params: Any) -> list[dict[str, Any]]:
    response = api.get(AUDIT, params={"limit": 200, **params})
    assert response.status_code == 200, response.text
    return list(response.json()["events"])


# --- Audit log (Task 17) ----------------------------------------------------------------


def test_faq_changes_are_audited_with_the_real_actor_and_request(admin: AdminApp) -> None:  # noqa: F811
    created = admin.post(FAQS, NEW_FAQ)
    faq_id = created.json()["id"]
    admin.patch(f"{FAQS}/{faq_id}", {"expected_version": 1, "answer": "Changed."})
    admin.post(f"{FAQS}/{faq_id}/deactivate", {"expected_version": 2})
    admin.post(f"{FAQS}/{faq_id}/activate", {"expected_version": 3})

    events = audit_events(admin, resource_id=faq_id)

    assert [e["action"] for e in reversed(events)] == [
        "faq.created",
        "faq.updated",
        "faq.deactivated",
        "faq.activated",
    ]
    assert {(e["actor_type"], e["actor_id"], e["outcome"]) for e in events} == {
        ("staff", "alex", "success")
    }
    assert events[-1]["request_id"] == created.headers["X-Request-ID"]
    assert events[-2]["details"] == {"version": 2, "fields": ["answer"]}
    assert "Changed." not in str(events) and NEW_FAQ["answer"] not in str(events)


def test_ticket_changes_are_audited_without_personal_data(admin: AdminApp) -> None:  # noqa: F811
    ticket, _ = create_ticket(admin)
    tid = ticket["ticket_id"]
    assign(admin, tid, "maria", None, token=MARIA)
    set_status(admin, tid, "in_progress")
    admin.client.post(
        f"{TICKETS}/{tid}/notes",
        json={
            "body": "Customer phone 555-0100.",
            "client_note_id": str(uuid.uuid4()),
        },
        headers=bearer(MARIA),
    )

    events = audit_events(admin, resource_type="ticket", resource_id=tid)

    assert [(e["action"], e["actor_id"]) for e in reversed(events)] == [
        ("ticket.assignment_changed", "maria"),
        ("ticket.status_changed", "alex"),
        ("ticket.note_added", "maria"),
    ]
    assert events[1]["details"] == {"from": "assigned", "to": "in_progress"}
    dump = json.dumps(events)
    assert "555-0100" not in dump and "customer@example.com" not in dump
    assert "Refund" not in dump


def test_refused_staff_access_is_audited_without_tokens(admin: AdminApp) -> None:  # noqa: F811
    admin.client.get(FAQS)  # no token
    admin.client.get(FAQS, headers=bearer("stf_" + "x" * 43))  # unknown token
    admin.get(FAQS, token=MARIA)  # support role

    denied = audit_events(admin, action="auth.access_denied")

    reasons = [(e["actor_type"], e["actor_id"], e["details"]["reason"]) for e in reversed(denied)]
    assert reasons == [
        ("unauthenticated", None, "invalid_or_missing_staff_token"),
        ("unauthenticated", None, "invalid_or_missing_staff_token"),
        ("staff", "maria", "role_not_permitted"),
    ]
    assert denied[0]["details"]["permission"] == "manage_faqs"
    assert all(e["outcome"] == "denied" and e["resource_id"] == FAQS for e in denied)
    assert "stf_" not in json.dumps(denied)


def test_the_seed_import_is_audited_as_the_documented_service(admin: AdminApp) -> None:  # noqa: F811
    imported = audit_events(admin, action="faq.imported")
    assert [(e["actor_type"], e["actor_id"], e["resource_type"]) for e in imported] == [
        ("system", "faq-import", "faq_catalog")
    ]
    assert imported[0]["details"]["created"] > 0


def test_refused_changes_leave_no_success_event(admin: AdminApp) -> None:  # noqa: F811
    before = len(audit_events(admin, outcome="success"))
    admin.patch(f"{FAQS}/faq_101", {"expected_version": 7, "answer": "Stale."})  # 409
    admin.post(FAQS, {**NEW_FAQ, "category": "pets"})  # 422
    assert len(audit_events(admin, outcome="success")) == before


def test_the_audit_log_is_read_only_and_for_admins(admin: AdminApp) -> None:  # noqa: F811
    assert error_code(admin.get(AUDIT, token=MARIA), 403) == "STAFF_FORBIDDEN"
    assert admin.client.delete(AUDIT, headers=bearer(ALEX)).status_code == 405
    with pytest.raises(DBAPIError), admin.database.write() as db:
        db.execute(text("UPDATE admin_audit_events SET actor_id = 'someone-else'"))
    with pytest.raises(DBAPIError), admin.database.write() as db:
        db.execute(text("DELETE FROM admin_audit_events"))
    assert audit_events(admin)  # still there


def test_the_audit_log_filters_by_date(admin: AdminApp) -> None:  # noqa: F811
    today = datetime.now(UTC).date()
    assert audit_events(admin, start_date=today.isoformat(), end_date=today.isoformat())
    yesterday = (today - timedelta(days=1)).isoformat()
    assert audit_events(admin, end_date=yesterday) == []


def test_an_unwritable_audit_log_never_breaks_a_refusal(tmp_path: Path) -> None:
    repo = AuditRepository(Database(f"sqlite:///{(tmp_path / 'none.db').as_posix()}"))
    recorded = repo.record(  # no schema: the write fails, is logged and reported
        actor=AuditActor.unauthenticated(),
        action=AuditAction.ACCESS_DENIED,
        resource_type=AuditResource.ENDPOINT,
        resource_id="/x",
        outcome=AuditOutcome.DENIED,
    )
    assert recorded is False


# --- Analytics endpoints (Task 16) ----------------------------------------------------------


def test_the_summary_endpoint_and_its_alias_agree(admin: AdminApp) -> None:  # noqa: F811
    summary = admin.get(f"{ADMIN}/dashboard/summary").json()
    alias = admin.get(f"{ADMIN}/overview").json()
    summary.pop("generated_at"), alias.pop("generated_at")
    assert summary == alias


@pytest.mark.parametrize("path", ["analytics/chat", "analytics/tickets"])
def test_analytics_periods_are_validated(admin: AdminApp, path: str) -> None:  # noqa: F811
    url = f"{ADMIN}/{path}"
    today = datetime.now(UTC).date()
    bad = [
        {"days": 7, "start_date": today.isoformat()},
        {"start_date": today.isoformat(), "end_date": (today - timedelta(days=1)).isoformat()},
        {"end_date": (today + timedelta(days=1)).isoformat()},
        {"start_date": (today - timedelta(days=400)).isoformat()},
        {"days": 0},
        {"start_date": "not-a-date"},
    ]
    for params in bad:
        assert error_code(admin.get(url, params=params), 422) == "VALIDATION_ERROR", params
    default = admin.get(url).json()
    assert default["days"] == 30 and default["end_date"] == today.isoformat()
    ranged = admin.get(url, params={"start_date": "2026-01-01", "end_date": "2026-01-31"}).json()
    assert (ranged["days"], ranged["start_date"]) == (31, "2026-01-01")
    assert len(ranged["daily"]) == 31


def test_chat_analytics_respect_date_filters(admin: AdminApp) -> None:  # noqa: F811
    admin.chat("I want to track my parcel")
    today = datetime.now(UTC).date()
    url = f"{ADMIN}/analytics/chat"
    assert admin.get(url, params={"days": 1}).json()["chatbot_responses"] == 1
    yesterday = (today - timedelta(days=1)).isoformat()
    assert admin.get(url, params={"end_date": yesterday}).json()["chatbot_responses"] == 0


def test_ticket_analytics_count_volumes_resolutions_and_backlog(admin: AdminApp) -> None:  # noqa: F811
    resolved, _ = create_ticket(admin)
    create_ticket(admin, "sales")
    assign(admin, resolved["ticket_id"], "maria", None)
    set_status(admin, resolved["ticket_id"], "in_progress")
    set_status(admin, resolved["ticket_id"], "resolved")

    data = admin.get(f"{ADMIN}/analytics/tickets", params={"days": 1}).json()

    assert data["created"] == 2
    assert data["created_by_type"] == {"support": 1, "sales": 1}
    assert data["created_by_current_status"]["resolved"] == 1
    assert data["leads_by_category"]["bulk_order"] == 1
    assert data["resolved"] == 1 and data["closed"] == 0
    assert data["time_to_resolution"]["samples"] == 1
    assert data["time_to_resolution"]["median"] >= 0
    assert data["backlog"] == {
        "total": 1,
        "by_status": {"open": 1, "assigned": 0, "in_progress": 0, "waiting_for_customer": 0},
        "unassigned": 1,
        "oldest_created_at": data["backlog"]["oldest_created_at"],
    }
    assert (data["daily"][-1]["created"], data["daily"][-1]["resolved"]) == (2, 1)
    assert "customer@example.com" not in json.dumps(data) and "Refund" not in json.dumps(data)

    earlier = admin.get(
        f"{ADMIN}/analytics/tickets",
        params={"end_date": (datetime.now(UTC).date() - timedelta(days=1)).isoformat()},
    ).json()
    assert earlier["created"] == 0 and earlier["resolved"] == 0
    assert earlier["backlog"]["total"] == 1  # the backlog is always "now"


def test_ticket_analytics_on_an_empty_database(admin: AdminApp) -> None:  # noqa: F811
    data = admin.get(f"{ADMIN}/analytics/tickets").json()
    assert data["created"] == 0 and data["time_to_resolution"]["median"] is None
    assert data["backlog"]["oldest_created_at"] is None


def test_analytics_endpoints_are_for_admins(admin: AdminApp) -> None:  # noqa: F811
    for path in ("dashboard/summary", "analytics/chat", "analytics/tickets"):
        assert error_code(admin.get(f"{ADMIN}/{path}", token=MARIA), 403) == "STAFF_FORBIDDEN"
        assert error_code(admin.client.get(f"{ADMIN}/{path}"), 401) == "STAFF_AUTH_REQUIRED"


# --- FAQ edge cases (Task 18) ---------------------------------------------------------------


def test_a_failed_database_write_changes_nothing_and_says_so(
    admin: AdminApp,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = admin.app.state.faq_service.latest().revision

    def broken_write() -> Any:
        raise OperationalError("INSERT", {}, Exception("disk I/O error"))

    monkeypatch.setattr(admin.database, "write", broken_write)
    response = admin.post(FAQS, NEW_FAQ)

    assert error_code(response, 503) == "SERVICE_UNAVAILABLE"
    assert "disk" not in response.text and "INSERT" not in response.text
    monkeypatch.undo()
    assert admin.get(FAQS, params={"q": "norway"}).json()["total"] == 0
    assert admin.app.state.faq_service.latest().revision == revision


def test_a_change_that_cannot_be_read_back_pauses_answers_until_verified(
    admin: AdminApp,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = admin.app.state.faq_service.repository
    real_reload = repo.reload
    failures = {"left": 2}

    def flaky_reload() -> int:
        if failures["left"]:
            failures["left"] -= 1
            raise FAQStoreError("database unreadable")
        return real_reload()

    monkeypatch.setattr(repo, "reload", flaky_reload)
    deactivated = admin.post(f"{FAQS}/faq_101/deactivate", {"expected_version": 1})
    assert deactivated.status_code == 200  # committed (and audited)

    # The snapshot still has faq_101 active: answering from it would be unsafe.
    created = admin.client.post("/api/v1/conversations").json()
    chat = admin.client.post(
        f"/api/v1/conversations/{created['session_id']}/chat",
        json={"message": "I want to track my parcel"},
        headers=bearer(created["access_token"]),
    )
    assert error_code(chat, 503) == "SERVICE_UNAVAILABLE"
    assert "help articles" in chat.json()["error"]["message"]

    reply = admin.chat("I want to track my parcel")  # the reload now succeeds
    assert "faq_101" not in [s["faq_id"] for s in reply["sources"]]
    assert admin.client.get("/api/v1/ready").json()["checks"]["knowledge_base"] == "ok"


def test_history_keeps_replies_that_cite_a_deactivated_faq(admin: AdminApp) -> None:  # noqa: F811
    created = admin.client.post("/api/v1/conversations").json()
    headers = bearer(created["access_token"])
    base = f"/api/v1/conversations/{created['session_id']}"
    answer = admin.client.post(
        f"{base}/chat", json={"message": "I want to track my parcel"}, headers=headers
    ).json()
    assert answer["sources"][0]["faq_id"] == "faq_101"

    admin.post(f"{FAQS}/faq_101/deactivate", {"expected_version": 1})
    assert admin.client.delete(f"{FAQS}/faq_101", headers=bearer(ALEX)).status_code == 405

    history = admin.client.get(f"{base}/messages", headers=headers).json()["messages"]
    assert [m["content"] for m in history][-1] == answer["answer"]  # the reply is kept
    assert admin.faq("faq_101")["id"] == "faq_101"  # and the FAQ it cites still exists


def test_an_empty_seed_file_starts_with_no_answers_and_reports_not_ready(
    make_settings: Callable[..., Settings], tmp_path: Path
) -> None:
    empty = tmp_path / "empty.json"
    empty.write_text("[]", encoding="utf-8")
    from fastapi.testclient import TestClient

    app = create_app(
        make_settings(faq_data_path=empty, staff_token_hashes=STAFF_HASHES),
        database=Database("sqlite://"),
    )
    with TestClient(app) as client:
        assert client.get("/api/v1/ready").json()["checks"]["knowledge_base"] == "unavailable"
        assert client.get("/api/v1/faqs").json()["total"] == 0
        listing = client.get(FAQS, headers=bearer(ALEX)).json()
        assert listing["total"] == 0


def test_an_empty_table_without_a_seed_file_refuses_to_start(
    make_settings: Callable[..., Settings], tmp_path: Path
) -> None:
    with pytest.raises(FAQDataError):
        create_app(
            make_settings(faq_data_path=tmp_path / "missing.json"), database=Database("sqlite://")
        )


def test_a_revoked_staff_token_is_refused_at_once(admin: AdminApp) -> None:  # noqa: F811
    assert admin.get(FAQS).status_code == 200
    admin.app.state.staff_authenticator._hashes.pop("alex")  # the entry was removed
    assert error_code(admin.get(FAQS), 401) == "STAFF_AUTH_REQUIRED"
    admin.app.state.staff_authenticator = StaffAuthenticator(STAFF_HASHES)
