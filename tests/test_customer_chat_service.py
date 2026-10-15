"""Conversational turns: history + rewriting around the unchanged Stage 4/5 pipeline."""

import json
import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

import app.repositories.conversation_repository as repository_module
from app.database.connection import Database
from app.database.faq_repository import FAQFileNotFoundError
from app.models.answer import GenerationMethod
from app.models.chat import CustomerChatRequest
from app.models.conversation import MessageRole, SessionStatus
from app.repositories.conversation_repository import (
    ConversationRepository,
    ConversationStoreError,
)
from app.services.answer_decision_service import (
    NO_MATCH_MESSAGE,
    AnswerDecisionService,
    Decision,
)
from app.services.context_builder import ContextBuilder
from app.services.conversation_history_service import ConversationHistoryService
from app.services.conversation_service import (
    ConversationNotActiveError,
    ConversationNotFoundError,
    ConversationService,
    CreatedConversation,
)
from app.services.customer_chat_service import (
    CustomerChatService,
    KnowledgeBaseUnavailableError,
)
from app.services.grounded_answer_service import GroundedAnswerService
from app.services.llm_service import LLMService
from app.services.query_rewrite_service import QueryRewriteService, RewriteMethod
from tests.fakes import FAQS, FakeHybrid, FakeKeyword, result

# Relative to the real clock: some calls (like the API) use the current time.
NOW = datetime.now(UTC).replace(microsecond=0)
TTL = timedelta(minutes=60)
KEY = SecretStr("sk-test-0123456789abcdefghij")


class RoutingHybrid(FakeHybrid):
    """Search results depend on words in the query, like real retrieval."""

    ROUTES = {
        "return": [result("faq_106", similarity=0.8)],
        "password": [result("faq_107", similarity=0.8)],
        # Two acceptable candidates within the ambiguity margin -> Stage 4 asks to clarify.
        "delivery": [result("faq_103", similarity=0.66), result("faq_104", similarity=0.64)],
    }

    def __init__(self) -> None:
        super().__init__([])

    def search(self, query: str, *, top_k: int, category: Any) -> list:
        lowered = query.lower()
        self.specs = next(
            (specs for word, specs in self.ROUTES.items() if word in lowered),
            [result("faq_101", similarity=0.1)],  # nothing acceptable -> no_match
        )
        return super().search(query, top_k=top_k, category=category)


class FakeClient:
    """OpenAI client stand-in: returns scripted outputs in order, or raises."""

    def __init__(self, *outputs: str | Exception) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict[str, Any]] = []
        self.responses = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return SimpleNamespace(
            status="completed",
            output_text=output,
            output=[],
            model="gpt-5.4-mini",
            id="resp_1",
            usage=SimpleNamespace(input_tokens=300, output_tokens=40),
            _request_id="req_1",
        )

    def close(self) -> None:
        pass


def reply(answer: str, faq_id: str) -> str:
    return json.dumps(
        {"status": "answered", "answer": answer, "source_faq_ids": [faq_id], "reason": "r"}
    )


@pytest.fixture
def database() -> Iterator[Database]:
    db = Database("sqlite://")
    db.init_schema()
    yield db
    db.dispose()


def ask(message: str, **fields: Any) -> CustomerChatRequest:
    """A validated chat request, as the API route receives it."""
    return CustomerChatRequest.model_validate({"message": message, **fields})


class Setup(SimpleNamespace):
    chat: CustomerChatService
    conversations: ConversationService
    repo: ConversationRepository
    hybrid: RoutingHybrid
    client: FakeClient
    created: CreatedConversation

    def send(self, message: str, **kwargs: Any) -> Any:
        sid, token = self.created.session.session_id, self.created.access_token
        request = ask(message, category=kwargs.pop("category", None))
        return self.chat.chat(sid, token, request, now=kwargs.pop("now", NOW), **kwargs)

    def stored(self) -> list:
        return self.repo.list_messages(self.created.session.session_id)


