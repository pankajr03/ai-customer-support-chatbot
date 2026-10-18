"""Staff admin endpoints (Stage 11), used by the admin dashboard (``frontend/admin_app.py``).

    GET   /admin/me                        any staff     who am I, what may I do
    GET   /admin/staff                     ticket staff  who tickets can be assigned to
    GET   /admin/dashboard/summary         admin         FAQ, ticket, conversation counts
    GET   /admin/analytics/chat            admin         chatbot decisions, rates, latency
    GET   /admin/analytics/tickets         admin         ticket volumes, resolution, backlog
    GET   /admin/audit                     admin         the administrative audit log
    (/admin/overview and /admin/analytics remain as aliases of the first two.)

Analytics take either ``days`` (the last N UTC days, today included) or
``start_date``/``end_date`` (inclusive UTC days); at most 366 days, never after today.
    GET   /admin/faqs                      admin         list / search / filter FAQs
    POST  /admin/faqs                      admin         create an FAQ
    GET   /admin/faqs/{faq_id}             admin         one FAQ with its version
    PATCH /admin/faqs/{faq_id}             admin         edit (needs expected_version)
    POST  /admin/faqs/{faq_id}/deactivate  admin         hide from customers (kept)
    POST  /admin/faqs/{faq_id}/activate    admin         show to customers again
    GET   /admin/faqs/{faq_id}/revisions   admin         change history, newest first

Tickets stay on ``/support/tickets`` (Stage 10), open to admins and support staff.

Every endpoint requires a staff token (``Authorization: Bearer stf_...``), checked
before anything is read: ``401 STAFF_AUTH_REQUIRED`` without a valid one, ``403
STAFF_ACCESS_DISABLED`` while no staff are configured, ``403 STAFF_FORBIDDEN`` when the
role does not allow the operation, ``400`` for a token in the URL. Conversation tokens
are never accepted. The services check the role again. Responses carry
``Cache-Control: no-store``.

Handlers only translate HTTP to the services; validation lives in the request models and
rules in ``FAQManagementService``.
"""

from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Request, Response, status

from app.api.dependencies import (
    AdminDashboardServiceDep,
    AnalyticsServiceDep,
    AnalyticsStaffDep,
    AuditRepositoryDep,
    AuditStaffDep,
    FAQAdminDep,
    FAQManagementServiceDep,
    StaffMemberDep,
    TicketStaffDep,
    get_staff_authenticator,
)
from app.api.errors import (
    FAQ_LAST_ACTIVE_MESSAGE,
    FAQ_NOT_FOUND_MESSAGE,
    FAQ_STORE_UNAVAILABLE_MESSAGE,
    FAQ_VERSION_CONFLICT_MESSAGE,
    STAFF_AUTH_MESSAGE,
    STAFF_DISABLED_MESSAGE,
    STAFF_FORBIDDEN_MESSAGE,
)
from app.api.routes.conversations import VALIDATION, _error
from app.models.admin_api import (
    AdminOverview,
    ChatAnalytics,
    FAQAdminList,
    StaffDirectory,
    StaffDirectoryEntry,
    StaffIdentity,
    TicketAnalytics,
)
from app.models.audit import AuditAction, AuditEventPage, AuditOutcome, AuditResource
from app.models.errors import ErrorCode
from app.models.faq import Category
from app.models.faq_admin import (
    MAX_SEARCH_LENGTH,
    FAQActivation,
    FAQChanges,
    FAQDraft,
    FAQRevisionPage,
    ManagedFAQ,
)
from app.repositories.audit_repository import MAX_PAGE_SIZE as AUDIT_MAX_PAGE
from app.repositories.audit_repository import AuditFilter
from app.repositories.faq_repository import MAX_PAGE_SIZE
from app.services.analytics_service import DEFAULT_DAYS as DEFAULT_ANALYTICS_DAYS
from app.services.analytics_service import MAX_DAYS as MAX_ANALYTICS_DAYS
from app.services.analytics_service import AnalyticsPeriod
from app.services.faq_management_service import DEFAULT_PAGE_SIZE


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(_no_store)])

FAQIdPath = Annotated[str, Path(max_length=64, examples=["faq_001"])]

