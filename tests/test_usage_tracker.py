"""Usage tracking: real API token counts, optional cost estimates, no customer data."""

import dataclasses
import json
import logging
import threading
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import httpx2
import openai
import pytest
from pydantic import SecretStr, ValidationError

from app.api.dependencies import get_usage_tracker
from app.core.config import Settings
from app.main import create_app
from app.models.usage import ModelPricing
from app.services.llm_service import LLMResponseError, LLMService
from app.services.usage_tracker import COST_LABEL, TokenUsage, UsageTracker

KEY = SecretStr("sk-test-0123456789abcdefghij")
# Test prices only: they do not reflect any real model's pricing.
PRICE = ModelPricing(
    input_per_million_tokens=2.0,
    output_per_million_tokens=8.0,
    cached_input_per_million_tokens=0.5,
)
CUSTOMER_TEXT = "my email is jane.doe@example.com, order 4711"


def usage_obj(**fields: Any) -> SimpleNamespace:
    data = {
        "input_tokens": 1200,
        "output_tokens": 300,
        "total_tokens": 1500,
        "input_tokens_details": SimpleNamespace(cached_tokens=200, cache_write_tokens=0),
        "output_tokens_details": SimpleNamespace(reasoning_tokens=120),
    }
    data.update(fields)
    return SimpleNamespace(**data)


# --- Reading usage exactly as reported ---------------------------------------------------------


def test_usage_is_read_from_the_api_response() -> None:
    usage = TokenUsage.from_response(SimpleNamespace(usage=usage_obj()))

    assert usage == TokenUsage(
        input_tokens=1200,
        cached_input_tokens=200,
        cache_write_tokens=0,
        output_tokens=300,
        reasoning_tokens=120,
        total_tokens=1500,
    )
    assert usage.is_reported


def test_missing_usage_is_none_not_invented() -> None:
    assert TokenUsage.from_response(SimpleNamespace(usage=None)) == TokenUsage()
    assert TokenUsage.from_response(SimpleNamespace()) == TokenUsage()
    assert not TokenUsage().is_reported


def test_total_is_never_computed_when_the_api_omits_it() -> None:
    usage = TokenUsage.from_response(
        SimpleNamespace(usage=usage_obj(total_tokens=None, output_tokens_details=None))
    )

    assert (usage.input_tokens, usage.output_tokens) == (1200, 300)
    assert (usage.total_tokens, usage.reasoning_tokens) == (None, None)


@pytest.mark.parametrize("bad", [-1, 2.5, True, "100", float("nan"), float("inf"), None])
def test_unusable_counts_become_none(bad: Any) -> None:
    usage = TokenUsage.from_response(SimpleNamespace(usage=usage_obj(input_tokens=bad)))

    assert usage.input_tokens is None


# --- Cost estimates: only with configured prices --------------------------------------------


def test_no_configured_price_means_no_cost() -> None:
    tracker = UsageTracker()

    assert tracker.estimate_cost(
        "gpt-5.4-mini", TokenUsage(input_tokens=1000, output_tokens=10)
    ) == (
        None,
        None,
    )


def test_cost_uses_configured_rates_and_cached_discount() -> None:
    tracker = UsageTracker({"gpt-5.4-mini": PRICE})
    usage = TokenUsage(input_tokens=1200, cached_input_tokens=200, output_tokens=300)

    cost, pricing_model = tracker.estimate_cost("gpt-5.4-mini", usage)

    # (1000 x 2.0 + 200 x 0.5 + 300 x 8.0) / 1e6
    assert cost == pytest.approx(0.0045)
    assert pricing_model == "gpt-5.4-mini"


def test_cached_tokens_use_the_input_rate_without_a_cached_price() -> None:
    tracker = UsageTracker(
        {"m": ModelPricing(input_per_million_tokens=2.0, output_per_million_tokens=8.0)}
    )

    cost, _ = tracker.estimate_cost(
        "m", TokenUsage(input_tokens=1200, cached_input_tokens=200, output_tokens=300)
    )

    assert cost == pytest.approx((1200 * 2.0 + 300 * 8.0) / 1e6)


