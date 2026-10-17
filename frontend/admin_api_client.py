"""HTTP client for the staff admin dashboard (Stage 11), used by ``frontend/admin_app.py``.

Endpoints (all need a staff token; the backend enforces the role on every request):

    GET   /api/v1/admin/me                          -> who the token belongs to
    GET   /api/v1/admin/overview                    -> counts (admin)
    GET   /api/v1/admin/faqs                        -> FAQs, filtered, paginated (admin)
    POST  /api/v1/admin/faqs                        -> create (admin)
    GET   /api/v1/admin/faqs/{id}                   -> one FAQ with its version (admin)
    PATCH /api/v1/admin/faqs/{id}                   -> edit with expected_version (admin)
    POST  /api/v1/admin/faqs/{id}/deactivate|activate                         (admin)
    GET   /api/v1/admin/faqs/{id}/revisions         -> change history (admin)
    GET   /api/v1/support/tickets                   -> ticket list (admin, support)
    GET   /api/v1/support/tickets/{id}              -> one ticket with contact details
    PATCH /api/v1/support/tickets/{id}/status       -> status change
    GET   /api/v1/support/tickets/{id}/events       -> history

Like ``frontend.api_client`` this mirrors the contract instead of importing the backend;
tests/test_admin_frontend.py checks the models against the backend's.

Tokens: the staff token is a ``SecretStr`` passed per call and sent only as
``Authorization: Bearer``; never in a URL, a log line or an error. The pooled
``httpx.Client`` holds no token and refuses cookies, so one client is shared safely by
every browser session of the Streamlit server.

Errors become ``AdminApiError``: an ``AdminErrorKind``, the HTTP status, the backend's
stable code and its short, safe message, the request ID, and for validation errors the
fields and rules (never the submitted values). Logging: method, route, status, request
ID only.
"""

import logging
import time
import uuid
from enum import StrEnum
from typing import Any, Self, TypeVar

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, SecretStr, ValidationError

from frontend.api_client import _refuse_all_cookies

logger = logging.getLogger(__name__)

M = TypeVar("M", bound=BaseModel)

FAQ_CATEGORIES = (
    "general",
    "orders",
    "shipping",
    "returns",
    "refunds",
    "payments",
    "account",
    "products",
    "technical",
)
TICKET_STATUSES = (
    "open",
    "assigned",
    "in_progress",
    "waiting_for_customer",
    "resolved",
    "closed",
)
# The workflow's allowed status changes for staff (mirrors app.models.support).
STAFF_NEXT_STATUSES: dict[str, tuple[str, ...]] = {
    "open": ("assigned", "closed"),
    "assigned": ("in_progress", "open", "closed"),
    "in_progress": ("waiting_for_customer", "resolved", "closed"),
    "waiting_for_customer": ("in_progress", "resolved", "closed"),
    "resolved": ("closed", "in_progress"),
    "closed": (),
}
# Field limits, mirrored for form hints (the backend enforces them).
MAX_QUESTION_LENGTH = 500
MAX_ANSWER_LENGTH = 5000
MAX_KEYWORDS = 20
MAX_KEYWORD_LENGTH = 50


# --- Contract models ------------------------------------------------------------------


class _ApiModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class StaffIdentity(_ApiModel):
    staff_id: str
    role: str
    permissions: tuple[str, ...]
    auth_notice: str

    def can(self, permission: str) -> bool:
        return permission in self.permissions


class FAQRecord(_ApiModel):
    id: str
    question: str
    answer: str
    category: str
    keywords: tuple[str, ...]
    is_active: bool
    version: int
    created_at: AwareDatetime
    updated_at: AwareDatetime
    created_by: str | None
    updated_by: str | None


class FAQList(_ApiModel):
    faqs: tuple[FAQRecord, ...]
    total: int
    offset: int
    limit: int
    has_more: bool


class FAQRevision(_ApiModel):
    version: int
    action: str
    changed_fields: tuple[str, ...]
    previous_values: dict[str, Any] | None
    new_values: dict[str, Any]
    changed_by: str | None
    changed_at: AwareDatetime


class FAQRevisionList(_ApiModel):
    faq_id: str
    revisions: tuple[FAQRevision, ...]
    total: int
    has_more: bool


class CategoryCount(_ApiModel):
    category: str
    active: int
    inactive: int


class FAQSummary(_ApiModel):
    total: int
    active: int
    inactive: int
    by_category: tuple[CategoryCount, ...]
    last_updated_at: AwareDatetime | None


class TicketCounts(_ApiModel):
    total: int
    by_status: dict[str, int]
    by_type: dict[str, int]
    by_lead_category: dict[str, int]
    created_last_7_days: int
    open_queue: int


class Overview(_ApiModel):
    generated_at: AwareDatetime
    faqs: FAQSummary | None
    tickets: TicketCounts


class TicketSummary(_ApiModel):
    ticket_id: uuid.UUID
    reference_number: str
    ticket_type: str
    lead_category: str | None
    status: str
    priority: str
    assigned_to: str | None
    subject: str
    created_at: AwareDatetime
    updated_at: AwareDatetime