def build(
    database: Database,
    *outputs: str | Exception,
    llm_enabled: bool = False,
    rewriter: Any = None,
    grounded: Any = None,
    max_messages: int = 10,
    faq_lookup: Any = None,
    **service_options: Any,
) -> Setup:
    repo = ConversationRepository(database)
    conversations = ConversationService(repo, TTL)
    history = ConversationHistoryService(
        conversations, repo, max_messages=max_messages, max_chars=4000
    )
    hybrid = RoutingHybrid()
    client = FakeClient(*outputs)
    llm = LLMService(
        KEY, model="gpt-5.4-mini", enabled=llm_enabled, client_factory=lambda key: client
    )  # type: ignore[arg-type,return-value]
    decisions = AnswerDecisionService(hybrid, FakeKeyword())  # type: ignore[arg-type]
    grounded = grounded or GroundedAnswerService(decisions, ContextBuilder(), llm)
    chat = CustomerChatService(
        conversations,
        history,
        rewriter or QueryRewriteService(None),
        grounded,
        faq_lookup,
        **service_options,
    )
    return Setup(
        chat=chat,
        conversations=conversations,
        repo=repo,
        hybrid=hybrid,
        client=client,
        created=conversations.create_conversation(now=NOW),
    )


# --- Happy path -----------------------------------------------------------------------


def test_first_message_is_answered_and_stored(database: Database) -> None:
    s = build(database)

    turn = s.send("What is your return policy?")

    response = turn.response
    assert response.session_id == s.created.session.session_id
    assert response.user_message.content == "What is your return policy?"
    assert response.assistant_message.content == FAQS["faq_106"].answer
    assert response.assistant_message.decision is Decision.ANSWERED
    assert response.assistant_message.source_faq_ids == ["faq_106"]
    assert response.generation_method is GenerationMethod.APPROVED_FAQ  # LLM off
    assert response.usage is None
    assert [(m.role, m.sequence) for m in s.stored()] == [
        (MessageRole.USER, 1),
        (MessageRole.ASSISTANT, 2),
    ]
    assert s.stored()[1].source_faq_ids == ("faq_106",)


def test_follow_up_is_rewritten_for_retrieval_but_stored_as_typed(database: Database) -> None:
    s = build(database)
    s.send("What is your return policy?")

    turn = s.send("What about after that?")

    assert s.hybrid.calls[-1]["query"] == "What is your return policy? What about after that?"
    assert turn.rewrite.method is RewriteMethod.DETERMINISTIC
    assert turn.response.assistant_message.decision is Decision.ANSWERED
    assert turn.response.assistant_message.source_faq_ids == ["faq_106"]
    assert turn.response.user_message.content == "What about after that?"
    assert [m.content for m in s.stored()][2] == "What about after that?"
    assert [m.sequence for m in s.stored()] == [1, 2, 3, 4]


def test_without_rewriting_the_follow_up_would_not_match(database: Database) -> None:
    """Shows the rewrite is what made the follow-up answerable (not history as evidence)."""
    s = build(database)

    turn = s.send("What about after that?")  # no history: nothing to resolve it with

    assert turn.response.assistant_message.decision is Decision.NO_MATCH
    assert turn.response.assistant_message.content == NO_MATCH_MESSAGE
    assert turn.response.assistant_message.source_faq_ids == []


def test_new_topic_is_searched_on_its_own(database: Database) -> None:
    s = build(database)
    s.send("What is your return policy?")

    turn = s.send("And how do I reset my password?")

    assert s.hybrid.calls[-1]["query"] == "And how do I reset my password?"
    assert turn.response.assistant_message.source_faq_ids == ["faq_107"]


def test_category_is_passed_to_retrieval(database: Database) -> None:
    s = build(database)

    s.send("What is your return policy?", category="returns")

    assert s.hybrid.calls[-1]["category"].value == "returns"


# --- Retrieval rules still apply ------------------------------------------------------


def test_rewriting_cannot_bypass_acceptance_rules(database: Database) -> None:
    s = build(database)
    s.send("Do you sell hats?")  # no match

    turn = s.send("How much do they cost?")

    assert turn.rewrite.rewritten  # context was added ...
    assert turn.response.assistant_message.decision is Decision.NO_MATCH  # ... still no answer
    assert turn.response.generation_method is GenerationMethod.FALLBACK
    assert s.client.calls == []