def test_reasoning_tokens_are_not_counted_twice() -> None:
    tracker = UsageTracker({"m": PRICE})
    plain = TokenUsage(input_tokens=1000, output_tokens=300)
    with_reasoning = TokenUsage(input_tokens=1000, output_tokens=300, reasoning_tokens=200)

    assert tracker.estimate_cost("m", plain) == tracker.estimate_cost("m", with_reasoning)


@pytest.mark.parametrize(
    "usage",
    [TokenUsage(), TokenUsage(input_tokens=100), TokenUsage(output_tokens=100)],
)
def test_incomplete_usage_gives_no_estimate(usage: TokenUsage) -> None:
    assert UsageTracker({"m": PRICE}).estimate_cost("m", usage) == (None, None)


def test_cached_count_above_input_is_capped() -> None:
    cost, _ = UsageTracker({"m": PRICE}).estimate_cost(
        "m", TokenUsage(input_tokens=100, cached_input_tokens=500, output_tokens=0)
    )

    assert cost == pytest.approx(100 * 0.5 / 1e6)


@pytest.mark.parametrize(
    ("model", "priced_as"),
    [
        ("gpt-5.4-mini", "gpt-5.4-mini"),
        ("gpt-5.4-mini-2026-03-17", "gpt-5.4-mini"),  # dated snapshot of a priced model
        ("gpt-5.4", None),  # a different model is never priced by prefix
        ("gpt-5.4-mini-pro", None),
        ("gpt-5.4-nano-2026-03-17", None),
    ],
)
def test_model_matching_prices_snapshots_but_not_other_models(
    model: str, priced_as: str | None
) -> None:
    found = UsageTracker({"gpt-5.4-mini": PRICE}).pricing_for(model)

    assert (found[0] if found else None) == priced_as


# --- Recording and totals ---------------------------------------------------------------------


