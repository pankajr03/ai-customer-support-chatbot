"""Retrieval stays consistent while staff edit FAQs (Stage 11, Tasks 8 and 9).

A real database catalogue, the real retrieval and decision services, and a deterministic
fake encoder whose hooks can commit an FAQ change *while* a search or an answer is in
progress, the race the consistency mechanism exists for (see app.services.faq_catalog).
"""

import threading
from collections.abc import Callable
from typing import Any

import numpy as np
import pytest

from app.core.config import Settings
from app.core.staff_auth import StaffMember, StaffRole
from app.database.connection import Database
from app.database.faq_repository import FAQRepository
from app.models.faq_admin import FAQChanges
from app.models.generation import FallbackReason, GenerationOutcome
from app.repositories.faq_repository import DatabaseFAQRepository
from app.services.answer_decision_service import Decision, RetrievalMode
from app.services.context_builder import ContextBuilder
from app.services.embedding_service import EmbeddingError, EmbeddingService
from app.services.faq_catalog import CatalogSnapshot
from app.services.faq_management_service import FAQManagementService
from app.services.faq_service import FAQService
from app.services.grounded_answer_service import GroundedAnswerService
from app.services.llm_service import LLMService
from app.services.search_pipeline import SearchServices, build_search_services
from app.services.semantic_search_service import SemanticSearchService
from tests.fakes import FAKE_MODEL_NAME, FakeEncoder

ADMIN = StaffMember("alex", StaffRole.ADMIN)
TRACK = "How can I track my order?"  # faq_101's exact question: answered deterministically


class HookedEncoder(FakeEncoder):
    """A fake encoder that can fail, and run hooks when a query (one text) is encoded."""

    def __init__(self) -> None:
        super().__init__()
        self.fail = False
        self.query_hooks: list[Callable[[], None]] = []

    def encode(self, sentences: list[str], **kwargs: Any) -> np.ndarray:
        if self.fail:
            raise RuntimeError("model broken")
        if len(sentences) == 1 and self.query_hooks:
            self.query_hooks.pop(0)()
        return super().encode(sentences, **kwargs)


class Stack:
    def __init__(self, faq_repository: FAQRepository, settings: Settings) -> None:
        database = Database("sqlite://")
        database.init_schema()
        self.repo = DatabaseFAQRepository(database)
        self.repo.import_faqs(faq_repository.list_all())
        self.faqs = FAQService(self.repo)
        self.encoder = HookedEncoder()
        self.semantic = SemanticSearchService(
            self.faqs.list_active_faqs,
            EmbeddingService(FAKE_MODEL_NAME, loader=lambda name: self.encoder),
            catalog_revision=self.faqs.catalog_revision,
        )
        self.search: SearchServices = build_search_services(
            settings, self.faqs.list_active_faqs, self.semantic, catalog=self.faqs
        )
        self.management = FAQManagementService(self.repo, on_change=[self.semantic.refresh])
        self.grounded = GroundedAnswerService(
            self.search.decision,
            ContextBuilder.from_settings(settings),
            LLMService.from_settings(settings),
            current_faq=lambda faq_id: self.faqs.latest().get(faq_id),
        )

    def edit(self, faq_id: str, **fields: Any) -> None:
        version = self.repo.get_record(faq_id).version
        self.management.update_faq(ADMIN, faq_id, FAQChanges(expected_version=version, **fields))

    def batch_texts(self) -> list[list[str]]:
        return [
            batch for batch, _ in self.encoder.encode_calls if len(batch) > 1 or batch[0] != TRACK
        ]


@pytest.fixture
def stack(faq_repository: FAQRepository, make_settings: Callable[..., Settings]) -> Stack:
    return Stack(faq_repository, make_settings())


# --- Snapshots and pinning ----------------------------------------------------------------


def test_the_revision_changes_only_with_the_content(stack: Stack) -> None:
    first = stack.faqs.latest()
    stack.repo.reload()
    assert stack.faqs.latest() is first  # nothing changed: same snapshot
    stack.edit("faq_101", answer="New answer.")
    assert stack.faqs.latest().revision == first.revision + 1


