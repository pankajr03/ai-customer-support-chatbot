"""The frontend's view of the API against the backend's contract (Stage 9, Task 2).

The Streamlit client keeps its own small response models (``frontend.api_client``)
instead of importing ``app.models``, so it never loads the backend's services. These
tests are what keeps the two from drifting apart: every field, enum value, route, error
code and limit the frontend relies on is checked against the backend's own models and
routes. The audit behind them is docs/integration-compatibility.md.
"""

import re
import types
import uuid
from collections.abc import Callable
from typing import Annotated, Any, Literal, Union, get_args, get_origin

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.core.config import DEFAULT_MAX_CUSTOMER_MESSAGE_CHARACTERS
from app.models.chat import (
    ChatDecision,
    ChatSourceReference,
    CustomerChatRequest,
    CustomerChatResponse,
)
from app.models.chat import strip_message as backend_strip_message
from app.models.conversation import (
    MAX_PAGE_SIZE,
    ConversationCreateResponse,
    ConversationHistoryMessage,
    ConversationHistoryResponse,
    ConversationSessionResponse,
    SessionStatus,
)
from app.models.errors import ErrorCode
from app.models.escalation import EscalationOffer
from app.models.health import HealthResponse, ReadinessChecks, ReadinessResponse
from app.models.support import CustomerTicketView
from app.models.support_api import SupportTicketCreatedResponse, SupportTicketCreateRequest
from app.services.answer_decision_service import Decision as InternalDecision
from frontend import api_client as fe
from frontend.config import FrontendSettings
from frontend.state import strip_message
from tests.test_api_workflows import RESET

# A support request as the page builds it (frontend.support.SupportForm.body).
TICKET_REQUEST = {
    "ticket_type": "support",
    "lead_category": None,
    "subject": "Refund assistance",
    "description": "I need help with a refund request.",
    "contact": {
        "name": "Example Customer",
        "email": "customer@example.com",
        "phone": None,
        "contact_preference": "email",
        "consent_given": True,
    },
    "confirmed": True,
}

# frontend model -> the backend response model it reads
PAIRS: list[tuple[type[BaseModel], type[BaseModel]]] = [
    (fe.HealthStatus, HealthResponse),
    (fe.ReadinessStatus, ReadinessResponse),
    (fe.ReadinessChecks, ReadinessChecks),
    (fe.ConversationCreated, ConversationCreateResponse),
    (fe.ConversationInfo, ConversationSessionResponse),
    (fe.SourceReference, ChatSourceReference),
    (fe.ChatReply, CustomerChatResponse),
    (fe.HistoryMessage, ConversationHistoryMessage),
    (fe._HistoryPage, ConversationHistoryResponse),
    (fe.EscalationOffer, EscalationOffer),  # Stage 10
    (fe.TicketCreated, SupportTicketCreatedResponse),
    (fe.CustomerTicket, CustomerTicketView),
]
# Filled in by the client itself (from the Idempotent-Replayed header), not the body.
CLIENT_SIDE_FIELDS = {"replayed"}


def _nullable(annotation: Any) -> bool:
    origin = get_origin(annotation)
    if origin is Annotated:
        return _nullable(get_args(annotation)[0])
    if origin in (Union, types.UnionType):
        return any(arg is type(None) for arg in get_args(annotation))
    return annotation is type(None)


def _literal_values(model: type[BaseModel], field: str) -> set[str]:
    annotation = model.model_fields[field].annotation
    assert get_origin(annotation) is Literal
    return set(get_args(annotation))


@pytest.mark.parametrize(("frontend", "backend"), PAIRS, ids=lambda m: m.__name__)
def test_every_field_the_frontend_reads_is_sent_by_the_backend(
    frontend: type[BaseModel], backend: type[BaseModel]
) -> None:
    for name, field in frontend.model_fields.items():
        if name in CLIENT_SIDE_FIELDS:
            continue
        assert name in backend.model_fields, f"{backend.__name__} has no {name}"
        if not _nullable(field.annotation):
            # The frontend requires a value: the backend must never send null.
            assert not _nullable(backend.model_fields[name].annotation), name


@pytest.mark.parametrize(("frontend", "backend"), PAIRS, ids=lambda m: m.__name__)
def test_the_backends_documented_examples_parse_on_the_frontend(
    frontend: type[BaseModel], backend: type[BaseModel]
) -> None:
    extra = backend.model_config.get("json_schema_extra")
    examples = extra.get("examples", []) if isinstance(extra, dict) else []
    for example in examples:
        frontend.model_validate(example)


def test_enums_have_the_same_values_on_both_sides() -> None:
    decisions = {d.value for d in fe.Decision}
    assert decisions == {d.value for d in ChatDecision}  # chat replies
    assert decisions == {d.value for d in InternalDecision}  # history messages
    assert {s.value for s in fe.ConversationStatus} == {s.value for s in SessionStatus}
    assert _literal_values(fe.HealthStatus, "status") == _literal_values(HealthResponse, "status")
    assert _literal_values(fe.ReadinessStatus, "status") == _literal_values(
        ReadinessResponse, "status"
    )


