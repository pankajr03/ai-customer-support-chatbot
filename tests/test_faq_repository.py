"""FAQRepository: loading, validation, error handling and queries."""

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from app.core.config import PROJECT_ROOT
from app.database.faq_repository import (
    DEFAULT_FAQ_PATH,
    MAX_REPORTED_ERRORS,
    DuplicateFAQIdError,
    FAQDataError,
    FAQFileNotFoundError,
    FAQFileReadError,
    FAQRepository,
    InvalidFAQDataError,
    InvalidFAQRecordError,
    MalformedFAQFileError,
    resolve_data_path,
)
from app.models.faq import FAQ, FAQ_EXAMPLE, FAQCategory

WriteFile = Callable[..., Path]


def record(faq_id: str, **overrides: Any) -> dict[str, Any]:
    """A valid raw FAQ record with a given id."""
    return {**FAQ_EXAMPLE, "keywords": list(FAQ_EXAMPLE["keywords"]), "id": faq_id, **overrides}


@pytest.fixture
def write_file(tmp_path: Path) -> WriteFile:
    """Write JSON data (or raw text/bytes) to a temporary FAQ file and return its path."""

    def _write(content: object, name: str = "faqs.json") -> Path:
        path = tmp_path / name
        if isinstance(content, bytes):
            path.write_bytes(content)
        elif isinstance(content, str):
            path.write_text(content, encoding="utf-8")
        else:
            path.write_text(json.dumps(content), encoding="utf-8")
        return path

    return _write


@pytest.fixture(scope="module")
def repository() -> FAQRepository:
    """The real sample knowledge base."""
    return FAQRepository.from_json_file()


# --- Loading the real knowledge base ---------------------------------------------


def test_loads_default_knowledge_base(repository: FAQRepository) -> None:
    assert len(repository) == 30
    assert repository.source == DEFAULT_FAQ_PATH.resolve()


