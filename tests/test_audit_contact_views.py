"""Stage 12 (Task 22, SEC-05): staff reads of customer contact details are audited, and
the audit table survives the schema upgrade that adds the new action.

The upgrade test builds a database file whose audit table predates the new action (its
CHECK constraint does not allow it), with an event and the append-only triggers, then
opens it with the current code.
"""

import sqlite3
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.database.connection import Database
from app.models.audit import AuditAction, AuditOutcome, AuditResource
from app.repositories.audit_repository import AuditActor, AuditRepository
from tests.test_admin_api import (  # noqa: F401 - fixtures
    ALEX,
    MARIA,
    TICKETS,
    AdminApp,
    make_admin_app,
)
from tests.test_admin_tickets_analytics import EMAIL, create_ticket

AUDIT = "/api/v1/admin/audit"
VIEWED = AuditAction.TICKET_CONTACT_VIEWED.value


@pytest.fixture
def api(make_admin_app: Callable[..., AdminApp]) -> AdminApp:  # noqa: F811
    return make_admin_app("contact-views", rate_limit_enabled=False)


def views(api: AdminApp) -> list[dict]:
    return api.get(AUDIT, params={"action": VIEWED}).json()["events"]


def test_every_staff_read_of_contact_details_is_recorded_without_the_details(
    api: AdminApp,
) -> None:
    ticket, _ = create_ticket(api)
    assert api.get(f"{TICKETS}/{ticket['ticket_id']}", token=MARIA).json()["contact"]
    api.get(f"{TICKETS}/{ticket['ticket_id']}", token=ALEX)

    events = views(api)
    assert [(e["actor_id"], e["resource_id"]) for e in reversed(events)] == [
        ("maria", ticket["ticket_id"]),
        ("alex", ticket["ticket_id"]),
    ]
    assert all(e["outcome"] == "success" and e["resource_type"] == "ticket" for e in events)
    log = api.get(AUDIT, params={"action": VIEWED}).text
    assert EMAIL not in log and "Example Customer" not in log


def test_lists_and_customer_reads_record_no_contact_view(api: AdminApp) -> None:
    ticket, token = create_ticket(api)
    api.get(TICKETS, token=MARIA)  # a list has no contact details
    api.client.get(
        f"{TICKETS}/{ticket['ticket_id']}",
        params={"session_id": ticket["session_id"]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert views(api) == []


def test_a_refused_read_is_not_a_view(api: AdminApp) -> None:
    ticket, _ = create_ticket(api)
    api.client.get(f"{TICKETS}/{ticket['ticket_id']}", headers={"Authorization": "Bearer x"})
    assert views(api) == []


# --- Upgrading a database from before the new action ---------------------------------------


def _downgrade_audit_table(path: Path) -> None:
    """Rewrite the audit table as an older version had it: the same columns and triggers,
    but a CHECK constraint that does not allow the new action."""
    connection = sqlite3.connect(path)
    try:
        current = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='admin_audit_events'"
        ).fetchone()[0]
        old_sql = current.replace(f"'{VIEWED}', ", "").replace(f", '{VIEWED}'", "")
        assert VIEWED not in old_sql
        triggers = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name='admin_audit_events'"
        ).fetchall()
        assert len(triggers) == 2
        connection.execute("PRAGMA foreign_keys = OFF")
        for name, _ in triggers:
            connection.execute(f'DROP TRIGGER "{name}"')
        connection.execute("ALTER TABLE admin_audit_events RENAME TO audit_tmp")
        connection.execute(old_sql)
        connection.execute("INSERT INTO admin_audit_events SELECT * FROM audit_tmp")
        connection.execute("DROP TABLE audit_tmp")
        for _, sql in triggers:
            connection.execute(sql)
        connection.commit()
    finally:
        connection.close()


def test_an_older_audit_table_is_upgraded_keeping_rows_and_append_only_triggers(
    tmp_path: Path,
) -> None:
    path = tmp_path / "old.db"
    url = f"sqlite:///{path.as_posix()}"
    database = Database(url)
    database.init_schema()
    AuditRepository(database).record(
        actor=AuditActor.staff("alex"),
        action=AuditAction.FAQ_UPDATED,
        resource_type=AuditResource.FAQ,
        resource_id="faq_001",
        outcome=AuditOutcome.SUCCESS,
    )
    database.dispose()
    _downgrade_audit_table(path)

    upgraded = Database(url)
    upgraded.init_schema()  # rebuilds the audit table for the new CHECK constraint
    audit = AuditRepository(upgraded)
    assert audit.record(
        actor=AuditActor.staff("maria"),
        action=AuditAction.TICKET_CONTACT_VIEWED,
        resource_type=AuditResource.TICKET,
        resource_id=str(uuid.uuid4()),
        outcome=AuditOutcome.SUCCESS,
    )
    events = audit.list_events().events
    assert [e.action.value for e in reversed(events)] == ["faq.updated", VIEWED]

    with upgraded.read() as db:
        triggers = db.execute(
            text(
                "SELECT count(*) FROM sqlite_master WHERE type='trigger' "
                "AND tbl_name='admin_audit_events'"
            )
        ).scalar()
    assert triggers == 2
    for statement in (
        "UPDATE admin_audit_events SET actor_id = 'nobody'",
        "DELETE FROM admin_audit_events",
    ):
        with pytest.raises(SQLAlchemyError), upgraded.write() as db:
            db.execute(text(statement))
    upgraded.dispose()


def test_rebuilding_any_table_keeps_its_triggers(tmp_path: Path) -> None:
    """Opening an up-to-date database twice changes nothing (no rebuild, no lost guard)."""
    url = f"sqlite:///{(tmp_path / 'twice.db').as_posix()}"
    for _ in range(2):
        database = Database(url)
        database.init_schema()
        with database.read() as db:
            names = db.execute(
                text("SELECT name FROM sqlite_master WHERE type='trigger' ORDER BY name")
            ).scalars()
            assert list(names) == ["trg_audit_no_delete", "trg_audit_no_update"]
        database.dispose()