def test_history_is_never_sent_to_the_answer_generator(database: Database) -> None:
    first = "Use the Forgot Password link and we will email you."  # earlier LLM wording
    second = "Select Forgot Password on the sign-in page and follow the emailed link."
    s = build(database, reply(first, "faq_107"), reply(second, "faq_107"), llm_enabled=True)
    s.send("How do I reset my password?")

    turn = s.send("And what about after that?")

    generation_input = s.client.calls[-1]["input"]
    assert first not in generation_input  # earlier reply: not evidence, not sent
    assert FAQS["faq_107"].answer in generation_input  # approved FAQ evidence is
    assert turn.response.generation_method is GenerationMethod.LLM_GROUNDED
    assert turn.response.usage is not None
    assert turn.response.assistant_message.source_faq_ids == ["faq_107"]
    assert s.client.calls[-1]["safety_identifier"] != str(s.created.session.session_id)


def test_llm_query_rewrite_feeds_retrieval(database: Database) -> None:
    class RewriteLLM:
        is_available = True

        def generate(self, **_: Any) -> Any:
            query = "What is your return policy after that?"
            return SimpleNamespace(text=json.dumps({"status": "rewritten", "query": query}))

    s = build(database, rewriter=QueryRewriteService(RewriteLLM()))  # type: ignore[arg-type]
    s.send("What is your return policy?")

    turn = s.send("And after that?")

    assert turn.rewrite.method is RewriteMethod.LLM
    assert s.hybrid.calls[-1]["query"] == "What is your return policy after that?"
    assert s.stored()[2].content == "And after that?"


# --- Fallbacks ------------------------------------------------------------------------


def test_generation_failure_returns_and_stores_the_approved_answer(database: Database) -> None:
    s = build(database, RuntimeError("SDK bug"), llm_enabled=True)

    turn = s.send("What is your return policy?")

    assert turn.response.generation_method is GenerationMethod.APPROVED_FAQ
    assert turn.response.assistant_message.content == FAQS["faq_106"].answer
    assert s.stored()[1].content == FAQS["faq_106"].answer


def test_invalid_generated_claim_is_not_stored(database: Database) -> None:
    invented = "You can return items within 90 days and we will refund you today."
    s = build(database, reply(invented, "faq_106"), llm_enabled=True)

    turn = s.send("What is your return policy?")

    assert turn.response.generation_method is GenerationMethod.APPROVED_FAQ
    assert all(invented not in m.content for m in s.stored())


def test_rewrite_failure_searches_the_original_message(database: Database) -> None:
    class BrokenRewriter:
        def rewrite(self, *_: Any) -> Any:
            raise RuntimeError("bug")

    s = build(database, rewriter=BrokenRewriter())
    s.send("What is your return policy?")

    turn = s.send("  What about   after that? ")

    assert s.hybrid.calls[-1]["query"] == "What about after that?"
    assert turn.rewrite.method is RewriteMethod.UNCHANGED
    assert len(s.stored()) == 4


# --- Access and consistency -----------------------------------------------------------


@pytest.mark.parametrize("token", [None, "cst_wrong", "OTHER"])
def test_unauthorised_messages_are_not_processed_or_stored(
    database: Database, token: str | None
) -> None:
    s = build(database)
    if token == "OTHER":  # noqa: S105 - placeholder for another conversation's token
        token = s.conversations.create_conversation(now=NOW).access_token

    with pytest.raises(ConversationNotFoundError):
        s.chat.chat(
            s.created.session.session_id, token, ask("What is your return policy?"), now=NOW
        )

    assert s.hybrid.calls == []
    assert s.stored() == []


def test_unknown_session_is_not_found(database: Database) -> None:
    s = build(database)

    with pytest.raises(ConversationNotFoundError):
        s.chat.chat(uuid.uuid4(), s.created.access_token, ask("Hi"), now=NOW)