def test_a_pinned_snapshot_is_what_every_read_returns(stack: Stack) -> None:
    old = stack.faqs.latest()
    with stack.faqs.pinned(old):
        stack.edit("faq_101", is_active=False)
        assert stack.faqs.get_active_faq("faq_101") is not None  # still the pinned view
        assert stack.faqs.catalog_revision() == old.revision
    assert stack.faqs.get_active_faq("faq_101") is None
    assert not stack.faqs.is_current(old.by_id["faq_101"])


def test_snapshots_compare_content_not_identity(faq_repository: FAQRepository) -> None:
    snapshot = CatalogSnapshot.of(faq_repository.list_all(), revision=4)
    faq = snapshot.active[0]
    assert snapshot.is_current(faq.model_copy())
    assert not snapshot.is_current(faq.model_copy(update={"answer": "Other."}))


# --- Semantic index follows the catalogue ---------------------------------------------------


def test_the_index_tracks_the_revision_and_embeds_only_changed_questions(stack: Stack) -> None:
    index = stack.semantic.get_index()
    calls = len(stack.encoder.encode_calls)

    stack.edit("faq_101", answer="A different answer.", keywords=["parcel"])  # no question change
    rebuilt = stack.semantic.get_index()
    assert rebuilt.catalog_revision == stack.faqs.latest().revision != index.catalog_revision
    assert len(stack.encoder.encode_calls) == calls  # nothing embedded, model not called
    assert rebuilt.get_faq("faq_101").answer == "A different answer."

    stack.edit("faq_101", question="Where can I follow my parcel?")
    stack.semantic.get_index()
    assert stack.encoder.encode_calls[-1][0] == ["Where can I follow my parcel?"]  # only that one


def test_deactivated_faqs_leave_the_index_and_return_without_embedding(stack: Stack) -> None:
    stack.semantic.get_index()
    builds = len(stack.batch_texts())

    stack.edit("faq_101", is_active=False)
    assert "faq_101" not in stack.semantic.get_index().faq_ids
    assert all(m.faq.id != "faq_101" for m in stack.semantic.search(TRACK, top_k=10))

    stack.edit("faq_101", is_active=True)
    assert "faq_101" in stack.semantic.get_index().faq_ids
    assert len(stack.batch_texts()) == builds  # no FAQ re-embedded: vectors were cached


def test_refresh_updates_a_built_index_and_never_loads_the_model_early(stack: Stack) -> None:
    stack.edit("faq_101", answer="Changed before any search.")
    assert stack.encoder.encode_calls == []  # refresh ran, but no index existed yet

    index = stack.semantic.get_index()
    stack.edit("faq_101", answer="Changed again.")
    assert stack.semantic._index is not index  # refreshed by the change itself
    assert stack.semantic._index.get_faq("faq_101").answer == "Changed again."


def test_a_failed_rebuild_never_serves_the_stale_index(stack: Stack) -> None:
    stack.semantic.get_index()
    stack.encoder.fail = True
    stack.edit("faq_101", question="Where can I follow my parcel?")  # refresh fails, logged

    with pytest.raises(EmbeddingError):
        stack.semantic.get_index()
    decision = stack.search.decision.decide("Where can I follow my parcel?")
    assert decision.retrieval_mode is RetrievalMode.KEYWORD_ONLY  # current FAQs, keyword only
    if decision.faq is not None:
        assert stack.faqs.is_current(decision.faq)

    stack.encoder.fail = False
    assert stack.semantic.get_index().get_faq("faq_101").question == (
        "Where can I follow my parcel?"
    )


