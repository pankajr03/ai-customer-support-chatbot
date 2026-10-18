"""Stage 12 (Task 16): FAQ update consistency end to end, through the public API.

Lower-level guarantees (catalogue revisions, incremental re-embedding, never serving a
stale index, changes committed during retrieval, versions never mixed in one answer)
are tested in ``tests/test_faq_consistency.py`` and ``tests/test_admin_api.py``. Here a
staff member's edits are followed through every customer-facing retrieval route
(keyword search, semantic search, hybrid search, chat) of the real application, with
the deterministic ``LexicalEncoder`` (so a changed question really changes its vector).
"""

import threading
from typing import Any

import numpy as np
import pytest

from app.core.staff_auth import StaffMember, StaffRole
from app.models.faq_admin import FAQChanges
from app.repositories.faq_repository import FAQVersionConflictError
from app.services.embedding_service import EmbeddingService
from scripts.ai_eval_harness import API, EvalApp, LexicalEncoder, eval_app

NEW = {
    "question": "Do you deliver to Norway?",
    "answer": "Yes. Orders to Norway usually arrive within 5-7 working days.",
    "category": "shipping",
    "keywords": ["norway"],
}


class SwitchableEncoder(LexicalEncoder):
    """Lexical vectors, or a failure while ``failing`` is set (an embedding outage)."""

    failing = False

    def encode(self, sentences: list[str], **kwargs: Any) -> np.ndarray:
        if self.failing:
            raise RuntimeError("simulated embedding failure")
        return super().encode(sentences, **kwargs)


@pytest.fixture
def encoder() -> SwitchableEncoder:
    return SwitchableEncoder()


@pytest.fixture
def ev(encoder: SwitchableEncoder) -> Any:
    embeddings = EmbeddingService("switchable-lexical", loader=lambda name: encoder)
    with eval_app(embeddings, llm_enabled=False, query_rewrite_use_llm=False) as app:
        yield app


def found(ev: EvalApp, faq_id: str, query: str) -> dict[str, bool]:
    """Whether each retrieval route returns ``faq_id`` for ``query``."""
    keyword = ev.client.get(f"{API}/faqs/search", params={"q": query}).json()["results"]
    semantic = ev.client.post(f"{API}/semantic-search", json={"query": query, "top_k": 5})
    hybrid = ev.client.post(f"{API}/hybrid-search", json={"query": query, "top_k": 5})
    session = ev.start_conversation()
    chat = ev.chat(*session, query)
    return {
        "keyword": faq_id in [r["id"] for r in keyword],
        "semantic": faq_id in [r["faq_id"] for r in semantic.json()["results"]],
        "hybrid": faq_id in [r["faq_id"] for r in hybrid.json()["results"]],
        "chat": faq_id in [s["faq_id"] for s in chat.get("sources") or []],
        "public_get": ev.client.get(f"{API}/faqs/{faq_id}").status_code == 200,
    }


def version(ev: EvalApp, faq_id: str) -> int:
    return int(ev.staff("GET", f"/admin/faqs/{faq_id}").json()["version"])


def test_the_full_lifecycle_is_reflected_everywhere_at_once(ev: EvalApp) -> None:
    created = ev.staff("POST", "/admin/faqs", NEW)
    assert created.status_code == 201
    faq_id = created.json()["id"]
    assert all(found(ev, faq_id, "Do you deliver to Norway?").values())

    # A new answer is served at once, and never mixed with the old one.
    changed = "Yes. Orders to Norway usually arrive within 3-4 working days."
    ev.staff("PATCH", f"/admin/faqs/{faq_id}", {"expected_version": 1, "answer": changed})
    session = ev.start_conversation()
    reply = ev.chat(*session, "Do you deliver to Norway?")
    assert reply["answer"] == changed and "5-7" not in reply["answer"]

    # A new question is re-embedded: semantic search finds the new wording, not the old.
    body = {"expected_version": 2, "question": "Can I order from Iceland?"}
    assert ev.staff("PATCH", f"/admin/faqs/{faq_id}", body).status_code == 200
    semantic = ev.client.post(
        f"{API}/semantic-search", json={"query": "Can I order from Iceland?", "top_k": 1}
    ).json()["results"]
    assert semantic[0]["faq_id"] == faq_id
    assert semantic[0]["question"] == "Can I order from Iceland?"

    # Deactivated: gone from every route, including a direct GET.
    ev.staff("POST", f"/admin/faqs/{faq_id}/deactivate", {"expected_version": 3})
    assert not any(found(ev, faq_id, "Can I order from Iceland?").values())

    # Reactivated: back everywhere, with the latest content.
    ev.staff("POST", f"/admin/faqs/{faq_id}/activate", {"expected_version": 4})
    assert all(found(ev, faq_id, "Can I order from Iceland?").values())
    assert ev.client.get(f"{API}/faqs/{faq_id}").json()["answer"] == changed
    assert version(ev, faq_id) == 5


def test_an_embedding_failure_during_a_refresh_is_safe(
    ev: EvalApp, encoder: SwitchableEncoder
) -> None:
    """The edit is saved; semantic search reports itself unavailable instead of using a
    stale index; the chat answers from keyword retrieval with approved text; once
    embeddings work again the index catches up."""
    ev.client.post(f"{API}/semantic-search", json={"query": "warm up", "top_k": 1})  # build
    encoder.failing = True
    created = ev.staff("POST", "/admin/faqs", NEW)
    assert created.status_code == 201  # the change is committed regardless
    faq_id = created.json()["id"]

    semantic = ev.client.post(f"{API}/semantic-search", json={"query": NEW["question"]})
    assert semantic.status_code == 503
    session = ev.start_conversation()
    reply = ev.chat(*session, NEW["question"])
    assert reply["status"] == 200
    assert reply["generation_method"] in ("approved_faq", "fallback", "clarification")
    if reply["decision"] == "answered":
        assert reply["answer"] in {NEW["answer"]} | {
            faq.answer for faq in ev.app.state.faq_service.list_active_faqs()
        }

    encoder.failing = False
    recovered = ev.client.post(
        f"{API}/semantic-search", json={"query": NEW["question"], "top_k": 1}
    )
    assert recovered.status_code == 200
    assert recovered.json()["results"][0]["faq_id"] == faq_id


def test_concurrent_edits_from_many_threads_apply_exactly_once(ev: EvalApp) -> None:
    service = ev.app.state.faq_management_service
    actor = StaffMember("eval-admin", StaffRole.ADMIN)
    start = service.get_faq(actor, "faq_001").version
    barrier = threading.Barrier(8)
    outcomes: list[str] = []

    def edit(n: int) -> None:
        barrier.wait()
        try:
            changes = FAQChanges(expected_version=start, answer=f"Edited by writer {n}.")
            service.update_faq(actor, "faq_001", changes)
            outcomes.append("saved")
        except FAQVersionConflictError:
            outcomes.append("conflict")

    threads = [threading.Thread(target=edit, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["conflict"] * 7 + ["saved"]
    record = service.get_faq(actor, "faq_001")
    assert record.version == start + 1
    revisions = ev.staff("GET", "/admin/faqs/faq_001/revisions").json()
    assert revisions["total"] == 2  # the import and exactly one edit
    # Customers see that one edit, whole.
    assert ev.client.get(f"{API}/faqs/faq_001").json()["answer"] == record.answer