def record(tracker: UsageTracker, model: str = "gpt-5.4-mini", **usage: Any) -> Any:
    counts = {"input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100}
    counts.update(usage)
    return tracker.record(
        model=model,
        status="completed",
        usage=TokenUsage(**counts),
        duration_ms=812.4,
        attempts=1,
        response_id="resp_1",
        request_id="req_1",
    )


def test_record_holds_usage_cost_and_ids_only() -> None:
    tracker = UsageTracker({"gpt-5.4-mini": PRICE}, currency="EUR")

    entry = record(tracker)

    assert entry.estimated_cost == pytest.approx((1000 * 2.0 + 100 * 8.0) / 1e6)
    assert (entry.cost_currency, entry.pricing_model) == ("EUR", "gpt-5.4-mini")
    assert (entry.model, entry.status, entry.attempts, entry.duration_ms) == (
        "gpt-5.4-mini",
        "completed",
        1,
        812.4,
    )
    assert entry.timestamp.tzinfo is not None
    fields = {f.name for f in dataclasses.fields(entry)}
    assert fields == {
        "timestamp",
        "model",
        "status",
        "usage",
        "duration_ms",
        "attempts",
        "response_id",
        "request_id",
        "estimated_cost",
        "cost_currency",
        "pricing_model",
    }  # fmt: skip  (no question, prompt, answer or user identifier)


def test_unpriced_record_has_no_cost_or_currency() -> None:
    entry = record(UsageTracker(), model="gpt-4.1-mini")

    assert (entry.estimated_cost, entry.cost_currency, entry.pricing_model) == (None, None, None)


def test_summary_totals_per_model_and_overall() -> None:
    tracker = UsageTracker({"gpt-5.4-mini": PRICE})
    record(tracker)
    record(tracker)
    record(tracker, model="gpt-4.1-mini")  # unpriced
    record(tracker, model="gpt-4.1-mini", input_tokens=None, output_tokens=None, total_tokens=None)

    summary = tracker.summary()

    priced = summary["models"]["gpt-5.4-mini"]
    unpriced = summary["models"]["gpt-4.1-mini"]
    assert (priced["requests"], priced["input_tokens"], priced["priced_requests"]) == (2, 2000, 2)
    assert priced[COST_LABEL] == pytest.approx(2 * (1000 * 2.0 + 100 * 8.0) / 1e6)
    assert (unpriced["requests"], unpriced["requests_without_usage"], unpriced[COST_LABEL]) == (
        2,
        1,
        None,
    )
    overall = summary["overall"]
    assert (overall["requests"], overall["total_tokens"], overall["priced_requests"]) == (
        4,
        3300,
        2,
    )
    assert overall[COST_LABEL] == pytest.approx(priced[COST_LABEL])
    assert summary["cost_note"] == "Estimated from configured prices; not a bill."


def test_summary_cost_is_none_without_any_priced_request() -> None:
    tracker = UsageTracker()
    record(tracker)

    assert tracker.summary()["overall"][COST_LABEL] is None


def test_recent_records_are_bounded() -> None:
    tracker = UsageTracker(max_recent=3)
    for index in range(5):
        record(tracker, input_tokens=index)

    assert [entry.usage.input_tokens for entry in tracker.recent()] == [2, 3, 4]


@pytest.mark.parametrize("max_recent", [0, 10_001])
def test_invalid_buffer_size_is_rejected(max_recent: int) -> None:
    with pytest.raises(ValueError):
        UsageTracker(max_recent=max_recent)


def test_concurrent_records_are_all_counted() -> None:
    tracker = UsageTracker({"gpt-5.4-mini": PRICE})

    def work() -> None:
        for _ in range(50):
            record(tracker)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    totals = tracker.summary()["models"]["gpt-5.4-mini"]
    assert (totals["requests"], totals["input_tokens"]) == (400, 400_000)


def test_log_line_is_labelled_and_has_no_text(caplog: pytest.LogCaptureFixture) -> None:
    tracker = UsageTracker({"gpt-5.4-mini": PRICE})

    with caplog.at_level(logging.INFO, logger="app.services.usage_tracker"):
        record(tracker, reasoning_tokens=40)
        record(tracker, model="unpriced-model")

    assert "event=llm_usage model=gpt-5.4-mini status=completed input_tokens=1000" in caplog.text
    assert "reasoning_tokens=40" in caplog.text
    assert "estimated_cost=0.002800 currency=USD" in caplog.text
    assert "model=unpriced-model" in caplog.text and "estimated_cost=- currency=-" in caplog.text


# --- Settings ---------------------------------------------------------------------------------


def test_pricing_is_empty_by_default(make_settings: Callable[..., Settings]) -> None:
    settings = make_settings()

    assert (settings.llm_pricing, settings.llm_pricing_currency) == ({}, "USD")
    assert settings.safe_summary()["llm_priced_models"] == []


def test_pricing_is_read_from_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "LLM_PRICING",
        json.dumps(
            {" gpt-5.4-mini ": {"input_per_million_tokens": 2, "output_per_million_tokens": 8}}
        ),
    )
    monkeypatch.setenv("LLM_PRICING_CURRENCY", "EUR")

    settings = Settings(_env_file=None)

    assert settings.llm_pricing == {
        "gpt-5.4-mini": ModelPricing(input_per_million_tokens=2, output_per_million_tokens=8)
    }
    assert settings.safe_summary()["llm_priced_models"] == ["gpt-5.4-mini"]
    tracker = UsageTracker.from_settings(settings)
    assert tracker.currency == "EUR" and tracker.pricing_for("gpt-5.4-mini") is not None


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("LLM_PRICING", '{"m": {"input_per_million_tokens": -1, "output_per_million_tokens": 1}}'),
        ("LLM_PRICING", '{"m": {"input_per_million_tokens": 1}}'),  # output price missing
        (
            "LLM_PRICING",
            '{"m": {"input_per_million_tokens": 1, "output_per_million_tokens": 1, "x": 1}}',
        ),
        (
            "LLM_PRICING",
            '{"m": {"input_per_million_tokens": "NaN", "output_per_million_tokens": 1}}',
        ),
        ("LLM_PRICING", '{" ": {"input_per_million_tokens": 1, "output_per_million_tokens": 1}}'),
        ("LLM_PRICING", "not json"),
        ("LLM_PRICING_CURRENCY", "dollars"),
    ],
)
def test_invalid_pricing_fails_at_startup(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises((ValidationError, ValueError)):
        Settings(_env_file=None)


def test_env_example_ships_no_prices() -> None:
    from app.core.config import PROJECT_ROOT

    settings = Settings(_env_file=PROJECT_ROOT / ".env.example")

    assert settings.llm_pricing == {}


# --- Through LLMService and the real SDK ------------------------------------------------------


def sdk_service(body: dict[str, Any], tracker: UsageTracker | None) -> tuple[LLMService, Any]:
    client = openai.OpenAI(
        api_key=KEY.get_secret_value(),
        max_retries=0,
        http_client=httpx2.Client(
            transport=httpx2.MockTransport(lambda request: httpx2.Response(200, json=body))
        ),
    )
    return LLMService(
        KEY, model="gpt-5.4-mini", usage_tracker=tracker, client_factory=lambda k: client
    ), client


def response_body(status: str = "completed", usage: dict[str, Any] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "resp_7",
        "object": "response",
        "created_at": 1760000000,
        "status": status,
        "model": "gpt-5.4-mini-2026-03-17",
        "output": [
            {
                "type": "message",
                "id": "msg_1",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "An answer.", "annotations": []}],
            }
        ],
    }
    if usage is not None:
        body["usage"] = usage
    return body