def test_an_older_pinned_request_gets_its_own_index_without_replacing_the_new_one(
    stack: Stack,
) -> None:
    old = stack.faqs.latest()
    stack.semantic.get_index()
    stack.edit("faq_101", answer="Newest.")
    newest = stack.semantic.get_index()

    with stack.faqs.pinned(old):
        for_old = stack.semantic.get_index()
    assert for_old.catalog_revision == old.revision
    assert for_old.get_faq("faq_101") == old.by_id["faq_101"]
    assert stack.semantic.get_index() is newest  # the published index was kept


# --- Decisions and answers during changes ---------------------------------------------------


def test_a_change_during_retrieval_is_decided_again_on_the_new_catalogue(stack: Stack) -> None:
    stack.encoder.query_hooks.append(lambda: stack.edit("faq_101", answer="Edited mid-search."))

    decision = stack.search.decision.decide(TRACK)

    assert decision.decision is Decision.ANSWERED
    assert decision.faq is not None and decision.faq.answer == "Edited mid-search."
    assert decision.message == "Edited mid-search."


def test_a_deactivation_during_retrieval_is_never_answered(stack: Stack) -> None:
    stack.encoder.query_hooks.append(lambda: stack.edit("faq_101", is_active=False))

    decision = stack.search.decision.decide(TRACK)

    assert decision.faq is None or decision.faq.id != "faq_101"


def test_a_catalogue_that_keeps_changing_gives_the_safe_message(stack: Stack) -> None:
    answers = iter(["One.", "Two.", "Three."])
    stack.encoder.query_hooks.extend(
        [lambda: stack.edit("faq_101", answer=next(answers)) for _ in range(3)]
    )

    decision = stack.search.decision.decide(TRACK)

    assert decision.decision is Decision.RETRIEVAL_UNAVAILABLE
    assert decision.message == stack.search.decision.messages.unavailable
    assert decision.faq is None


def test_evidence_changed_after_the_decision_is_not_served(stack: Stack) -> None:
    decide = stack.search.decision.decide

    def decide_then_edit(*args: Any, **kwargs: Any) -> Any:
        decision = decide(*args, **kwargs)  # e.g. while the LLM is still writing
        stack.edit("faq_101", is_active=False)
        return decision

    stack.search.decision.decide = decide_then_edit  # type: ignore[method-assign]
    answer = stack.grounded.answer(TRACK)

    assert answer.result.outcome is GenerationOutcome.RETRIEVAL_UNAVAILABLE
    assert answer.result.source_faq_ids == ()
    assert answer.result.answer == stack.search.decision.messages.unavailable
    assert answer.result.diagnostics.fallback_reason is FallbackReason.KNOWLEDGE_CHANGED


def test_unchanged_evidence_is_served_normally(stack: Stack) -> None:
    stack.edit("faq_102", answer="An unrelated FAQ changed.")
    answer = stack.grounded.answer(TRACK)
    assert answer.result.outcome is GenerationOutcome.APPROVED_ANSWER
    assert answer.result.source_faq_ids == ("faq_101",)


def test_concurrent_edits_never_mix_versions_in_one_answer(stack: Stack) -> None:
    """Questions and answers are edited together (Qn with An); every answer given while
    that happens must pair a question with its own answer and be a real version."""
    versions = [(f"How can I track my order {n}?", f"Answer version {n}.") for n in range(6)]
    pairs = dict(versions) | {TRACK: stack.repo.get_record("faq_101").answer}
    problems: list[str] = []
    stop = threading.Event()

    def editor() -> None:
        for question, answer in versions * 3:
            stack.edit("faq_101", question=question, answer=answer)
        stop.set()

    def asker() -> None:
        while not stop.is_set():
            result = stack.grounded.answer("track my order")
            decision = result.decision
            if decision.faq is not None and decision.faq.id == "faq_101":
                if pairs.get(decision.faq.question) != decision.faq.answer:
                    problems.append("mixed versions")
            for record in result.context.records:
                if record.faq_id == "faq_101" and pairs.get(record.question) != record.answer:
                    problems.append("mixed evidence")

    threads = [threading.Thread(target=asker) for _ in range(3)] + [threading.Thread(target=editor)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert problems == []