@pytest.mark.parametrize("path", ["data/faqs.json", Path("data") / "faqs.json"])
def test_relative_path_resolves_from_project_root_not_cwd(
    path: str | Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # simulate launching from an unrelated directory

    assert len(FAQRepository.from_json_file(path)) == 30
    assert len(FAQRepository.from_json_file()) == 30


def test_resolve_data_path() -> None:
    absolute = (PROJECT_ROOT / "data" / "faqs.json").resolve()

    assert resolve_data_path("data/faqs.json") == absolute
    assert resolve_data_path(absolute) == absolute


def test_load_is_logged(write_file: WriteFile, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="app.database.faq_repository"):
        FAQRepository.from_json_file(write_file([record("faq_001"), record("faq_002")]))

    assert "Loaded 2 FAQs (2 active) from faqs.json" in caplog.text


def test_empty_list_loads_as_empty_repository(write_file: WriteFile) -> None:
    repository = FAQRepository.from_json_file(write_file([]))

    assert len(repository) == 0
    assert repository.list_all() == []


def test_utf8_bom_is_accepted(write_file: WriteFile) -> None:
    path = write_file(b"\xef\xbb\xbf" + json.dumps([record("faq_001")]).encode("utf-8"))

    assert len(FAQRepository.from_json_file(path)) == 1


# --- Queries --------------------------------------------------------------------


def test_list_all_returns_every_faq_in_file_order(repository: FAQRepository) -> None:
    faqs = repository.list_all()

    assert [faq.id for faq in faqs] == [f"faq_{n:03d}" for n in range(1, 31)]
    assert all(isinstance(faq, FAQ) for faq in faqs)


def test_returned_lists_are_copies(repository: FAQRepository) -> None:
    repository.list_all().clear()
    repository.list_active().clear()

    assert len(repository.list_all()) == 30


def test_list_active_excludes_inactive(repository: FAQRepository) -> None:
    active_ids = {faq.id for faq in repository.list_active()}

    assert len(active_ids) == 29
    assert "faq_012" not in active_ids


@pytest.mark.parametrize("faq_id", ["faq_001", "FAQ_001", "  faq_001 "])
def test_get_by_id(repository: FAQRepository, faq_id: str) -> None:
    faq = repository.get_by_id(faq_id)

    assert faq is not None
    assert faq.question == "How can I track my order?"


def test_get_by_id_returns_inactive_faq(repository: FAQRepository) -> None:
    faq = repository.get_by_id("faq_012")

    assert faq is not None
    assert faq.is_active is False


@pytest.mark.parametrize("faq_id", ["faq_999", "", "not-an-id"])
def test_get_by_id_returns_none_when_missing(repository: FAQRepository, faq_id: str) -> None:
    assert repository.get_by_id(faq_id) is None


@pytest.mark.parametrize("category", [FAQCategory.SHIPPING, "shipping", " Shipping "])
def test_list_by_category(repository: FAQRepository, category: FAQCategory | str) -> None:
    faqs = repository.list_by_category(category)

    assert len(faqs) == 7
    assert all(faq.category is FAQCategory.SHIPPING for faq in faqs)


def test_list_by_category_active_only(repository: FAQRepository) -> None:
    faqs = repository.list_by_category("shipping", active_only=True)

    assert len(faqs) == 6
    assert all(faq.is_active for faq in faqs)


def test_list_by_category_with_no_faqs_returns_empty(repository: FAQRepository) -> None:
    assert repository.list_by_category(FAQCategory.TECHNICAL) == []


@pytest.mark.parametrize("category", ["accounts", "billing", ""])
def test_list_by_unknown_category_raises(repository: FAQRepository, category: str) -> None:
    with pytest.raises(ValueError, match="Unknown FAQ category"):
        repository.list_by_category(category)


def test_repository_constructor_rejects_duplicate_ids() -> None:
    faq = FAQ.model_validate(record("faq_001"))

    with pytest.raises(ValueError, match="Duplicate FAQ ids: faq_001"):
        FAQRepository([faq, faq])


# --- Missing and unreadable files ----------------------------------------------------


def test_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "nope.json"

    with pytest.raises(FAQFileNotFoundError, match="FAQ file not found") as exc:
        FAQRepository.from_json_file(missing)

    assert exc.value.path == missing.resolve()
    assert str(missing.resolve()) in str(exc.value)


def test_directory_instead_of_file(tmp_path: Path) -> None:
    with pytest.raises(FAQFileNotFoundError):
        FAQRepository.from_json_file(tmp_path)


def test_unreadable_file(write_file: WriteFile, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_file([record("faq_001")])

    def deny(*args: object, **kwargs: object) -> str:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "read_text", deny)

    with pytest.raises(FAQFileReadError, match="Permission denied"):
        FAQRepository.from_json_file(path)


# --- Malformed JSON ------------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        "",
        "   ",
        "[",
        '[{"id": "faq_001",}]',
        "[{'id': 'faq_001'}]",
        '[{"id": "faq_001"} {"id": "faq_002"}]',
        "not json at all",
    ],
)
def test_malformed_json(write_file: WriteFile, content: str) -> None:
    with pytest.raises(MalformedFAQFileError, match="not valid JSON") as exc:
        FAQRepository.from_json_file(write_file(content))

    assert exc.value.line is not None
    assert exc.value.column is not None


def test_malformed_json_reports_line_and_column(write_file: WriteFile) -> None:
    content = '[\n  {"id": "faq_001",\n   "question": }\n]'

    with pytest.raises(MalformedFAQFileError, match=r"line 3, column 16"):
        FAQRepository.from_json_file(write_file(content))


def test_invalid_utf8(write_file: WriteFile) -> None:
    with pytest.raises(MalformedFAQFileError, match="not valid UTF-8"):
        FAQRepository.from_json_file(write_file(b'[{"id": "faq_\xff\xfe"}]'))


def test_duplicate_keys_in_a_record_are_rejected(write_file: WriteFile) -> None:
    content = '[{"id": "faq_001", "is_active": true, "is_active": false}]'

    with pytest.raises(MalformedFAQFileError, match="duplicate key.*is_active"):
        FAQRepository.from_json_file(write_file(content))


