"""The conversation evaluation dataset and script (fake embeddings, no LLM, no network)."""

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.database.faq_repository import FAQRepository
from scripts import evaluate_conversations as ev
from tests.fakes import make_fake_embedding_service

DATASET = Path(__file__).resolve().parents[1] / "data" / "conversation_eval.json"
REQUIRED_TYPES = {
    "refund_follow_up",
    "shipping_follow_up",
    "return_policy_follow_up",
    "topic_switch",
    "ambiguous_reference",
    "out_of_scope",
    "unsupported_promise",
    "prompt_injection",
    "long_conversation",
    "expired_session",
    "cross_session",
}


@pytest.fixture(scope="module")
def dataset() -> ev.Dataset:
    return ev.load_dataset(DATASET)


def fake_factory(_: str) -> Any:
    return make_fake_embedding_service()[0]


def settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, database_url="sqlite://", **overrides)


# --- Dataset --------------------------------------------------------------------------


@pytest.mark.sample_data
def test_dataset_has_20_multi_turn_scenarios_of_every_required_type(dataset: ev.Dataset) -> None:
    assert len(dataset.scenarios) >= 20
    assert REQUIRED_TYPES <= {s.type for s in dataset.scenarios}
    multi_turn = [s for s in dataset.scenarios if len(s.turns) >= 2]
    assert len(multi_turn) >= 20
    assert max(len(s.turns) for s in dataset.scenarios) >= 10  # a long conversation


@pytest.mark.sample_data
def test_dataset_labels_match_the_real_faqs(dataset: ev.Dataset) -> None:
    repository = FAQRepository.from_json_file(DATASET.parents[0] / "faqs.json")
    s = Settings(_env_file=None)
    fallbacks = [s.fallback_no_match_message, s.fallback_unavailable_message]

    assert ev.check_labels(dataset, repository.list_all(), fallbacks) == []


@pytest.mark.sample_data
def test_dataset_labels_both_kinds_of_context(dataset: ev.Dataset) -> None:
    labels = Counter(t.context for s in dataset.scenarios for t in s.turns)

    assert labels["follow_up"] >= 10 and labels["standalone"] >= 10


def test_invalid_datasets_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ev.Dataset.model_validate({"description": "", "faq_data": "", "scenarios": []})
    with pytest.raises(ValidationError):
        ev.Turn.model_validate(
            {
                "message": "Hi",
                "context": "maybe",
                "acceptable_outcomes": ["answer"],
                "acceptable_source_ids": [],
                "must_include": [],
                "must_not_include": [],
            }
        )


def make_turn(**overrides: Any) -> dict[str, Any]:
    return {
        "message": "How can I track my order?",
        "context": "standalone",
        "acceptable_outcomes": ["answer"],
        "acceptable_source_ids": ["faq_101"],
        "must_include": [],
        "must_not_include": [],
    } | overrides


def make_dataset(*scenarios: dict[str, Any]) -> ev.Dataset:
    return ev.Dataset.model_validate(
        {"description": "test", "faq_data": "faqs.json", "scenarios": list(scenarios)}
    )


def scenario(id: str = "s1", **overrides: Any) -> dict[str, Any]:
    return {"id": id, "type": "t", "explanation": "e", "turns": [make_turn()]} | overrides


def test_label_problems_are_reported(faq_repository: FAQRepository) -> None:
    bad = make_dataset(
        scenario("dup"),
        scenario("dup"),
        scenario("s2", turns=[make_turn(acceptable_source_ids=["faq_999"])]),
        scenario("s3", turns=[make_turn(acceptable_source_ids=[])]),
        scenario("s4", turns=[make_turn(must_not_include=["My Orders"])]),
        scenario("s5", turns=[make_turn(session="B")]),
        scenario("s6", check="cross_session"),
        scenario("s7", turns=[make_turn(must_not_include=["sorry"])]),
    )

    problems = ev.check_labels(bad, faq_repository.list_all(), ["Sorry, nothing found."])

    text = "\n".join(problems)
    assert "duplicate scenario id dup" in text
    assert "faq_999 is not an active FAQ" in text
    assert "s3 turn 1: answers allowed but no acceptable sources" in text
    assert "s4 turn 1: forbidden phrase 'My Orders' is in an allowed reply" in text
    assert "s5: only cross_session scenarios may use session B" in text
    assert "s6: cross_session needs turns in sessions A and B" in text
    assert "s7 turn 1: forbidden phrase 'sorry'" in text


# --- Running --------------------------------------------------------------------------


@pytest.fixture
def small_dataset(tmp_path: Path) -> Path:
    data = {
        "description": "test",
        "faq_data": "faqs.json",
        "scenarios": [
            scenario(
                "a",
                turns=[
                    make_turn(),  # exact FAQ question: accepted as an exact match
                    make_turn(
                        message="Where is it now?",
                        context="follow_up",
                        acceptable_outcomes=["answer", "fallback", "clarification"],
                    ),
                ],
            ),
            scenario(
                "b",
                check="expired_session",
                turns=[
                    make_turn(
                        message="Do you sell rockets?",
                        acceptable_outcomes=["fallback", "clarification"],
                        acceptable_source_ids=[],
                    )
                ],
            ),
            scenario(
                "c",
                check="cross_session",
                turns=[
                    make_turn(),
                    make_turn(
                        session="B",
                        message="How do I reset my password?",
                        acceptable_source_ids=["faq_107"],
                    ),
                ],
            ),
        ],
    }
    path = tmp_path / "conversation_eval.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def run(small_dataset: Path, faq_file: Path, **overrides: Any) -> dict[str, Any]:
    return ev.evaluate(
        small_dataset, faq_file, settings(**overrides), embedding_factory=fake_factory
    )


