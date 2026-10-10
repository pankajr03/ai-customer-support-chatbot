"""Checks for the bundled sample knowledge base in data/faqs.json.

These guard the data file itself, so a future client-specific replacement can be
validated the same way: it must parse, satisfy the FAQ models and avoid personal data.
"""

import json
import re
from collections import Counter

import pytest

from app.core.config import PROJECT_ROOT
from app.models.faq import FAQ, FAQCategory, FAQCollection

pytestmark = pytest.mark.sample_data

FAQ_FILE = PROJECT_ROOT / "data" / "faqs.json"
REQUIRED_CATEGORIES = {"orders", "shipping", "payments", "returns", "refunds", "account"}


@pytest.fixture(scope="module")
def raw_text() -> str:
    return FAQ_FILE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def faqs(raw_text: str) -> FAQCollection:
    return FAQCollection.model_validate_json(raw_text)


def test_file_is_valid_json_list(raw_text: str) -> None:
    data = json.loads(raw_text)

    assert isinstance(data, list)


def test_file_validates_against_faq_models(faqs: FAQCollection) -> None:
    # Validation, including duplicate-ID checks, happens in the fixture.
    assert all(isinstance(faq, FAQ) for faq in faqs.root)


def test_has_roughly_thirty_faqs(faqs: FAQCollection) -> None:
    assert 25 <= len(faqs) <= 40


def test_ids_are_unique_and_sequential(faqs: FAQCollection) -> None:
    ids = [faq.id for faq in faqs.root]

    assert len(ids) == len(set(ids))
    assert ids == [f"faq_{n:03d}" for n in range(1, len(ids) + 1)]


def test_file_is_stored_already_normalised(raw_text: str, faqs: FAQCollection) -> None:
    """The file should not rely on the model's normalisation to be correct."""
    assert json.loads(raw_text) == faqs.model_dump(mode="json")


def test_every_required_category_is_covered(faqs: FAQCollection) -> None:
    counts = Counter(faq.category.value for faq in faqs.active())

    assert REQUIRED_CATEGORIES <= counts.keys()
    assert all(counts[category] >= 3 for category in REQUIRED_CATEGORIES), counts
    assert set(counts) <= {category.value for category in FAQCategory}


def test_includes_inactive_example_for_filtering(faqs: FAQCollection) -> None:
    inactive = [faq for faq in faqs.root if not faq.is_active]

    assert 1 <= len(inactive) < len(faqs) // 5


def test_questions_are_unique(faqs: FAQCollection) -> None:
    questions = [faq.question.lower() for faq in faqs.root]

    assert len(questions) == len(set(questions))


def test_every_faq_has_several_keywords(faqs: FAQCollection) -> None:
    assert all(len(faq.keywords) >= 3 for faq in faqs.root)


def test_answers_are_substantial(faqs: FAQCollection) -> None:
    # Very short answers make poor retrieval targets and unhelpful replies.
    assert all(len(faq.answer) >= 80 for faq in faqs.root)


def test_contains_no_personal_or_contact_data(raw_text: str) -> None:
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", raw_text), "email address found"
    assert not re.search(r"\+?\d[\d\s().-]{7,}\d", raw_text), "phone-like number found"
    assert not re.search(r"https?://|www\.", raw_text), "URL found"