# --- Invalid structure and records ---------------------------------------------------


@pytest.mark.parametrize("content", [{"faqs": []}, "faq", 42, None, True])
def test_top_level_must_be_a_list(write_file: WriteFile, content: object) -> None:
    path = write_file(json.dumps(content))

    with pytest.raises(InvalidFAQDataError, match="must contain a JSON array") as exc:
        FAQRepository.from_json_file(path)

    assert type(exc.value) is InvalidFAQDataError


def test_invalid_records_are_all_reported(write_file: WriteFile) -> None:
    data = [
        record("faq_001"),
        record("faq_002", answer="   "),
        record("faq_003"),
        record("faq_004", category="refundz", keywords=[]),
    ]

    with pytest.raises(InvalidFAQRecordError) as exc:
        FAQRepository.from_json_file(write_file(data))

    reported = {(e["index"], e["faq_id"], e["field"]) for e in exc.value.errors}
    assert reported == {
        (1, "faq_002", "answer"),
        (3, "faq_004", "category"),
        (3, "faq_004", "keywords"),
    }
    assert "record 1 (id='faq_002') answer: Value error, must not be empty" in str(exc.value)


@pytest.mark.parametrize("bad_record", ["just a string", 7, None, ["a", "list"]])
def test_non_object_record(write_file: WriteFile, bad_record: object) -> None:
    with pytest.raises(InvalidFAQRecordError) as exc:
        FAQRepository.from_json_file(write_file([record("faq_001"), bad_record]))

    assert exc.value.errors[0]["index"] == 1
    assert exc.value.errors[0]["faq_id"] is None
    assert exc.value.errors[0]["field"] == "(record)"


def test_missing_required_field(write_file: WriteFile) -> None:
    bad = record("faq_001")
    del bad["answer"]

    with pytest.raises(InvalidFAQRecordError, match="answer: Field required"):
        FAQRepository.from_json_file(write_file([bad]))


def test_error_message_is_capped_but_all_errors_kept(write_file: WriteFile) -> None:
    data = [record(f"faq_{n:03d}", answer="") for n in range(1, MAX_REPORTED_ERRORS + 6)]

    with pytest.raises(InvalidFAQRecordError) as exc:
        FAQRepository.from_json_file(write_file(data))

    assert len(exc.value.errors) == MAX_REPORTED_ERRORS + 5
    assert "... and 5 more" in str(exc.value)


def test_error_message_does_not_echo_record_contents(write_file: WriteFile) -> None:
    secret_answer = "INTERNAL NOTE do not show customers " + "x" * 6000

    with pytest.raises(InvalidFAQRecordError) as exc:
        FAQRepository.from_json_file(write_file([record("faq_001", answer=secret_answer)]))

    assert "INTERNAL NOTE" not in str(exc.value)


def test_duplicate_ids(write_file: WriteFile) -> None:
    data = [record("faq_001"), record("faq_002"), record("FAQ_001"), record("faq_002")]

    with pytest.raises(DuplicateFAQIdError, match="Duplicate FAQ ids: faq_001, faq_002") as exc:
        FAQRepository.from_json_file(write_file(data))

    assert exc.value.duplicate_ids == ["faq_001", "faq_002"]


def test_nothing_is_loaded_when_any_record_is_invalid(write_file: WriteFile) -> None:
    """Corrupted records are never skipped: one bad record fails the whole load."""
    data = [record(f"faq_{n:03d}") for n in range(1, 30)] + [record("faq_030", question="")]

    with pytest.raises(InvalidFAQRecordError):
        FAQRepository.from_json_file(write_file(data))


@pytest.mark.parametrize(
    "error_class",
    [
        FAQFileNotFoundError,
        FAQFileReadError,
        MalformedFAQFileError,
        InvalidFAQDataError,
        InvalidFAQRecordError,
        DuplicateFAQIdError,
    ],
)
def test_all_loading_errors_share_a_base_class(error_class: type[Exception]) -> None:
    assert issubclass(error_class, FAQDataError)