STAFF_ERRORS: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: _error(
        "No valid staff token.", ("staff", ErrorCode.STAFF_AUTH_REQUIRED, STAFF_AUTH_MESSAGE)
    ),
    status.HTTP_403_FORBIDDEN: _error(
        "No staff configured on this server, or the staff role does not allow this.",
        ("disabled", ErrorCode.STAFF_ACCESS_DISABLED, STAFF_DISABLED_MESSAGE),
        ("forbidden", ErrorCode.STAFF_FORBIDDEN, STAFF_FORBIDDEN_MESSAGE),
    ),
    **VALIDATION,
}
FAQ_ERRORS: dict[int | str, dict[str, Any]] = {
    **STAFF_ERRORS,
    status.HTTP_503_SERVICE_UNAVAILABLE: _error(
        "FAQ storage is unavailable, or FAQs are not editable on this server.",
        ("unavailable", ErrorCode.SERVICE_UNAVAILABLE, FAQ_STORE_UNAVAILABLE_MESSAGE),
    ),
}
FAQ_MISSING = {
    status.HTTP_404_NOT_FOUND: _error(
        "Unknown FAQ.", ("faq", ErrorCode.FAQ_NOT_FOUND, FAQ_NOT_FOUND_MESSAGE)
    )
}
FAQ_CONFLICTS = {
    status.HTTP_409_CONFLICT: _error(
        "The FAQ changed since it was read (reload and retry), or this would leave no active FAQ.",
        ("version", ErrorCode.FAQ_VERSION_CONFLICT, FAQ_VERSION_CONFLICT_MESSAGE),
        ("last_active", ErrorCode.FAQ_LAST_ACTIVE, FAQ_LAST_ACTIVE_MESSAGE),
    )
}


@router.get("/me", response_model=StaffIdentity, summary="The signed-in staff member")
def who_am_i(staff: StaffMemberDep) -> StaffIdentity:
    """The staff ID, role and permissions behind the presented token. The dashboard
    calls this to sign in; any configured staff member may."""
    return StaffIdentity.of(staff)


@router.get(
    "/staff",
    response_model=StaffDirectory,
    summary="Staff who can be assigned tickets",
    responses=STAFF_ERRORS,
)
def staff_directory(_staff: TicketStaffDep, request: Request) -> StaffDirectory:
    """Configured staff IDs whose role handles tickets, with their roles: the only valid
    assignees. Never token hashes."""
    members = get_staff_authenticator(request).assignable()
    return StaffDirectory(
        staff=[StaffDirectoryEntry(staff_id=m.staff_id, role=m.role) for m in members]
    )


def analytics_period(
    days: Annotated[
        int | None,
        Query(ge=1, le=MAX_ANALYTICS_DAYS, description="The last N UTC days, today included."),
    ] = None,
    start_date: Annotated[
        date | None, Query(description="First UTC day (inclusive), e.g. 2026-10-01.")
    ] = None,
    end_date: Annotated[
        date | None, Query(description="Last UTC day (inclusive); default today.")
    ] = None,
) -> AnalyticsPeriod:
    """A bounded analytics period (default: the last 30 days). Raises InvalidPeriodError
    (422) for both forms at once, a reversed or over-long range, or a future end."""
    if days is None and start_date is None and end_date is None:
        days = DEFAULT_ANALYTICS_DAYS
    return AnalyticsPeriod.resolve(days=days, start_date=start_date, end_date=end_date)


PeriodDep = Annotated[AnalyticsPeriod, Depends(analytics_period)]


@router.get(
    "/dashboard/summary",
    response_model=AdminOverview,
    summary="Dashboard counts (admin)",
    responses=FAQ_ERRORS,
)
@router.get("/overview", response_model=AdminOverview, include_in_schema=False)
def overview(staff: AnalyticsStaffDep, service: AdminDashboardServiceDep) -> AdminOverview:
    """Counts only: FAQs by category and status, tickets by status, type and lead
    category. No ticket text or contact details."""
    return service.overview(staff)


@router.get(
    "/analytics/chat",
    response_model=ChatAnalytics,
    summary="Chatbot analytics (admin)",
    responses=FAQ_ERRORS,
)
@router.get("/analytics", response_model=ChatAnalytics, include_in_schema=False)
def analytics(
    staff: AnalyticsStaffDep, service: AnalyticsServiceDep, period: PeriodDep
) -> ChatAnalytics:
    """Recorded chatbot decisions, escalation offers, ticket requests and server
    processing times, with each rate's formula and inputs. Rates on fewer than
    `min_sample` responses are null. `answered` is a chatbot decision, not a resolved
    customer issue; see `limitations`."""
    return service.chat_analytics(staff, period=period)


@router.get(
    "/analytics/tickets",
    response_model=TicketAnalytics,
    summary="Ticket analytics (admin)",
    responses=FAQ_ERRORS,
)
def ticket_analytics(
    staff: AnalyticsStaffDep, service: AnalyticsServiceDep, period: PeriodDep
) -> TicketAnalytics:
    """Tickets created in the period (by type, current status, priority, lead category),
    resolved and closed in the period with time to resolution, and the current backlog.
    Counts and durations only: no subjects, descriptions or contact details."""
    return service.ticket_analytics(staff, period)


