"""Liveness (``GET /health``), readiness (``GET /ready``) and public status
(``GET /status``) responses.

- Liveness: the process is running and answering HTTP. Nothing else is checked.
- Readiness: the process can answer chat requests. Only essential, local dependencies
  decide it (the FAQ knowledge base and conversation storage). The optional ones, the
  embedding model and the LLM, are reported but never block readiness: without them chat
  still works (keyword-only retrieval, approved FAQ answers), and checking them for real
  would mean loading a model or making a paid API call.
- Public status: the readiness verdict without the checks, for the public site.
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
        description="ok: the conversation database opens, its tables exist, and it can "
        "be written (the file and folder are writable and the disk is not almost full). "
        "Nothing is written to find out."
    )
    semantic_search: Literal["ready", "not_loaded", "unavailable"] = Field(
        description="ready: the embedding model and FAQ index are in memory. not_loaded: "
        "they load on the first chat message, which is then slow (set "
        "SEMANTIC_INDEX_PRELOAD=true to load them at startup). unavailable: the model "
        "failed to load recently, or it is configured as a local folder that holds no "
        "model (in Docker: model-init has not been run); chat answers from keyword "
        "search only or returns retrieval_unavailable until it loads. Never loads or "
        "downloads the model."
    )
    llm: Literal["disabled", "enabled", "paused"] = Field(
        description="disabled: LLM_ENABLED=false (approved FAQ answers are used). enabled: "
        "a key is configured; it is not tested (no paid call is made). paused: recent "
        "failures paused LLM calls for a cooldown; approved FAQ answers are used meanwhile."
    )


class PublicStatusResponse(BaseModel):
    """The public form of readiness (``GET /status``): the verdict, no checks.

    Published through the customer site, so it names no dependency and no state of one.
    """

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"status": "ready"}]})

    status: Literal["ready", "not_ready"] = Field(
        description="ready when chat requests can be answered (the same verdict as /ready)."
    )
    error: ErrorBody | None = Field(
        default=None,
        description="Only when not ready (503): code SERVICE_UNAVAILABLE. Absent when ready.",
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
