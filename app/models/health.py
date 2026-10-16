"""Liveness (``GET /health``) and readiness (``GET /ready``) responses.

- Liveness: the process is running and answering HTTP. Nothing else is checked.
- Readiness: the process can answer chat requests. Only local dependencies decide it
  (the FAQ knowledge base and conversation storage). The embedding model and the LLM are
  reported but never block readiness: without them chat still works (keyword-only
  retrieval, approved FAQ answers), and checking them would mean loading a model or
  making a paid API call.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.errors import ErrorBody


class HealthResponse(BaseModel):
    """Result of a liveness check."""

    status: Literal["healthy"] = Field(examples=["healthy"])
    service: str = Field(examples=["ai-customer-support-chatbot"])


class ReadinessChecks(BaseModel):
    """The state of each dependency of a chat request. No secrets, paths or errors."""

    model_config = ConfigDict(extra="forbid")

    knowledge_base: Literal["ok", "unavailable"] = Field(
        description="ok: approved FAQs are loaded. unavailable: there are no active FAQs."
    )
    conversation_storage: Literal["ok", "unavailable"] = Field(
        description="ok: the conversation database opens and its tables exist."
    )
    semantic_search: Literal["ready", "not_loaded", "unavailable"] = Field(
        description="ready: the embedding model and FAQ index are in memory. not_loaded: "
        "they load on the first chat message, which is then slow (set "
        "SEMANTIC_INDEX_PRELOAD=true to load them at startup). unavailable: the model "
        "failed to load recently; chat answers from keyword search only or returns "
        "retrieval_unavailable until it loads. Never loads or downloads the model."
    )
    llm: Literal["disabled", "enabled", "paused"] = Field(
        description="disabled: LLM_ENABLED=false (approved FAQ answers are used). enabled: "
        "a key is configured; it is not tested (no paid call is made). paused: recent "
        "failures paused LLM calls for a cooldown; approved FAQ answers are used meanwhile."
    )


class ReadinessResponse(BaseModel):
    """Whether the service can answer chat requests (200 ready, 503 not ready).

    A 503 also carries the standard ``error`` object, so a client that handles every
    error response the same way (``{"error": {"code", ...}}``) still can.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "status": "ready",
                    "service": "ai-customer-support-chatbot",
                    "checks": {
                        "knowledge_base": "ok",
                        "conversation_storage": "ok",
                        "semantic_search": "not_loaded",
                        "llm": "disabled",
                    },
                }
            ]
        },
    )

    status: Literal["ready", "not_ready"] = Field(
        description="ready when knowledge_base and conversation_storage are ok."
    )
    service: str = Field(examples=["ai-customer-support-chatbot"])
    checks: ReadinessChecks
    error: ErrorBody | None = Field(
        default=None,
        description="Only when not ready (503): code SERVICE_UNAVAILABLE. Absent when ready.",
    )