@router.get(
    "/audit",
    response_model=AuditEventPage,
    summary="Administrative audit log (admin)",
    responses=STAFF_ERRORS,
)
def audit_log(
    _staff: AuditStaffDep,
    audit: AuditRepositoryDep,
    action: AuditAction | None = None,
    actor_id: Annotated[str | None, Query(max_length=64)] = None,
    resource_type: AuditResource | None = None,
    resource_id: Annotated[str | None, Query(max_length=200)] = None,
    outcome: AuditOutcome | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=AUDIT_MAX_PAGE)] = 50,
) -> AuditEventPage:
    """Recorded administrative actions and refused staff access, newest first. Read
    only: there is no way to change or delete an event."""
    since = datetime.combine(start_date, time.min, UTC) if start_date else None
    until = datetime.combine(end_date, time.min, UTC) + timedelta(days=1) if end_date else None
    return audit.list_events(
        AuditFilter(
            action=action,
            actor_id=actor_id,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome=outcome,
            created_from=since,
            created_before=until,
        ),
        offset=offset,
        limit=limit,
    )


@router.get("/faqs", response_model=FAQAdminList, summary="List FAQs (admin)", responses=FAQ_ERRORS)
def list_faqs(
    staff: FAQAdminDep,
    service: FAQManagementServiceDep,
    q: Annotated[
        str | None,
        Query(
            max_length=MAX_SEARCH_LENGTH,
            description="Text in the question, answer or keywords, or an exact FAQ ID.",
        ),
    ] = None,
    category: Category | None = None,
    is_active: Annotated[
        bool | None, Query(description="true: active only; false: inactive only.")
    ] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> FAQAdminList:
    """Active and inactive FAQs in ID order, with versions and who changed them."""
    page = service.list_faqs(
        staff, search=q, category=category, is_active=is_active, offset=offset, limit=limit
    )
    return FAQAdminList.of(page)


@router.post(
    "/faqs",
    status_code=status.HTTP_201_CREATED,
    response_model=ManagedFAQ,
    summary="Create an FAQ (admin)",
    responses=FAQ_ERRORS,
)
def create_faq(
    body: FAQDraft,
    staff: FAQAdminDep,
    service: FAQManagementServiceDep,
    request: Request,
    response: Response,
) -> ManagedFAQ:
    """A new FAQ under the next free ID (version 1). Active FAQs are used for customer
    answers right away."""
    created = service.create_faq(staff, body)
    response.headers["Location"] = f"{request.url.path}/{created.id}"
    return created


@router.get(
    "/faqs/{faq_id}",
    response_model=ManagedFAQ,
    summary="Get an FAQ (admin)",
    responses={**FAQ_ERRORS, **FAQ_MISSING},
)
def get_faq(faq_id: FAQIdPath, staff: FAQAdminDep, service: FAQManagementServiceDep) -> ManagedFAQ:
    """One FAQ, active or not, with its current `version` (send it back when editing)."""
    return service.get_faq(staff, faq_id)


@router.patch(
    "/faqs/{faq_id}",
    response_model=ManagedFAQ,
    summary="Edit an FAQ (admin)",
    responses={**FAQ_ERRORS, **FAQ_MISSING, **FAQ_CONFLICTS},
)
def update_faq(
    faq_id: FAQIdPath, body: FAQChanges, staff: FAQAdminDep, service: FAQManagementServiceDep
) -> ManagedFAQ:
    """Change only the given fields. `expected_version` must be the FAQ's current
    version: if someone changed it meanwhile the edit is refused (`409
    FAQ_VERSION_CONFLICT`) rather than overwriting their change."""
    return service.update_faq(staff, faq_id, body)


@router.post(
    "/faqs/{faq_id}/deactivate",
    response_model=ManagedFAQ,
    summary="Deactivate an FAQ (admin)",
    responses={**FAQ_ERRORS, **FAQ_MISSING, **FAQ_CONFLICTS},
)
def deactivate_faq(
    faq_id: FAQIdPath, body: FAQActivation, staff: FAQAdminDep, service: FAQManagementServiceDep
) -> ManagedFAQ:
    """Stop using the FAQ for customer answers and search. It stays stored (and can be
    activated again); FAQs are never deleted, so past replies that cite it keep their
    meaning."""
    return service.deactivate_faq(staff, faq_id, body.expected_version)


@router.post(
    "/faqs/{faq_id}/activate",
    response_model=ManagedFAQ,
    summary="Activate an FAQ (admin)",
    responses={**FAQ_ERRORS, **FAQ_MISSING, **FAQ_CONFLICTS},
)
def activate_faq(
    faq_id: FAQIdPath, body: FAQActivation, staff: FAQAdminDep, service: FAQManagementServiceDep
) -> ManagedFAQ:
    """Use a deactivated FAQ for customer answers again."""
    return service.activate_faq(staff, faq_id, body.expected_version)


@router.get(
    "/faqs/{faq_id}/revisions",
    response_model=FAQRevisionPage,
    summary="An FAQ's change history (admin)",
    responses={**FAQ_ERRORS, **FAQ_MISSING},
)
def list_revisions(
    faq_id: FAQIdPath,
    staff: FAQAdminDep,
    service: FAQManagementServiceDep,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> FAQRevisionPage:
    """Every recorded change, newest first: the action (`created`, `updated`,
    `activated`, `deactivated`), the changed fields before and after, who and when.
    Staff only; never part of customer responses."""
    return service.list_revisions(staff, faq_id, offset=offset, limit=limit)
