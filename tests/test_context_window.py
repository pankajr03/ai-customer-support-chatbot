"""Context window management: tokenizers, token budgets and per-message limits."""

import json
import logging
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import tiktoken
from pydantic import ValidationError

from app.core.config import Settings
from app.database.connection import Database
from app.models.conversation import MessageRole
from app.prompts.query_rewrite import REWRITE_INSTRUCTIONS, render_rewrite_input, render_turn
from app.repositories.conversation_repository import ConversationRepository
from app.services.answer_decision_service import Decision
from app.services.context_window import (
    ContextWindow,
    EstimatedTokenizer,
    TiktokenTokenizer,
    build_tokenizer,
)
from app.services.conversation_history_service import (
    TRUNCATION_MARK,
    ConversationHistoryService,
    HistoryTurn,
    RecentHistory,
)
from app.services.conversation_service import ConversationService
from app.services.query_rewrite_service import (
    REWRITE_MAX_OUTPUT_TOKENS,
    QueryRewriteService,
    RewriteMethod,
)

# Relative to the real clock: some calls (like the API) use the current time.
NOW = datetime.now(UTC).replace(microsecond=0)


def byte_encoding() -> tiktoken.Encoding:
    """An offline tiktoken encoding: one token per byte (no merges, no download)."""
    return tiktoken.Encoding(
        name="bytes",
        pat_str=r"\S+|\s+",
        mergeable_ranks={bytes([i]): i for i in range(256)},
        special_tokens={},
    )


class WordTokenizer:
    """One token per whitespace-separated word: easy to reason about in tests."""

    name = "words"

    def count(self, text: str) -> int:
        return len(text.split())


# --- Tokenizers -----------------------------------------------------------------------


def test_estimate_is_bytes_over_three_rounded_up() -> None:
    tokenizer = EstimatedTokenizer()

    assert tokenizer.count("") == 0
    assert tokenizer.count("abc") == 1
    assert tokenizer.count("abcd") == 2
    assert tokenizer.count("日本語") == 3  # 9 bytes: about one token per character
    assert tokenizer.name == "estimate"


def test_estimate_overcounts_typical_english() -> None:
    text = "How long does standard delivery take after my order has been dispatched?"
    # GPT tokenizers use about one token per common English word (plus punctuation).
    word_level = len(text.split()) + 1

    assert EstimatedTokenizer().count(text) > word_level


def test_tiktoken_counts_with_the_loaded_encoding() -> None:
    tokenizer = TiktokenTokenizer("bytes", loader=lambda name: byte_encoding())

    assert tokenizer.count("hello") == 5
    assert tokenizer.count("<|endoftext|>") == 13  # special-token text counted as text
    assert tokenizer.name == "tiktoken:bytes"