class TicketList(_ApiModel):
    tickets: tuple[TicketSummary, ...]
    total: int
    offset: int
    limit: int
    has_more: bool


class TicketContact(_ApiModel):
    name: str
    email: str
    phone: str | None
    contact_preference: str


class TicketDetail(TicketSummary):
    description: str
    contact: TicketContact | None


class TicketEvent(_ApiModel):
    sequence: int
    event_type: str
    previous_status: str | None
    new_status: str | None
    actor_type: str
    actor_id: str | None
    assigned_to: str | None
    created_at: AwareDatetime


class _TicketEvents(_ApiModel):
    events: tuple[TicketEvent, ...]


# --- Errors ---------------------------------------------------------------------------


class AdminErrorKind(StrEnum):
    UNREACHABLE = "unreachable"  # no connection: backend down or wrong URL
    TIMEOUT = "timeout"
    SIGNED_OUT = "signed_out"  # 401: the token is not (or no longer) a staff token
    STAFF_DISABLED = "staff_disabled"  # 403 STAFF_ACCESS_DISABLED: no staff configured
    FORBIDDEN = "forbidden"  # 403 STAFF_FORBIDDEN: the role does not allow it
    NOT_FOUND = "not_found"  # 404
    CONFLICT = "conflict"  # 409: changed meanwhile, or not allowed from here
    INVALID = "invalid"  # 422: see ``fields``
    UNAVAILABLE = "unavailable"  # 503
    SERVER_ERROR = "server_error"  # 500 and other errors
    INVALID_RESPONSE = "invalid_response"  # not the documented contract


class AdminApiError(Exception):
    """A failed admin call. Safe to show and log: no token, no submitted values."""

    def __init__(
        self,
        kind: AdminErrorKind,
        *,
        status_code: int | None = None,
        code: str | None = None,
        message: str | None = None,
        request_id: str | None = None,
        fields: tuple[str, ...] = (),
    ) -> None:
        self.kind = kind
        self.status_code = status_code
        self.code = code
        self.message = message
        self.request_id = request_id
        self.fields = fields
        status = f" HTTP {status_code}" if status_code is not None else ""
        super().__init__(f"{kind}{status} {code or ''}".rstrip())


_KINDS_BY_STATUS = {
    401: AdminErrorKind.SIGNED_OUT,
    404: AdminErrorKind.NOT_FOUND,
    409: AdminErrorKind.CONFLICT,
    422: AdminErrorKind.INVALID,
    503: AdminErrorKind.UNAVAILABLE,
}


def admin_error_from_response(response: httpx.Response) -> AdminApiError:
    code = message = request_id = None
    fields: list[str] = []
    try:
        error = response.json().get("error", {})
        code, message, request_id = error.get("code"), error.get("message"), error.get("request_id")
        for detail in error.get("details") or []:
            location = [str(part) for part in detail.get("loc", []) if part != "body"]
            fields.append(f"{'.'.join(location) or 'request'}: {detail.get('msg', 'invalid')}")
    except (ValueError, AttributeError):
        pass
    request_id = request_id or response.headers.get("X-Request-ID")
    if response.status_code == 403:
        kind = (
            AdminErrorKind.STAFF_DISABLED
            if code == "STAFF_ACCESS_DISABLED"
            else AdminErrorKind.FORBIDDEN
        )
    else:
        kind = _KINDS_BY_STATUS.get(response.status_code, AdminErrorKind.SERVER_ERROR)
    return AdminApiError(
        kind,
        status_code=response.status_code,
        code=code if isinstance(code, str) else None,
        message=message if isinstance(message, str) else None,
        request_id=request_id if isinstance(request_id, str) else None,
        fields=tuple(fields),
    )


# --- Client ---------------------------------------------------------------------------


