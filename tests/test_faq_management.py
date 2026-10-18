"""Database FAQ repository, management service and import script (Stage 11, Tasks 4-6)."""

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.staff_auth import StaffMember, StaffPermissionError, StaffRole
from app.database.connection import Database
from app.database.faq_repository import FAQRepository
from app.models.faq import FAQ
from app.models.faq_admin import FAQChanges, FAQDraft, FAQFilter
from app.repositories.faq_repository import (
    DatabaseFAQRepository,
    DuplicateFAQQuestionError,
    FAQNotFoundError,
    FAQStoreError,
    FAQVersionConflictError,
    LastActiveFAQError,
)
from app.services.faq_management_service import FAQManagementService
from scripts import import_faqs

NOW = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)
ADMIN = StaffMember("alex", StaffRole.ADMIN)
SUPPORT = StaffMember("maria", StaffRole.SUPPORT)
DRAFT = FAQDraft(
    question="Do you ship to Norway?",
    answer="Yes, within 5-7 working days.",
    category="shipping",
    keywords=["norway"],
)


@pytest.fixture
def database() -> Database:
    db = Database("sqlite://")
    db.init_schema()
    return db


@pytest.fixture
def repository(database: Database, faq_repository: FAQRepository) -> DatabaseFAQRepository:
    repo = DatabaseFAQRepository(database)
    repo.import_faqs(faq_repository.list_all(), now=NOW)
    return repo


# --- Repository: retrieval interface ----------------------------------------------------


def test_the_retrieval_interface_matches_the_json_repository(
    repository: DatabaseFAQRepository, faq_repository: FAQRepository
) -> None:
    assert repository.list_all() == faq_repository.list_all()
    assert repository.list_active() == faq_repository.list_active()
    assert len(repository) == len(faq_repository)
    assert repository.get_by_id(" FAQ_101 ") == faq_repository.get_by_id("faq_101")
    assert repository.get_by_id("faq_999") is None
    assert repository.list_by_category("orders", active_only=True) == (
        faq_repository.list_by_category("orders", active_only=True)
    )


def test_ids_are_ordered_by_number(database: Database, faq_records: list[dict[str, Any]]) -> None:
    base = faq_records[0]
    faqs = [FAQ(**{**base, "id": faq_id}) for faq_id in ("faq_1000", "faq_010", "faq_002")]
    repo = DatabaseFAQRepository(database)
    repo.import_faqs(faqs)
    assert [faq.id for faq in repo.list_all()] == ["faq_002", "faq_010", "faq_1000"]


def test_the_snapshot_is_replaced_after_a_change(repository: DatabaseFAQRepository) -> None:
    before = repository.list_active()
    created = repository.create(DRAFT, actor_id="alex", now=NOW)
    assert before == repository.list_active()[:-1]
    assert repository.get_by_id(created.id) == created.to_faq()


def test_a_change_by_another_process_is_seen_after_reload(
    repository: DatabaseFAQRepository, database: Database
) -> None:
    other = DatabaseFAQRepository(database)  # e.g. the import script
    created = other.create(DRAFT, actor_id="alex", now=NOW)
    assert repository.get_by_id(created.id) is None
    repository.reload()
    assert repository.get_by_id(created.id) is not None


# --- Repository: management --------------------------------------------------------------


def test_new_ids_follow_the_highest_and_are_never_reused(repository: DatabaseFAQRepository) -> None:
    highest = max(int(faq.id[4:]) for faq in repository.list_all())
    first = repository.create(DRAFT, actor_id="alex", now=NOW)
    repository.update(first.id, FAQChanges(expected_version=1, is_active=False), actor_id="a")
    second = repository.create(
        DRAFT.model_copy(update={"question": "Do you ship to Iceland?"}), actor_id="alex", now=NOW
    )
    assert (first.id, second.id) == (f"faq_{highest + 1:03d}", f"faq_{highest + 2:03d}")


def test_a_second_faq_with_the_same_question_is_refused(repository: DatabaseFAQRepository) -> None:
    repository.create(DRAFT, actor_id="alex", now=NOW)
    copy = DRAFT.model_copy(update={"question": DRAFT.question.upper()})
    with pytest.raises(DuplicateFAQQuestionError):
        repository.create(copy, actor_id="alex", now=NOW)
    with pytest.raises(DuplicateFAQQuestionError):  # nor by editing another FAQ's question
        repository.update(
            "faq_102", FAQChanges(expected_version=1, question=DRAFT.question), actor_id="a"
        )