def test_tiktoken_loads_once_across_threads() -> None:
    loads: list[str] = []

    def loader(name: str) -> tiktoken.Encoding:
        loads.append(name)
        return byte_encoding()

    tokenizer = TiktokenTokenizer("bytes", loader=loader)
    threads = [threading.Thread(target=tokenizer.count, args=("hi",)) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert loads == ["bytes"]


def test_tiktoken_failure_falls_back_to_the_estimate(caplog: pytest.LogCaptureFixture) -> None:
    calls: list[str] = []

    def offline(name: str) -> Any:
        calls.append(name)
        raise ConnectionError("no network")

    tokenizer = TiktokenTokenizer("o200k_base", loader=offline)

    assert tokenizer.count("abcdef") == 2
    assert tokenizer.count("abcdef") == 2
    assert tokenizer.name == "estimate"
    assert calls == ["o200k_base"]  # not retried on every request
    assert caplog.text.count("using token estimates") == 1


def test_build_tokenizer() -> None:
    assert isinstance(build_tokenizer("estimate"), EstimatedTokenizer)
    tokenizer = build_tokenizer("tiktoken", "cl100k_base")
    assert isinstance(tokenizer, TiktokenTokenizer)
    assert tokenizer.encoding_name == "cl100k_base"


def test_default_tiktoken_loader_is_lazy() -> None:
    TiktokenTokenizer()  # constructing it must not import or download anything


# --- Budget fitting -------------------------------------------------------------------


def fit(window: ContextWindow, items: list[str], **kwargs: Any) -> Any:
    defaults = {"instructions": "a b c", "message": "m", "reserved_output_tokens": 4}
    return window.fit(items, lambda item: item, **(defaults | kwargs))


def test_fixed_parts_are_reserved_before_history() -> None:
    window = ContextWindow(20, WordTokenizer())

    fitted = fit(window, ["one two", "three four five", "six"], evidence="e1 e2")

    # fixed = 3 instructions + 2 evidence + 1 message + 4 output = 10; 10 left for history
    assert fitted.fixed_tokens == 10
    assert fitted.items == ("one two", "three four five", "six")
    assert fitted.history_tokens == 6
    assert fitted.total_tokens == 16 <= window.max_tokens
    assert fitted.fits and fitted.omitted == 0


def test_oldest_items_are_dropped_and_order_is_kept() -> None:
    window = ContextWindow(14, WordTokenizer())  # 6 tokens left for history

    fitted = fit(window, ["one two", "three four five", "six seven", "eight"], evidence="e1 e2")

    assert fitted.items == ("six seven", "eight")  # newest that fit, oldest first
    assert fitted.omitted == 2


def test_window_has_no_gaps() -> None:
    window = ContextWindow(13, WordTokenizer())  # 3 tokens left

    fitted = fit(window, ["x", "a b c d e", "y"], evidence="e1 e2")

    assert fitted.items == ("y",)  # "x" would fit, but not after skipping the long one


def test_fixed_parts_over_the_limit_use_no_history() -> None:
    window = ContextWindow(8, WordTokenizer())

    fitted = fit(window, ["one"], message="a long customer message here")

    assert not fitted.fits
    assert fitted.items == () and fitted.omitted == 1


def test_render_is_what_gets_counted() -> None:
    window = ContextWindow(12, WordTokenizer())

    fitted = window.fit(
        ["a", "b"],
        lambda item: f"<turn> {item} </turn>",  # 3 words each
        instructions="i",
        message="m",
        reserved_output_tokens=4,
    )

    assert fitted.items == ("a", "b") and fitted.history_tokens == 6


def test_only_needed_items_are_counted() -> None:
    counted: list[str] = []

    class Spy(WordTokenizer):
        def count(self, text: str) -> int:
            counted.append(text)
            return super().count(text)

    window = ContextWindow(12, Spy())
    fit(window, [f"old{i} x x x" for i in range(100)] + ["new"])

    history_counts = [t for t in counted if t.startswith(("old", "new"))]
    assert len(history_counts) == 2  # "new", then the first that did not fit; not all 101


def test_invalid_window() -> None:
    with pytest.raises(ValueError, match="max_tokens"):
        ContextWindow(0)
    assert isinstance(ContextWindow(10).tokenizer, EstimatedTokenizer)


# --- Settings -------------------------------------------------------------------------


def test_settings_defaults_and_validation() -> None:
    settings = Settings()

    assert settings.conversation_history_max_messages == 10
    assert settings.conversation_history_max_chars == 4000
    assert settings.conversation_history_max_message_chars == 1000
    assert settings.conversation_context_max_tokens == 3000
    assert settings.context_tokenizer == "estimate"
    assert settings.tiktoken_encoding == "o200k_base"
    with pytest.raises(ValidationError):
        Settings(context_tokenizer="words")
    with pytest.raises(ValidationError):
        Settings(conversation_context_max_tokens=100)
    with pytest.raises(ValidationError):
        Settings(tiktoken_encoding="../etc")


# --- Per-message limit in history -----------------------------------------------------


@pytest.fixture
def database() -> Iterator[Database]:
    db = Database("sqlite://")
    db.init_schema()
    yield db
    db.dispose()


def history_setup(database: Database, **limits: Any) -> tuple[Any, Any, Any]:
    repo = ConversationRepository(database)
    conversations = ConversationService(repo, timedelta(hours=1))
    service = ConversationHistoryService(
        conversations, repo, **({"max_messages": 10, "max_chars": 4000} | limits)
    )
    created = conversations.create_conversation(now=NOW)
    return service, repo, created


def test_long_message_is_shortened_not_dropped(database: Database) -> None:
    service, repo, created = history_setup(database, max_chars=300, max_message_chars=100)
    sid = created.session.session_id
    repo.add_exchange(
        sid,
        "What is your return policy?",
        "x" * 5000,
        Decision.ANSWERED,
        received_at=NOW,
        answered_at=NOW,
    )

    history = service.load_recent(sid, created.access_token, now=NOW)

    question, reply = history.turns
    assert not question.truncated and question.content == "What is your return policy?"
    assert reply.truncated
    assert len(reply.content) == 100 and reply.content.endswith(TRUNCATION_MARK)
    assert not history.omitted_older


def test_without_a_per_message_limit_one_long_reply_hides_everything(database: Database) -> None:
    service, repo, created = history_setup(database, max_chars=300)
    sid = created.session.session_id
    repo.add_exchange(sid, "Hi", "x" * 5000, Decision.ANSWERED, received_at=NOW, answered_at=NOW)

    history = service.load_recent(sid, created.access_token, now=NOW)

    assert history.turns == () and history.omitted_older


def test_fallback_replies_are_left_out(database: Database) -> None:
    service, repo, created = history_setup(database)
    sid = created.session.session_id
    repo.add_exchange(
        sid,
        "Do you sell hats?",
        "Sorry, no answer.",
        Decision.NO_MATCH,
        received_at=NOW,
        answered_at=NOW,
    )
    repo.add_exchange(
        sid,
        "Search?",
        "Try later.",
        Decision.RETRIEVAL_UNAVAILABLE,
        received_at=NOW,
        answered_at=NOW,
    )
    repo.add_exchange(
        sid,
        "Which one?",
        "Did you mean A or B?",
        Decision.AMBIGUOUS,
        received_at=NOW,
        answered_at=NOW,
    )

    history = service.load_recent(sid, created.access_token, now=NOW)

    assert [t.content for t in history.turns] == [
        "Do you sell hats?",
        "Search?",
        "Which one?",
        "Did you mean A or B?",
    ]
    assert not history.omitted_older


def test_per_message_limit_from_settings_and_validation(database: Database) -> None:
    repo = ConversationRepository(database)
    conversations = ConversationService(repo, timedelta(hours=1))

    service = ConversationHistoryService.from_settings(
        Settings(conversation_history_max_message_chars=250), conversations, repo
    )

    assert service.max_message_chars == 250
    with pytest.raises(ValueError, match="max_message_chars"):
        ConversationHistoryService(
            conversations,
            repo,
            max_messages=1,
            max_chars=10,
            max_message_chars=len(TRUNCATION_MARK),
        )


# --- Token budget in query rewriting --------------------------------------------------


def turns(*pairs: tuple[str, str], truncated: bool = False) -> RecentHistory:
    return RecentHistory(
        session_id=uuid.uuid4(),
        turns=tuple(
            HistoryTurn(MessageRole(role), text, i, truncated=truncated)
            for i, (role, text) in enumerate(pairs, start=1)
        ),
        omitted_older=False,
    )


class FakeLLM:
    is_available = True

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def generate(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(text=json.dumps({"status": "unchanged", "query": ""}))


LONG_HISTORY = turns(
    ("user", "What is your return policy for opened items?"),
    ("assistant", "Opened items can be returned within 14 days. " * 20),
    ("user", "What is your return policy?"),
    ("assistant", "Most unused items can be returned within 30 days."),
)


def test_rewrite_prompt_keeps_only_history_that_fits(monkeypatch: pytest.MonkeyPatch) -> None:
    tokenizer = EstimatedTokenizer()
    message = "And after that?"
    fixed = (
        tokenizer.count(REWRITE_INSTRUCTIONS)
        + tokenizer.count(render_rewrite_input(message, ()))
        + REWRITE_MAX_OUTPUT_TOKENS
    )
    recent = LONG_HISTORY.turns[2:]
    budget = fixed + sum(tokenizer.count(render_turn(t)) for t in recent)
    fake = FakeLLM()
    service = QueryRewriteService(fake, context_window=ContextWindow(budget, tokenizer))  # type: ignore[arg-type]

    service.rewrite(message, LONG_HISTORY)

    sent = fake.calls[0]["input"]
    assert "Most unused items" in sent and "What is your return policy?" in sent
    assert "opened items" not in sent.lower()  # older turns omitted, not squeezed in
    assert (
        tokenizer.count(REWRITE_INSTRUCTIONS) + tokenizer.count(sent) + REWRITE_MAX_OUTPUT_TOKENS
        <= budget
    )


def test_rewrite_skips_the_llm_when_the_prompt_cannot_fit() -> None:
    fake = FakeLLM()
    service = QueryRewriteService(fake, context_window=ContextWindow(500))  # type: ignore[arg-type]

    result = service.rewrite("And after that?", LONG_HISTORY)

    assert fake.calls == []
    assert result.llm_fallback == "context_too_large"
    assert result.method is RewriteMethod.DETERMINISTIC  # conservative fallback still works


def test_rewrite_skips_the_llm_when_no_customer_turn_fits() -> None:
    fake = FakeLLM()
    h = turns(("user", "What is your return policy?"), ("assistant", "word " * 400))
    tokenizer = EstimatedTokenizer()
    fixed = (
        tokenizer.count(REWRITE_INSTRUCTIONS)
        + tokenizer.count(render_rewrite_input("And after that?", ()))
        + REWRITE_MAX_OUTPUT_TOKENS
    )
    service = QueryRewriteService(fake, context_window=ContextWindow(fixed + 50, tokenizer))  # type: ignore[arg-type]

    result = service.rewrite("And after that?", h)

    assert fake.calls == [] and result.llm_fallback == "context_too_large"


def test_truncated_questions_are_never_copied_into_a_query() -> None:
    h = turns(("user", "What is your return policy for [...]"), truncated=True)

    result = QueryRewriteService(None).rewrite("And after that?", h)

    assert result.query == "And after that?"
    assert result.reason.value == "unresolved"


def test_rewriter_window_from_settings() -> None:
    service = QueryRewriteService.from_settings(
        Settings(conversation_context_max_tokens=2000, context_tokenizer="tiktoken"), None
    )

    assert service.context_window.max_tokens == 2000
    assert isinstance(service.context_window.tokenizer, TiktokenTokenizer)


def test_logs_never_contain_text(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    QueryRewriteService(FakeLLM(), context_window=ContextWindow(500)).rewrite(  # type: ignore[arg-type]
        "And after that?", LONG_HISTORY
    )

    assert "llm_fallback=context_too_large" in caplog.text
    assert "return policy" not in caplog.text


def test_default_loader_uses_tiktoken_get_encoding(monkeypatch: pytest.MonkeyPatch) -> None:
    requested: list[str] = []

    def get_encoding(name: str) -> tiktoken.Encoding:
        requested.append(name)
        return byte_encoding()

    monkeypatch.setattr(tiktoken, "get_encoding", get_encoding)

    assert TiktokenTokenizer("o200k_base").count("abc") == 3
    assert requested == ["o200k_base"]
