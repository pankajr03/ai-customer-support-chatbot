"""Versioned FAQ catalogue snapshots (Stage 11): one consistent FAQ state per retrieval.

Since staff can edit FAQs while customers chat, every part of a retrieval operation must
see the *same* FAQ versions: keyword search, the semantic index, the answer decision and
the evidence given to the answer generator. Otherwise an answer could combine an
embedding of one version of an FAQ with the answer text of another, or be based on an
FAQ deactivated a moment ago.

The mechanism:

- ``CatalogSnapshot`` is an immutable state of the whole catalogue with a ``revision``
  number. The FAQ repository publishes a new snapshot (revision + 1) after every
  committed change; readers holding an older snapshot keep using it safely.
- A retrieval operation **pins** one snapshot (``FAQCatalog.pinned``, a context
  variable, so it applies to everything that operation calls in its thread and to
  nothing else). While pinned, every FAQ read returns that snapshot's FAQs.
- The semantic index records the revision it was built from, and is rebuilt when it
  differs from the pinned (or current) revision, so it never answers for another
  version (``SemanticSearchService``).
- After deciding, the decision service checks that the catalogue did not change
  meanwhile and otherwise decides again on the new snapshot
  (``AnswerDecisionService``). Before an answer leaves the pipeline, its evidence is
  checked against the then-current catalogue (``GroundedAnswerService``): an FAQ changed
  or deactivated in between gives the safe "retrieval unavailable" message instead.

Single process only: the snapshot lives in this process's memory. Another process (a
second API worker, the import script) changing the database is seen after a restart.
"""

from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol

from app.models.faq import FAQ


@dataclass(frozen=True)
class CatalogSnapshot:
    """The whole FAQ catalogue (active and inactive) at one revision."""

    revision: int
    faqs: tuple[FAQ, ...]
    by_id: Mapping[str, FAQ] = field(init=False, repr=False, compare=False)
    active: tuple[FAQ, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "by_id", MappingProxyType({faq.id: faq for faq in self.faqs}))
        object.__setattr__(self, "active", tuple(faq for faq in self.faqs if faq.is_active))

    @classmethod
    def of(cls, faqs: Sequence[FAQ], revision: int = 0) -> "CatalogSnapshot":
        return cls(revision=revision, faqs=tuple(faqs))

    def get(self, faq_id: str) -> FAQ | None:
        return self.by_id.get(faq_id.strip().lower())

    def is_current(self, faq: FAQ) -> bool:
        """Whether ``faq`` is active in this snapshot with exactly this content."""
        stored = self.by_id.get(faq.id)
        return stored is not None and stored.is_active and stored == faq


class FAQCatalog(Protocol):
    """What retrieval needs from the FAQ catalogue (``FAQService`` implements it)."""

    def snapshot(self) -> CatalogSnapshot:
        """The pinned snapshot inside ``pinned``, otherwise the latest one."""
        ...

    def latest(self) -> CatalogSnapshot:
        """The latest committed snapshot, ignoring any pin."""
        ...

    def pinned(self, snapshot: CatalogSnapshot) -> AbstractContextManager[CatalogSnapshot]:
        """Make ``snapshot`` what every FAQ read returns, for the block's duration."""
        ...


class CatalogPin:
    """A per-context pin of one snapshot (used by ``FAQService``)."""

    def __init__(self, name: str) -> None:
        self._var: ContextVar[CatalogSnapshot | None] = ContextVar(name, default=None)

    def get(self) -> CatalogSnapshot | None:
        return self._var.get()

    @contextmanager
    def pinned(self, snapshot: CatalogSnapshot) -> Iterator[CatalogSnapshot]:
        token = self._var.set(snapshot)
        try:
            yield snapshot
        finally:
            self._var.reset(token)
