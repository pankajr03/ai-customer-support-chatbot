"""Load and query the FAQ knowledge base.

The JSON file in ``data/faqs.json`` is the source of truth for Stage 2. The whole
file is validated when it is loaded: any problem (missing file, malformed JSON,
an invalid record, duplicate IDs) raises a specific ``FAQDataError`` subclass.
Corrupted records are never skipped, because a partially loaded knowledge base
would give customers incomplete answers without anyone noticing.

Usage:
    repository = FAQRepository.from_json_file()          # data/faqs.json
    repository = FAQRepository.from_json_file("data/client_faqs.json")
    repository.get_by_id("faq_001")
"""

import json
import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from app.core.config import PROJECT_ROOT
from app.models.faq import FAQ, FAQCategory, find_duplicate_ids, parse_category

logger = logging.getLogger(__name__)

DEFAULT_FAQ_PATH = PROJECT_ROOT / "data" / "faqs.json"
MAX_REPORTED_ERRORS = 10

_FAQ_LIST_ADAPTER = TypeAdapter(list[FAQ])


# --- Exceptions -----------------------------------------------------------------


class FAQDataError(Exception):
    """Base class for every failure to load the FAQ knowledge base."""

    def __init__(self, message: str, path: Path) -> None:
        super().__init__(f"{message} [file: {path}]")
        self.path = path


class FAQFileNotFoundError(FAQDataError):
    """The FAQ file does not exist (or is not a regular file)."""


class FAQFileReadError(FAQDataError):
    """The FAQ file exists but could not be read (e.g. permissions)."""


class MalformedFAQFileError(FAQDataError):
    """The file is not valid UTF-8 JSON, or contains duplicate keys in a record."""

    def __init__(
        self, message: str, path: Path, line: int | None = None, column: int | None = None
    ) -> None:
        location = f" (line {line}, column {column})" if line is not None else ""
        super().__init__(f"{message}{location}", path)
        self.line = line
        self.column = column


class InvalidFAQDataError(FAQDataError):
    """The JSON is well-formed but does not describe a valid FAQ knowledge base."""


class InvalidFAQRecordError(InvalidFAQDataError):
    """One or more records failed FAQ validation. ``errors`` lists every problem."""

    def __init__(self, path: Path, errors: list[dict[str, Any]]) -> None:
        lines = [
            f"record {e['index']} (id={e['faq_id']!r}) {e['field']}: {e['message']}"
            for e in errors[:MAX_REPORTED_ERRORS]
        ]
        if len(errors) > MAX_REPORTED_ERRORS:
            lines.append(f"... and {len(errors) - MAX_REPORTED_ERRORS} more")
        super().__init__(f"{len(errors)} invalid FAQ field(s):\n  " + "\n  ".join(lines), path)
        self.errors = errors


class DuplicateFAQIdError(InvalidFAQDataError):
    """Two or more records share the same ID."""

    def __init__(self, path: Path, duplicate_ids: list[str]) -> None:
        super().__init__(f"Duplicate FAQ ids: {', '.join(duplicate_ids)}", path)
        self.duplicate_ids = duplicate_ids


# --- Path resolution ------------------------------------------------------------


def resolve_data_path(path: str | Path) -> Path:
    """Resolve ``path`` against the project root unless it is already absolute.

    This makes "data/faqs.json" mean the same file whether the app is started from
    the project root, from ``app/``, from VS Code or from a test runner.
    """
    candidate = Path(path).expanduser()
    return (candidate if candidate.is_absolute() else PROJECT_ROOT / candidate).resolve()


# --- Loading helpers --------------------------------------------------------------


def _read_text(path: Path) -> str:
    if not path.is_file():
        raise FAQFileNotFoundError("FAQ file not found", path)
    try:
        # utf-8-sig also accepts the byte-order mark some Windows editors add.
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise MalformedFAQFileError("FAQ file is not valid UTF-8 text", path) from exc
    except OSError as exc:
        raise FAQFileReadError(f"FAQ file could not be read: {exc.strerror}", path) from exc


