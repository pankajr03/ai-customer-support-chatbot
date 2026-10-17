"""Build the keyword, hybrid and answer decision services from settings.

Shared by ``create_app()`` and the evaluation scripts, so an evaluation measures exactly
the retrieval and decision configuration the API uses.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from app.core.config import Settings
from app.models.faq import FAQ
from app.services.answer_decision_service import (
    AcceptancePolicy,
    AmbiguityPolicy,
    AnswerDecisionService,
    FallbackMessages,
)
from app.services.faq_catalog import FAQCatalog
from app.services.hybrid_search_service import HybridSearchService
from app.services.keyword_search_service import KeywordSearchService
from app.services.semantic_search_service import SemanticSearchService


@dataclass(frozen=True, slots=True)
class SearchServices:
    keyword: KeywordSearchService
    hybrid: HybridSearchService
    decision: AnswerDecisionService


def build_search_services(
    settings: Settings,
    faq_provider: Callable[[], Sequence[FAQ]],
    semantic: SemanticSearchService,
    catalog: FAQCatalog | None = None,
) -> SearchServices:
    """Wire keyword retrieval, hybrid ranking and the answer decision. Loads no model.

    ``catalog``: the FAQ catalogue behind ``faq_provider``, so each decision pins one
    snapshot of it (see ``app.services.faq_catalog``).
    """
    keyword = KeywordSearchService(faq_provider)
    hybrid = HybridSearchService(
        keyword,
        semantic,
        rrf_k=settings.hybrid_rrf_k,
        candidate_depth=settings.hybrid_candidate_depth,
    )
    # Answer acceptance, ambiguity detection and fallback texts are configured separately
    # from retrieval ranking (rrf_k and candidate depth above).
    decision = AnswerDecisionService(
        hybrid,
        keyword,
        acceptance=AcceptancePolicy(
            min_similarity=settings.answer_min_similarity,
            accept_exact_question=settings.answer_accept_exact_question,
            degraded_min_keyword_score=settings.degraded_min_keyword_score,
            degraded_min_query_terms=settings.degraded_min_query_terms,
        ),
        ambiguity=AmbiguityPolicy(
            margin=settings.answer_ambiguity_margin,
            max_options=settings.answer_max_clarification_options,
        ),
        messages=FallbackMessages(
            no_match=settings.fallback_no_match_message,
            unavailable=settings.fallback_unavailable_message,
        ),
        candidates=settings.answer_candidates,
        catalog=catalog,
    )
    return SearchServices(keyword=keyword, hybrid=hybrid, decision=decision)