@pytest.mark.parametrize("status", ["ready", "not_ready"])
@pytest.mark.parametrize("semantic", sorted(_literal_values(ReadinessChecks, "semantic_search")))
def test_every_readiness_state_has_a_page_status(status: str, semantic: str) -> None:
    readiness = fe.ReadinessStatus.model_validate(
        {
            "status": status,
            "service": "s",
            "checks": {
                "knowledge_base": "ok",
                "conversation_storage": "ok",
                "semantic_search": semantic,
                "llm": "disabled",
            },
        }
    )
    page_status = fe.BackendStatus.from_readiness(readiness)
    if status == "not_ready":
        assert page_status is fe.BackendStatus.UNAVAILABLE
    else:
        expected = {
            "ready": fe.BackendStatus.ONLINE,
            "not_loaded": fe.BackendStatus.WARMING_UP,
            "unavailable": fe.BackendStatus.LIMITED,
        }
        assert page_status is expected[semantic]


# --- Errors ---------------------------------------------------------------------------

# Every (status, code) the backend sends -> what the page does with it.
BACKEND_ERRORS: dict[tuple[int, ErrorCode], fe.ErrorKind] = {
    (400, ErrorCode.VALIDATION_ERROR): fe.ErrorKind.CLIENT_ERROR,  # never sent by the page
    (422, ErrorCode.VALIDATION_ERROR): fe.ErrorKind.INVALID_MESSAGE,
    (401, ErrorCode.UNAUTHORIZED_SESSION): fe.ErrorKind.UNAUTHORIZED,
    (404, ErrorCode.SESSION_NOT_FOUND): fe.ErrorKind.SESSION_NOT_FOUND,
    (410, ErrorCode.SESSION_EXPIRED): fe.ErrorKind.SESSION_EXPIRED,
    (409, ErrorCode.SESSION_CLOSED): fe.ErrorKind.SESSION_CLOSED,
    (422, ErrorCode.DUPLICATE_MESSAGE_CONFLICT): fe.ErrorKind.MESSAGE_CONFLICT,
    (409, ErrorCode.REQUEST_IN_PROGRESS): fe.ErrorKind.BUSY,
    (429, ErrorCode.RATE_LIMITED): fe.ErrorKind.RATE_LIMITED,
    (404, ErrorCode.NOT_FOUND): fe.ErrorKind.CLIENT_ERROR,  # wrong BACKEND_API_URL/prefix
    (405, ErrorCode.METHOD_NOT_ALLOWED): fe.ErrorKind.CLIENT_ERROR,
    (413, ErrorCode.PAYLOAD_TOO_LARGE): fe.ErrorKind.INVALID_MESSAGE,
    (503, ErrorCode.SERVICE_UNAVAILABLE): fe.ErrorKind.SERVICE_UNAVAILABLE,
    (500, ErrorCode.INTERNAL_ERROR): fe.ErrorKind.SERVER_ERROR,
    # Stage 10. The page never calls staff endpoints; their codes mean a frontend bug.
    (401, ErrorCode.STAFF_AUTH_REQUIRED): fe.ErrorKind.UNAUTHORIZED,
    (403, ErrorCode.STAFF_ACCESS_DISABLED): fe.ErrorKind.FORBIDDEN,
    (404, ErrorCode.TICKET_NOT_FOUND): fe.ErrorKind.TICKET_NOT_FOUND,
    (409, ErrorCode.INVALID_STATUS_TRANSITION): fe.ErrorKind.CLIENT_ERROR,
    (409, ErrorCode.TICKET_CHANGED): fe.ErrorKind.CLIENT_ERROR,
    (422, ErrorCode.TICKET_REQUEST_CONFLICT): fe.ErrorKind.REQUEST_CONFLICT,
    # Stage 11 admin endpoints: never called by the customer page.
    (403, ErrorCode.STAFF_FORBIDDEN): fe.ErrorKind.FORBIDDEN,
    (404, ErrorCode.FAQ_NOT_FOUND): fe.ErrorKind.CLIENT_ERROR,
    (409, ErrorCode.FAQ_VERSION_CONFLICT): fe.ErrorKind.CLIENT_ERROR,
    (409, ErrorCode.FAQ_LAST_ACTIVE): fe.ErrorKind.CLIENT_ERROR,
    (409, ErrorCode.FAQ_DUPLICATE_QUESTION): fe.ErrorKind.CLIENT_ERROR,
    (409, ErrorCode.INVALID_ASSIGNMENT): fe.ErrorKind.CLIENT_ERROR,
    (409, ErrorCode.TICKET_ANONYMIZED): fe.ErrorKind.CLIENT_ERROR,
}


def test_every_backend_error_code_is_handled_by_the_frontend() -> None:
    # A new backend code fails here until the frontend decides what it means.
    assert {code for _, code in BACKEND_ERRORS} == set(ErrorCode)