def _parse_json(text: str, path: Path) -> object:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        # json.loads would otherwise keep only the last value for a repeated key.
        keys = [key for key, _ in pairs]
        if repeated := sorted({key for key in keys if keys.count(key) > 1}):
            raise ValueError(f"duplicate key(s) in one record: {', '.join(repeated)}")
        return dict(pairs)

    try:
        return json.loads(text, object_pairs_hook=reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise MalformedFAQFileError(
            f"FAQ file is not valid JSON: {exc.msg}", path, exc.lineno, exc.colno
        ) from exc
    except ValueError as exc:
        raise MalformedFAQFileError(f"FAQ file is malformed: {exc}", path) from exc


def _validate_records(data: object, path: Path) -> list[FAQ]:
    if not isinstance(data, list):
        raise InvalidFAQDataError(
            f"FAQ file must contain a JSON array of FAQ objects, not {type(data).__name__}",
            path,
        )
    try:
        faqs = _FAQ_LIST_ADAPTER.validate_python(data)
    except ValidationError as exc:
        raise InvalidFAQRecordError(path, _describe_errors(exc, data)) from exc

    if duplicates := find_duplicate_ids(faqs):
        raise DuplicateFAQIdError(path, duplicates)
    return faqs


def _describe_errors(exc: ValidationError, data: list[Any]) -> list[dict[str, Any]]:
    """Turn Pydantic errors into readable entries without echoing whole records."""
    described = []
    for error in exc.errors(include_input=False, include_url=False):
        index, *field_path = error["loc"]
        record = data[index] if isinstance(index, int) else None
        faq_id = record.get("id") if isinstance(record, dict) else None
        described.append(
            {
                "index": index,
                "faq_id": faq_id,
                "field": ".".join(str(part) for part in field_path) or "(record)",
                "message": error["msg"],
            }
        )
    return described


# --- Repository ---------------------------------------------------------------------


class FAQRepository:
    """Read-only, in-memory access to a validated set of FAQs."""

    def __init__(self, faqs: Iterable[FAQ], source: Path | None = None) -> None:
        self._faqs: tuple[FAQ, ...] = tuple(faqs)
        if duplicates := find_duplicate_ids(self._faqs):
            raise ValueError(f"Duplicate FAQ ids: {', '.join(duplicates)}")
        self._by_id: dict[str, FAQ] = {faq.id: faq for faq in self._faqs}
        self.source = source

    @classmethod
    def from_json_file(cls, path: str | Path = DEFAULT_FAQ_PATH) -> "FAQRepository":
        """Load, validate and index FAQs from a JSON file.

        Raises:
            FAQFileNotFoundError: the file does not exist.
            FAQFileReadError: the file cannot be read.
            MalformedFAQFileError: the file is not valid UTF-8 JSON.
            InvalidFAQDataError: the JSON is not an array of FAQ objects.
            InvalidFAQRecordError: one or more records are invalid.
            DuplicateFAQIdError: two or more records share an ID.
        """
        resolved = resolve_data_path(path)
        faqs = _validate_records(_parse_json(_read_text(resolved), resolved), resolved)
        repository = cls(faqs, source=resolved)
        logger.info(
            "Loaded %d FAQs (%d active) from %s",
            len(repository),
            len(repository.list_active()),
            resolved.name,
        )
        return repository

    def __len__(self) -> int:
        return len(self._faqs)

    def list_all(self) -> list[FAQ]:
        """Every FAQ, active or not, in file order."""
        return list(self._faqs)

    def list_active(self) -> list[FAQ]:
        """FAQs available for retrieval, in file order."""
        return [faq for faq in self._faqs if faq.is_active]

    def get_by_id(self, faq_id: str) -> FAQ | None:
        """Return the FAQ with this ID (case-insensitive), or None if there is none."""
        return self._by_id.get(faq_id.strip().lower())

    def list_by_category(
        self, category: FAQCategory | str, *, active_only: bool = False
    ) -> list[FAQ]:
        """FAQs in a category, in file order.

        Raises:
            ValueError: ``category`` is not a known FAQ category.
        """
        wanted = parse_category(category)
        return [
            faq
            for faq in self._faqs
            if faq.category is wanted and (faq.is_active or not active_only)
        ]