def test_updates_check_the_version(repository: DatabaseFAQRepository) -> None:
    later = NOW + timedelta(hours=1)
    edited = repository.update(
        "faq_101", FAQChanges(expected_version=1, answer="New."), actor_id="alex", now=later
    )
    assert (edited.version, edited.updated_at, edited.updated_by) == (2, later, "alex")
    with pytest.raises(FAQVersionConflictError) as caught:
        repository.update("faq_101", FAQChanges(expected_version=1, answer="X"), actor_id="b")
    assert (caught.value.expected, caught.value.current) == (1, 2)
    assert repository.get_record("faq_101").answer == "New."


def test_unknown_ids_are_not_found(repository: DatabaseFAQRepository) -> None:
    for faq_id in ("faq_999999", "nope", ""):
        with pytest.raises(FAQNotFoundError):
            repository.get_record(faq_id)
        with pytest.raises(FAQNotFoundError):
            repository.update(faq_id, FAQChanges(expected_version=1, answer="x"), actor_id="a")


def test_the_last_active_faq_stays_active(database: Database, faq_records: list[dict]) -> None:
    repo = DatabaseFAQRepository(database)
    repo.import_faqs([FAQ(**{**faq_records[0], "is_active": True})])
    with pytest.raises(LastActiveFAQError):
        repo.update(
            faq_records[0]["id"], FAQChanges(expected_version=1, is_active=False), actor_id="a"
        )


def test_search_filters_and_pages(repository: DatabaseFAQRepository) -> None:
    everything = repository.list_records(limit=100)
    assert everything.total == len(repository) and not everything.has_more
    inactive = repository.list_records(FAQFilter(is_active=False))
    assert inactive.total == len(repository) - len(repository.list_active())
    page = repository.list_records(offset=1, limit=2)
    assert [r.id for r in page.items] == [r.id for r in everything.items[1:3]]
    for wildcard in ("%", "_", "\\"):
        assert repository.list_records(FAQFilter(search=wildcard)).total == 0
    with pytest.raises(ValueError):
        repository.list_records(limit=101)


def test_store_failures_hide_their_cause(tmp_path: Path) -> None:
    repo = DatabaseFAQRepository(Database(f"sqlite:///{(tmp_path / 'x.db').as_posix()}"))
    with pytest.raises(FAQStoreError) as caught:  # no schema: the table does not exist
        repo.count()
    assert "SELECT" not in str(caught.value)


# --- Import -------------------------------------------------------------------------------


def test_import_is_idempotent_and_never_overwrites(
    repository: DatabaseFAQRepository, faq_repository: FAQRepository
) -> None:
    faqs = faq_repository.list_all()
    repository.update("faq_101", FAQChanges(expected_version=1, answer="Staff."), actor_id="a")

    report = repository.import_faqs(faqs)

    assert report.created == ()
    assert report.kept_database_version == ("faq_101",)
    assert len(report.unchanged) == len(faqs) - 1
    assert repository.get_record("faq_101").answer == "Staff."


def test_imported_records_keep_ids_and_metadata(
    repository: DatabaseFAQRepository, faq_repository: FAQRepository
) -> None:
    for faq in faq_repository.list_all():
        record = repository.get_record(faq.id)
        assert record.to_faq() == faq
        assert (record.version, record.created_at, record.created_by) == (1, NOW, None)
        assert record.created_at.tzinfo is not None


def test_a_dry_run_writes_nothing(database: Database, faq_repository: FAQRepository) -> None:
    repo = DatabaseFAQRepository(database)
    report = repo.import_faqs(faq_repository.list_all(), dry_run=True)
    assert len(report.created) == len(faq_repository) and repo.count() == 0


def test_import_refuses_repeated_ids(database: Database, faq_records: list[dict]) -> None:
    faq = FAQ(**faq_records[0])
    with pytest.raises(ValueError):
        DatabaseFAQRepository(database).import_faqs([faq, faq])


# --- Service --------------------------------------------------------------------------------


def test_support_staff_cannot_manage_faqs(repository: DatabaseFAQRepository) -> None:
    service = FAQManagementService(repository)
    calls: list[Callable[[], Any]] = [
        lambda: service.list_faqs(SUPPORT),
        lambda: service.get_faq(SUPPORT, "faq_101"),
        lambda: service.create_faq(SUPPORT, DRAFT),
        lambda: service.update_faq(SUPPORT, "faq_101", FAQChanges(expected_version=1, answer="x")),
        lambda: service.deactivate_faq(SUPPORT, "faq_101", 1),
        lambda: service.summary(SUPPORT),
    ]
    for call in calls:
        with pytest.raises(StaffPermissionError):
            call()
    assert repository.get_record("faq_101").version == 1