def test_report_has_every_metric_with_denominators(small_dataset: Path, faq_file: Path) -> None:
    report = run(small_dataset, faq_file)

    metrics = report["metrics"]
    assert report["meta"]["turns"] == 5 and report["meta"]["scenarios"] == 3
    for name in (
        "context_resolution",
        "outcome_correct",
        "fallback_correct",
        "retrieval_correct",
        "answer_relevance",
        "grounding_passed",
        "no_forbidden_phrases",
        "isolation_checks",
        "expiry_checks",
        "history_within_budget",
    ):
        assert set(metrics[name]) == {"n", "of", "rate"}
    assert metrics["expiry_checks"] == {"n": 6, "of": 6, "rate": 1.0}
    assert metrics["isolation_checks"]["of"] == 8
    assert metrics["isolation_checks"]["n"] == 8
    assert metrics["history_within_budget"]["of"] == 5
    assert metrics["latency_ms"]["n"] == 5
    first = report["turns"][0]
    assert first["outcome"] == "answer" and first["source_ids"] == ["faq_101"]
    assert first["source_correct"] is True and first["grounding_passed"] is True
    assert report["turns"][1]["probe"]["rewritten"] is True  # "it" resolved from turn 1


def test_report_text(small_dataset: Path, faq_file: Path) -> None:
    text = ev.format_report(run(small_dataset, faq_file))

    for heading in (
        "Context resolution",
        "Retrieval correct",
        "Session isolation",
        "Expiry checks",
        "History within budget",
        "Latency per /chat",
    ):
        assert heading in text
    assert "not proof of reliability" in text


def test_failures_are_listed(small_dataset: Path, faq_file: Path) -> None:
    report = run(small_dataset, faq_file)
    report["turns"][0].update(
        outcome_correct=False,
        outcome="fallback",
        context_correct=False,
        source_correct=False,
        relevance_correct=False,
        grounding_passed=False,
        grounding_issues=["number"],
        forbidden_found=["x"],
    )
    report["checks"][0].update(passed=False, detail="expected 410, got 200")

    text = ev.format_report(report)

    assert "Turns with at least one failed check (" in text
    assert f"{report['turns'][0]['scenario_id']} turn 1:" in text
    assert "outcome=fallback" in text and "grounding=['number']" in text
    assert "Failed scenario checks" in text and "expected 410, got 200" in text


def test_tight_history_limits_are_respected(small_dataset: Path, faq_file: Path) -> None:
    report = run(small_dataset, faq_file, conversation_history_max_messages=1)

    assert report["metrics"]["history_within_budget"]["n"] == 5


def test_invalid_labels_stop_the_evaluation(tmp_path: Path, faq_file: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps(
            {
                "description": "",
                "faq_data": "",
                "scenarios": [scenario(turns=[make_turn(acceptable_source_ids=["faq_999"])])],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Invalid evaluation labels"):
        ev.evaluate(path, faq_file, settings(), embedding_factory=fake_factory)


# --- Command line ---------------------------------------------------------------------


def test_main_writes_the_report(
    small_dataset: Path, faq_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "report.json"

    code = ev.main(
        ["--dataset", str(small_dataset), "--faq-data", str(faq_file), "--output", str(output)],
        embedding_factory=fake_factory,
        settings=settings(),
    )

    assert code == 0
    assert "Conversation evaluation" in capsys.readouterr().out
    assert json.loads(output.read_text(encoding="utf-8"))["meta"]["mode"] == "off"


def test_live_mode_requires_confirmation(capsys: pytest.CaptureFixture[str]) -> None:
    assert ev.main(["--llm", "live"]) == 2
    assert "PAID" in capsys.readouterr().err


@pytest.fixture
def no_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """main() builds Settings itself: make it ignore the developer's .env (which may hold
    a real OPENAI_API_KEY), so these tests can never reach a live configuration."""
    real = ev.Settings
    monkeypatch.setattr(ev, "Settings", lambda **kw: real(_env_file=None, **kw))


@pytest.mark.usefixtures("no_env_file")
def test_live_mode_without_a_key_is_a_configuration_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert ev.main(["--llm", "live", "--yes"]) == 2
    assert "Invalid configuration" in capsys.readouterr().err


@pytest.mark.usefixtures("no_env_file")
def test_default_settings_never_use_the_real_database(
    monkeypatch: pytest.MonkeyPatch, small_dataset: Path, faq_file: Path
) -> None:
    captured: dict[str, Settings] = {}
    real_evaluate = ev.evaluate

    def fake_evaluate(dataset: Path, faq: Path, s: Settings, **_: Any) -> dict[str, Any]:
        captured["settings"] = s
        return real_evaluate(small_dataset, faq_file, settings(), embedding_factory=fake_factory)

    monkeypatch.delenv("DATABASE_URL")
    monkeypatch.setattr(ev, "evaluate", fake_evaluate)

    assert ev.main(["--offline"]) == 0
    assert captured["settings"].database_url == "sqlite://"
    assert not captured["settings"].conversation_cleanup_on_startup
    assert not captured["settings"].llm_enabled