class AdminApiClient:
    """Staff calls to the backend. One instance may serve every browser session."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8000",
        *,
        api_prefix: str = "/api/v1",
        connect_timeout: float = 5.0,
        read_timeout: float = 30.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._prefix = api_prefix
        self._http = http_client or httpx.Client(
            base_url=base_url,
            timeout=httpx.Timeout(read_timeout, connect=connect_timeout),
            cookies=_refuse_all_cookies(),
            follow_redirects=False,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # --- Identity and overview ---

    def who_am_i(self, token: SecretStr) -> StaffIdentity:
        return self._call("GET", "/admin/me", token, StaffIdentity)

    def overview(self, token: SecretStr) -> Overview:
        return self._call("GET", "/admin/overview", token, Overview)

    # --- FAQs ---

    def list_faqs(
        self,
        token: SecretStr,
        *,
        search: str | None = None,
        category: str | None = None,
        is_active: bool | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> FAQList:
        params: dict[str, Any] = {"offset": offset, "limit": limit}
        if search:
            params["q"] = search
        if category:
            params["category"] = category
        if is_active is not None:
            params["is_active"] = "true" if is_active else "false"
        return self._call("GET", "/admin/faqs", token, FAQList, params=params)

    def get_faq(self, token: SecretStr, faq_id: str) -> FAQRecord:
        return self._call("GET", f"/admin/faqs/{faq_id}", token, FAQRecord)

    def create_faq(
        self,
        token: SecretStr,
        *,
        question: str,
        answer: str,
        category: str,
        keywords: list[str],
        is_active: bool,
    ) -> FAQRecord:
        body = {
            "question": question,
            "answer": answer,
            "category": category,
            "keywords": keywords,
            "is_active": is_active,
        }
        return self._call("POST", "/admin/faqs", token, FAQRecord, json=body)

    def update_faq(
        self, token: SecretStr, faq_id: str, expected_version: int, **changes: Any
    ) -> FAQRecord:
        body = {"expected_version": expected_version, **changes}
        return self._call("PATCH", f"/admin/faqs/{faq_id}", token, FAQRecord, json=body)

    def set_faq_active(
        self, token: SecretStr, faq_id: str, expected_version: int, *, active: bool
    ) -> FAQRecord:
        action = "activate" if active else "deactivate"
        return self._call(
            "POST",
            f"/admin/faqs/{faq_id}/{action}",
            token,
            FAQRecord,
            json={"expected_version": expected_version},
        )

    def faq_revisions(
        self, token: SecretStr, faq_id: str, *, offset: int = 0, limit: int = 20
    ) -> FAQRevisionList:
        return self._call(
            "GET",
            f"/admin/faqs/{faq_id}/revisions",
            token,
            FAQRevisionList,
            params={"offset": offset, "limit": limit},
        )

    # --- Tickets ---

    def list_tickets(
        self,
        token: SecretStr,
        *,
        statuses: list[str] | None = None,
        ticket_type: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> TicketList:
        params: list[tuple[str, Any]] = [("offset", offset), ("limit", limit)]
        params += [("status", status) for status in statuses or []]
        if ticket_type:
            params.append(("ticket_type", ticket_type))
        return self._call("GET", "/support/tickets", token, TicketList, params=params)

    def get_ticket(self, token: SecretStr, ticket_id: uuid.UUID) -> TicketDetail:
        return self._call("GET", f"/support/tickets/{ticket_id}", token, TicketDetail)

    def ticket_events(self, token: SecretStr, ticket_id: uuid.UUID) -> tuple[TicketEvent, ...]:
        return self._call(
            "GET", f"/support/tickets/{ticket_id}/events", token, _TicketEvents
        ).events

    def change_ticket_status(
        self,
        token: SecretStr,
        ticket_id: uuid.UUID,
        status: str,
        *,
        expected_status: str,
        assigned_to: str | None = None,
    ) -> TicketSummary:
        body: dict[str, Any] = {"status": status, "expected_status": expected_status}
        if assigned_to:
            body["assigned_to"] = assigned_to
        return self._call(
            "PATCH", f"/support/tickets/{ticket_id}/status", token, TicketSummary, json=body
        )

    # --- Plumbing ---

    def _call(
        self,
        method: str,
        path: str,
        token: SecretStr,
        model: type[M],
        *,
        params: Any = None,
        json: Any = None,
    ) -> M:
        headers = {"Authorization": f"Bearer {token.get_secret_value()}"}
        started = time.perf_counter()
        try:
            response = self._http.request(
                method, f"{self._prefix}{path}", headers=headers, params=params, json=json
            )
        except httpx.TimeoutException:
            logger.warning("admin %s %s timed out", method, _route(path))
            raise AdminApiError(AdminErrorKind.TIMEOUT) from None
        except httpx.HTTPError as exc:
            logger.warning("admin %s %s failed: %s", method, _route(path), type(exc).__name__)
            raise AdminApiError(AdminErrorKind.UNREACHABLE) from None
        logger.info(
            "admin %s %s -> %d in %.0f ms request_id=%s",
            method,
            _route(path),
            response.status_code,
            (time.perf_counter() - started) * 1000,
            response.headers.get("X-Request-ID", "-"),
        )
        if response.status_code >= 400 or response.status_code < 200:
            raise admin_error_from_response(response)
        try:
            return model.model_validate(response.json())
        except (ValueError, ValidationError):
            raise AdminApiError(
                AdminErrorKind.INVALID_RESPONSE,
                status_code=response.status_code,
                request_id=response.headers.get("X-Request-ID"),
            ) from None


def _route(path: str) -> str:
    """The path with IDs replaced, for logs (``/admin/faqs/{id}``)."""
    parts = path.split("/")
    return "/".join("{id}" if part.startswith("faq_") or _is_uuid(part) else part for part in parts)


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def parse_keywords(text: str) -> list[str]:
    """Comma- or newline-separated tags as a list (the backend normalises them)."""
    return [part.strip() for part in text.replace("\n", ",").split(",") if part.strip()]


def staff_token(value: str) -> SecretStr | None:
    """A pasted staff token, trimmed; None if empty."""
    value = value.strip()
    return SecretStr(value) if value else None