def test_listeners_run_after_changes_only(repository: DatabaseFAQRepository) -> None:
    changes: list[str] = []
    service = FAQManagementService(repository, on_change=[lambda: changes.append("x")])
    service.list_faqs(ADMIN, search="  ")  # blank search lists everything, no change
    current = service.get_faq(ADMIN, "faq_101")
    service.update_faq(ADMIN, "faq_101", FAQChanges(expected_version=1, answer=current.answer))
    assert changes == []
    service.create_faq(ADMIN, DRAFT)
    service.deactivate_faq(ADMIN, "faq_101", 1)
    service.activate_faq(ADMIN, "faq_101", 2)
    assert changes == ["x", "x", "x"]


def test_a_failing_listener_does_not_undo_the_change(repository: DatabaseFAQRepository) -> None:
    def broken() -> None:
        raise RuntimeError("index unavailable")

    created = FAQManagementService(repository, on_change=[broken]).create_faq(ADMIN, DRAFT)
    assert repository.get_record(created.id).question == DRAFT.question


@pytest.mark.parametrize(
    "fields",
    [
        {"question": ""},
        {"answer": " \n "},
        {"question": "q" * 501},
        {"keywords": ["", "x"]},
        {"keywords": ["x" * 51]},
        {"category": "unknown"},
        {"is_active": 1},
        {"version": 3},
    ],
)
def test_drafts_are_validated(fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        FAQDraft.model_validate({**DRAFT.model_dump(), **fields})


def test_tags_are_normalised() -> None:
    draft = FAQDraft.model_validate(
        {**DRAFT.model_dump(), "keywords": [" Norway ", "NORWAY", "Express   shipping"]}
    )
    assert draft.keywords == ["norway", "express shipping"]


def test_an_edit_must_change_something() -> None:
    with pytest.raises(ValidationError):
        FAQChanges(expected_version=1)
    with pytest.raises(ValidationError):
        FAQChanges(expected_version=0, answer="x")


# --- Import script ------------------------------------------------------------------------


@pytest.fixture
def run_import(
    make_settings: Callable[..., Settings], tmp_path: Path, faq_file: Path
) -> Callable[..., tuple[int, str]]:
    url = f"sqlite:///{(tmp_path / 'import.db').as_posix()}"

    def _run(*args: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
        settings = make_settings(database_url=url, faq_data_path=faq_file)
        code = import_faqs.main(list(args), settings=settings)
        captured = capsys.readouterr()
        return code, captured.out + captured.err

    return _run


def _stored(tmp_path: Path) -> int:
    with sqlite3.connect(tmp_path / "import.db") as connection:
        return int(connection.execute("SELECT count(*) FROM faqs").fetchone()[0])


def test_the_script_imports_once_then_changes_nothing(
    run_import: Callable[..., tuple[int, str]],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    faq_records: list[dict],
) -> None:
    code, out = run_import(capsys=capsys)
    assert code == 0 and f"Imported {len(faq_records)} new FAQ(s)." in out

    code, out = run_import(capsys=capsys)
    assert code == 0 and "Imported 0 new FAQ(s)." in out
    assert f"identical: {len(faq_records)}" in out
    assert _stored(tmp_path) == len(faq_records)


def test_the_script_dry_run_and_backup(
    run_import: Callable[..., tuple[int, str]],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(import_faqs, "BACKUP_DIR", tmp_path / "backups")
    code, out = run_import("--dry-run", capsys=capsys)
    assert code == 0 and "nothing written" in out and _stored(tmp_path) == 0

    run_import(capsys=capsys)
    code, out = run_import("--backup", capsys=capsys)
    backups = list((tmp_path / "backups").glob("import-*.db"))
    assert code == 0 and len(backups) == 1
    with sqlite3.connect(backups[0]) as connection:
        assert connection.execute("SELECT count(*) FROM faqs").fetchone()[0] == _stored(tmp_path)


@pytest.mark.parametrize(
    "records",
    [
        "not json",
        [{"id": "faq_001", "question": "Q?"}],  # missing fields
        "duplicates",
    ],
)
def test_the_script_refuses_invalid_files_and_writes_nothing(
    run_import: Callable[..., tuple[int, str]],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    faq_file: Path,
    faq_records: list[dict],
    records: Any,
) -> None:
    if records == "duplicates":
        faq_file.write_text(json.dumps([faq_records[0], faq_records[0]]), encoding="utf-8")
    elif isinstance(records, str):
        faq_file.write_text(records, encoding="utf-8")
    else:
        faq_file.write_text(json.dumps(records), encoding="utf-8")

    code, out = run_import(capsys=capsys)

    assert code == import_faqs.EXIT_INVALID_FILE and "nothing written" in out
    assert not (tmp_path / "import.db").exists()