@pytest.mark.parametrize(("status", "code"), list(BACKEND_ERRORS), ids=str)
def test_backend_errors_map_to_the_intended_kind(status: int, code: ErrorCode) -> None:
    body = {"error": {"code": code.value, "message": "m", "request_id": "r1"}}
    error = fe.error_from_response(httpx.Response(status, json=body))
    assert error.kind is BACKEND_ERRORS[(status, code)]
    assert error.code == code.value and error.request_id == "r1"


# --- Routes, headers and requests, against the real backend ---------------------------


# The status checks' short timeouts mean nothing to the in-process TestClient.
@pytest.mark.filterwarnings("ignore:You should not use the 'timeout' argument")
def test_every_call_matches_a_backend_route_method_and_header(
    make_backend: Callable[..., TestClient],
) -> None:
    backend = make_backend()
    sent: list[httpx.Request] = []
    backend.event_hooks = {"request": [sent.append], "response": []}
    client = fe.ChatApiClient(http_client=backend, sleep=lambda seconds: None)

    client.backend_status()
    client.check_health()
    credentials = client.create_conversation().credentials()
    client.send_message(credentials, RESET, uuid.uuid4())
    client.get_conversation(credentials)
    client.get_history(credentials)
    ticket = client.create_ticket(credentials, TICKET_REQUEST, uuid.uuid4())
    client.get_ticket(credentials, ticket.ticket_id)
    client.close_conversation(credentials)
    client.delete_conversation(credentials)
    backend.event_hooks = {"request": [], "response": []}

    operations = {
        (method.upper(), path)
        for path, item in backend.get("/openapi.json").json()["paths"].items()
        for method in item
    }
    called = set()
    for request in sent:
        operation = _operation(request, operations)
        called.add(operation)
        public = operation[1] in ("/api/v1/ready", "/api/v1/health") or operation == (
            "POST",
            "/api/v1/conversations",
        )
        assert ("authorization" not in request.headers) is public, operation
        assert "token" not in str(request.url).lower()  # never in a URL
        if operation[1].endswith("/messages"):
            assert int(request.url.params["limit"]) <= MAX_PAGE_SIZE
        if operation[1].endswith("/chat"):
            assert request.headers["content-type"] == "application/json"
            CustomerChatRequest.model_validate_json(request.content)  # extra="forbid"
        if operation == ("POST", "/api/v1/support/tickets"):
            SupportTicketCreateRequest.model_validate_json(request.content)  # extra="forbid"
    assert called == {
        ("GET", "/api/v1/ready"),
        ("GET", "/api/v1/health"),
        ("POST", "/api/v1/conversations"),
        ("GET", "/api/v1/conversations/{session_id}"),
        ("GET", "/api/v1/conversations/{session_id}/messages"),
        ("POST", "/api/v1/conversations/{session_id}/chat"),
        ("POST", "/api/v1/conversations/{session_id}/close"),
        ("DELETE", "/api/v1/conversations/{session_id}"),
        ("POST", "/api/v1/support/tickets"),
        ("GET", "/api/v1/support/tickets/{ticket_id}"),
    }


def _operation(request: httpx.Request, operations: set[tuple[str, str]]) -> tuple[str, str]:
    for method, template in operations:
        pattern = "^" + re.sub(r"\{[^/]+\}", "[^/]+", template) + "$"
        if method == request.method and re.match(pattern, request.url.path):
            return method, template
    raise AssertionError(f"no backend route for {request.method} {request.url.path}")


def test_backend_timestamps_are_utc_with_an_offset(
    make_backend: Callable[..., TestClient],
) -> None:
    body = make_backend().post("/api/v1/conversations").json()
    assert body["created_at"].endswith("Z") and body["expires_at"].endswith("Z")


def test_a_timestamp_without_an_offset_is_a_contract_error() -> None:
    created = {
        "session_id": str(uuid.uuid4()),
        "access_token": "cst_x",
        "token_type": "Bearer",
        "status": "active",
        "created_at": "2026-10-15T09:30:00",  # naive: which time zone?
        "expires_at": None,
    }
    http = httpx.Client(
        base_url="http://backend.test",
        transport=httpx.MockTransport(lambda request: httpx.Response(201, json=created)),
    )
    with pytest.raises(fe.ApiError) as caught:
        fe.ChatApiClient(http_client=http).create_conversation()
    assert caught.value.kind is fe.ErrorKind.INVALID_RESPONSE


# --- Limits ---------------------------------------------------------------------------


def test_history_pages_use_the_backends_largest_page() -> None:
    assert fe.HISTORY_PAGE_SIZE == MAX_PAGE_SIZE


def test_the_default_message_limit_matches_the_backend() -> None:
    frontend = FrontendSettings(_env_file=None).max_message_characters
    assert frontend == DEFAULT_MAX_CUSTOMER_MESSAGE_CHARACTERS


@pytest.mark.parametrize(
    "text",
    [
        "  hello  ",
        "​﻿hello‍",
        "\n⁠ two\nlines ‌\t",
        "a‍b",  # an inner joiner is kept
        " ​ ",
    ],
)
def test_messages_are_trimmed_like_the_backend_trims_them(text: str) -> None:
    # So the length the page checks is the length the backend checks.
    assert strip_message(text) == backend_strip_message(text)
