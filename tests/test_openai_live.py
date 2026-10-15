"""Opt-in live tests against the real OpenAI API (small, PAID requests).

Skipped unless explicitly enabled and a key is available:

    $env:RUN_OPENAI_LIVE_TESTS = "1"; pytest -m openai_live; Remove-Item Env:RUN_OPENAI_LIVE_TESTS

The key and model come from your environment or .env (OPENAI_API_KEY, OPENAI_MODEL).
Each run makes two short requests (roughly 1-2 thousand tokens in total). These tests
check what mocks cannot: that the configured model accepts our request options and
the strict reply schema, and that usage metadata is really returned.
"""

from typing import Any

import pytest

from app.core.config import Settings
from app.models.generation import GenerationOutcome
from app.services.answer_decision_service import AnswerDecisionService
from app.services.context_builder import ContextBuilder
from app.services.grounded_answer_service import GroundedAnswerService
from app.services.llm_service import LLMService
from app.services.usage_tracker import UsageTracker
from tests.fakes import FAQS, FakeHybrid, FakeKeyword
from tests.fakes import result as hybrid_spec

pytestmark = pytest.mark.openai_live


def test_live_generation_returns_text_and_real_usage(live_openai_settings: Settings) -> None:
    tracker = UsageTracker()
    llm = LLMService.from_settings(live_openai_settings, usage_tracker=tracker)

    try:
        result = llm.generate(
            instructions="You are a test probe. Reply with the single word: ready",
            input="Status?",
        )
    finally:
        llm.close()

    assert result.text
    assert result.input_tokens and result.input_tokens > 0
    assert result.output_tokens and result.output_tokens > 0
    assert result.total_tokens and result.total_tokens >= result.input_tokens
    assert result.model.startswith(live_openai_settings.openai_model.split("-")[0])
    [record] = tracker.recent()
    assert record.usage.total_tokens == result.total_tokens
    assert record.estimated_cost is None  # no price configured in this test


def test_live_grounded_answer_accepts_the_structured_reply_schema(
    live_openai_settings: Settings,
) -> None:
    hybrid = FakeHybrid([hybrid_spec("faq_107", similarity=0.8)])  # Stage 4 matches faq_107
    llm = LLMService.from_settings(live_openai_settings)
    service = GroundedAnswerService(
        AnswerDecisionService(hybrid, FakeKeyword()),  # type: ignore[arg-type]
        ContextBuilder(),
        llm,
    )

    try:
        result = service.answer("I forgot my password, what should I do?").result
    finally:
        llm.close()

    d: Any = result.diagnostics
    assert d.llm_called is True
    # The API accepted the request, the model, the options and the strict schema.
    assert d.llm_error_category is None, f"LLM call failed: {d.llm_error_category} ({d.detail})"
    assert d.input_tokens and d.output_tokens
    # Either the validated rewrite, or (if a check overruled it) the approved answer.
    assert result.outcome in (GenerationOutcome.GENERATED, GenerationOutcome.APPROVED_ANSWER)
    assert "faq_107" in result.source_faq_ids
    if result.outcome is GenerationOutcome.APPROVED_ANSWER:
        assert result.answer == FAQS["faq_107"].answer