SDK_USAGE = {
    "input_tokens": 900,
    "input_tokens_details": {"cached_tokens": 100, "cache_write_tokens": 0},
    "output_tokens": 250,
    "output_tokens_details": {"reasoning_tokens": 90},
    "total_tokens": 1150,
}


def test_llm_service_records_real_sdk_usage_and_cost() -> None:
    tracker = UsageTracker({"gpt-5.4-mini": PRICE})
    llm, client = sdk_service(response_body(usage=SDK_USAGE), tracker)

    try:
        result = llm.generate(instructions="i", input=CUSTOMER_TEXT)
    finally:
        client.close()

    assert (result.input_tokens, result.output_tokens, result.total_tokens) == (900, 250, 1150)
    assert result.usage.cached_input_tokens == 100 and result.usage.reasoning_tokens == 90
    expected = (800 * 2.0 + 100 * 0.5 + 250 * 8.0) / 1e6
    assert result.estimated_cost == pytest.approx(expected)
    assert result.cost_currency == "USD"
    [entry] = tracker.recent()
    assert (entry.model, entry.pricing_model) == ("gpt-5.4-mini-2026-03-17", "gpt-5.4-mini")
    assert "jane.doe" not in repr(entry) and "4711" not in repr(entry)


def test_rejected_responses_are_still_recorded() -> None:
    tracker = UsageTracker({"gpt-5.4-mini": PRICE})
    body = response_body(status="incomplete", usage=SDK_USAGE)
    body["incomplete_details"] = {"reason": "max_output_tokens"}
    llm, client = sdk_service(body, tracker)

    try:
        with pytest.raises(LLMResponseError):
            llm.generate(instructions="i", input="q")
    finally:
        client.close()

    [entry] = tracker.recent()
    assert (entry.status, entry.usage.output_tokens) == ("incomplete", 250)
    assert entry.estimated_cost is not None


def test_response_without_usage_is_recorded_without_counts() -> None:
    tracker = UsageTracker({"gpt-5.4-mini": PRICE})
    llm, client = sdk_service(response_body(usage=None), tracker)

    try:
        result = llm.generate(instructions="i", input="q")
    finally:
        client.close()

    assert (result.input_tokens, result.output_tokens, result.total_tokens) == (None, None, None)
    assert result.estimated_cost is None
    assert tracker.summary()["overall"]["requests_without_usage"] == 1


def test_llm_service_works_without_a_tracker() -> None:
    llm, client = sdk_service(response_body(usage=SDK_USAGE), None)

    try:
        result = llm.generate(instructions="i", input="q")
    finally:
        client.close()

    assert result.input_tokens == 900
    assert (result.estimated_cost, result.cost_currency) == (None, None)


# --- Wiring -----------------------------------------------------------------------------------


def test_app_shares_one_tracker_with_the_llm_service(
    make_settings: Callable[..., Settings],
) -> None:
    app = create_app(make_settings())

    tracker = app.state.usage_tracker
    assert isinstance(tracker, UsageTracker)
    assert app.state.llm_service.usage_tracker is tracker
    assert get_usage_tracker(SimpleNamespace(app=app)) is tracker  # type: ignore[arg-type]


def test_usage_tracker_dependency_fails_loudly_without_create_app() -> None:
    bare = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    with pytest.raises(RuntimeError, match="UsageTracker is not initialised"):
        get_usage_tracker(bare)  # type: ignore[arg-type]