@pytest.mark.parametrize("when", ["closed", "expired"])
def test_inactive_conversations_take_no_messages(database: Database, when: str) -> None:
    s = build(database)
    now = NOW
    if when == "closed":
        s.conversations.close_conversation(
            s.created.session.session_id, s.created.access_token, now=NOW
        )
    else:
        now = NOW + TTL

    with pytest.raises(ConversationNotActiveError):
        s.send("What is your return policy?", now=now)
    assert s.hybrid.calls == []
    assert s.stored() == []


def test_database_failure_stores_neither_message(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = build(database)
    original = repository_module._message_row

    def broken_reply(message: Any) -> Any:
        row = original(message)
        if message.role is MessageRole.ASSISTANT:
            row.content = ""  # violates the content CHECK constraint at commit
        return row

    monkeypatch.setattr(repository_module, "_message_row", broken_reply)

    with pytest.raises(ConversationStoreError):
        s.send("What is your return policy?")

    monkeypatch.undo()
    assert s.stored() == []  # not the customer message without its reply
    session = s.repo.get_session(s.created.session.session_id)
    assert session.updated_at == NOW and session.status is SessionStatus.ACTIVE


def test_knowledge_base_failure_stores_nothing(database: Database) -> None:
    class BrokenGrounded:
        def answer(self, *_: Any, **__: Any) -> Any:
            raise FAQFileNotFoundError("missing", path=Path("faqs.json"))

    s = build(database, grounded=BrokenGrounded())

    with pytest.raises(KnowledgeBaseUnavailableError):
        s.send("What is your return policy?")
    assert s.stored() == []


def test_session_closed_during_generation_stores_nothing(database: Database) -> None:
    s = build(database)
    original = s.chat._grounded.answer

    def close_meanwhile(*args: Any, **kwargs: Any) -> Any:
        s.conversations.close_conversation(
            s.created.session.session_id, s.created.access_token, now=NOW
        )
        return original(*args, **kwargs)

    s.chat._grounded.answer = close_meanwhile  # type: ignore[method-assign]

    with pytest.raises(ConversationNotActiveError):
        s.send("What is your return policy?")
    assert s.stored() == []


def test_reply_finishing_after_expiry_is_stored(database: Database) -> None:
    s = build(database)
    sid = s.created.session.session_id
    received = NOW + TTL - timedelta(seconds=1)

    user, assistant = s.repo.add_exchange(
        sid, "Hi", "Hello", Decision.NO_MATCH, received_at=received, answered_at=NOW + TTL
    )

    assert (user.sequence, assistant.sequence) == (1, 2)
    with pytest.raises(Exception, match="expired"):
        s.repo.add_exchange(sid, "Hi", "Hello", Decision.NO_MATCH, received_at=NOW + TTL)


def test_history_given_to_the_rewriter_is_bounded(database: Database) -> None:
    seen: list[int] = []

    class SpyRewriter(QueryRewriteService):
        def rewrite(self, message: str, history: Any) -> Any:
            seen.append(len(history))
            return super().rewrite(message, history)

    s = build(database, rewriter=SpyRewriter(None), max_messages=2)
    for _ in range(3):
        s.send("What is your return policy?")

    assert seen == [0, 2, 2]


def test_turn_logs_contain_no_customer_text(
    database: Database, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    s = build(database)

    s.send("What is your return policy? my email is jane@example.com")

    assert "event=conversation_turn" in caplog.text
    assert "decision=answered" in caplog.text
    assert "return policy" not in caplog.text
    assert s.created.access_token not in caplog.text


def test_ambiguous_question_gets_and_stores_a_clarifying_question(database: Database) -> None:
    s = build(database, reply("unused", "faq_103"), llm_enabled=True)

    turn = s.send("Tell me about delivery")

    reply_message = turn.response.assistant_message
    assert reply_message.decision is Decision.AMBIGUOUS
    assert turn.response.generation_method is GenerationMethod.CLARIFICATION
    assert set(reply_message.source_faq_ids) == {"faq_103", "faq_104"}  # the options offered
    assert reply_message.content.startswith("Could you clarify")
    assert s.client.calls == []  # ambiguous questions are never handed to the LLM
    assert s.stored()[1].decision is Decision.AMBIGUOUS
