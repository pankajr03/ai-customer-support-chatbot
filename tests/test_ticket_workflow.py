"""Ticket status workflow and staff authentication (Stage 10, Task 6)."""

import itertools
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.staff_auth import (
    StaffAuthenticationError,
    StaffAuthenticator,
    generate_staff_token,
    hash_staff_token,
)
from app.database.connection import Database
from app.models.support import (
    ALLOWED_TRANSITIONS,
    CUSTOMER_TRANSITIONS,
    REOPEN_TRANSITIONS,
    ActorType,
    TicketStatus,
)
from app.repositories.conversation_repository import ConversationRepository
from app.repositories.ticket_repository import (
    StaleTicketStatusError,
    TicketNotFoundError,
    TicketRepository,
)
from app.services.conversation_service import (
    ConversationExpiredError,
    ConversationNotFoundError,
    ConversationService,
)
from app.services.ticket_workflow import (
    ActorNotPermittedError,
    TicketWorkflow,
    TransitionNotAllowedError,
    UnknownAssigneeError,
    allowed_next,
    check_transition,
)
from scripts import create_staff_token
from tests.ticket_test_data import form

S = TicketStatus
NOW = datetime(2026, 10, 16, 9, 30, tzinfo=UTC)
ALL_CHANGES = list(itertools.product(TicketStatus, TicketStatus))


# --- The rules ------------------------------------------------------------------------


def test_the_documented_transitions() -> None:
    allowed = {(a, b) for a, targets in ALLOWED_TRANSITIONS.items() for b in targets}

    assert set(ALLOWED_TRANSITIONS) == set(TicketStatus)
    assert {
        (S.OPEN, S.ASSIGNED),
        (S.ASSIGNED, S.IN_PROGRESS),
        (S.IN_PROGRESS, S.WAITING_FOR_CUSTOMER),
        (S.IN_PROGRESS, S.RESOLVED),
        (S.WAITING_FOR_CUSTOMER, S.IN_PROGRESS),
        (S.RESOLVED, S.CLOSED),
    } <= allowed
    assert ALLOWED_TRANSITIONS[S.CLOSED] == frozenset()  # closed is final
    assert REOPEN_TRANSITIONS == {(S.RESOLVED, S.IN_PROGRESS)}  # the only way back
    assert REOPEN_TRANSITIONS <= allowed
    assert not any(current is new for current, new in allowed)


@pytest.mark.parametrize(("current", "requested"), ALL_CHANGES)
def test_staff_may_make_exactly_the_allowed_changes(
    current: TicketStatus, requested: TicketStatus
) -> None:
    if requested in ALLOWED_TRANSITIONS[current]:
        check_transition(current, requested, ActorType.STAFF)
    else:
        with pytest.raises(TransitionNotAllowedError):
            check_transition(current, requested, ActorType.STAFF)


@pytest.mark.parametrize(("current", "requested"), ALL_CHANGES)
def test_customers_may_only_withdraw_or_answer(
    current: TicketStatus, requested: TicketStatus
) -> None:
    if (current, requested) in CUSTOMER_TRANSITIONS:
        check_transition(current, requested, ActorType.CUSTOMER)
    else:
        with pytest.raises((ActorNotPermittedError, TransitionNotAllowedError)):
            check_transition(current, requested, ActorType.CUSTOMER)


@pytest.mark.parametrize("current", list(TicketStatus))
def test_customers_can_never_assign_or_resolve(current: TicketStatus) -> None:
    for requested in (S.ASSIGNED, S.RESOLVED):
        with pytest.raises((ActorNotPermittedError, TransitionNotAllowedError)):
            check_transition(current, requested, ActorType.CUSTOMER)


@pytest.mark.parametrize(("current", "requested"), ALL_CHANGES)
def test_the_system_never_changes_a_status(current: TicketStatus, requested: TicketStatus) -> None:
    # So nothing, a chatbot answer included, can resolve a ticket automatically.
    with pytest.raises((ActorNotPermittedError, TransitionNotAllowedError)):
        check_transition(current, requested, ActorType.SYSTEM)


def test_allowed_next_lists_the_buttons_an_actor_may_see() -> None:
    assert allowed_next(S.OPEN, ActorType.STAFF) == {S.ASSIGNED, S.CLOSED}
    assert allowed_next(S.OPEN, ActorType.CUSTOMER) == {S.CLOSED}
    assert allowed_next(S.WAITING_FOR_CUSTOMER, ActorType.CUSTOMER) == {S.IN_PROGRESS}
    assert allowed_next(S.IN_PROGRESS, ActorType.CUSTOMER) == frozenset()
    assert allowed_next(S.RESOLVED, ActorType.SYSTEM) == frozenset()


# --- Staff authentication -------------------------------------------------------------


def staff_entry(staff_id: str, token: str) -> str:
    return f"{staff_id}:{hash_staff_token(token)}"


def test_a_configured_token_identifies_its_staff_member() -> None:
    maria, sam = generate_staff_token(), generate_staff_token()
    staff = StaffAuthenticator([staff_entry("maria", maria), staff_entry("sam", sam)])

    assert staff.authenticate(maria).staff_id == "maria"
    assert staff.authenticate(sam).staff_id == "sam"
    assert staff.staff_ids == {"maria", "sam"}


@pytest.mark.parametrize(
    "token", [None, "", "stf_short", "cst_" + "a" * 43, "stf_" + "a" * 43, "stf_" + "a" * 44]
)
def test_unknown_or_malformed_tokens_are_refused(token: str | None) -> None:
    staff = StaffAuthenticator([staff_entry("maria", generate_staff_token())])

    with pytest.raises(StaffAuthenticationError):
        staff.authenticate(token)


def test_without_configured_staff_nobody_is_staff() -> None:
    with pytest.raises(StaffAuthenticationError):
        StaffAuthenticator().authenticate(generate_staff_token())


def test_tokens_are_random_and_only_their_hash_is_configured() -> None:
    tokens = {generate_staff_token() for _ in range(100)}

    assert len(tokens) == 100
    assert all(t.startswith("stf_") and len(t) == 47 for t in tokens)
    assert all(len(hash_staff_token(t)) == 64 for t in tokens)


def test_settings_accept_valid_staff_entries(make_settings: Callable[..., Settings]) -> None:
    entries = [staff_entry("maria", generate_staff_token()), staff_entry("agent-07", "x")]

    settings = make_settings(staff_token_hashes=entries)

    assert StaffAuthenticator.from_settings(settings).staff_ids == {"maria", "agent-07"}
    assert settings.safe_summary()["staff_accounts"] == 2
    assert entries[0] not in str(settings.safe_summary())


@pytest.mark.parametrize(
    "entries",
    [
        ["maria"],
        ["maria:" + "A" * 64],  # uppercase hex
        ["maria:" + "a" * 63],
        ["bad id:" + "a" * 64],
        ["maria:" + "a" * 64, "maria:" + "b" * 64],  # the same staff ID twice
        ["stf_" + "a" * 43],  # a token where a hash belongs
    ],
)
def test_settings_refuse_invalid_staff_entries(
    make_settings: Callable[..., Settings], entries: list[str]
) -> None:
    with pytest.raises(ValidationError) as caught:
        make_settings(staff_token_hashes=entries)

    for entry in entries:  # never echoed back
        assert entry not in str(caught.value)


def test_the_token_script_prints_a_token_and_its_entry(capsys: pytest.CaptureFixture[str]) -> None:
    assert create_staff_token.main(["maria"]) == 0

    lines = [line.strip() for line in capsys.readouterr().out.splitlines()]
    token = next(line for line in lines if line.startswith("stf_"))
    entry = next(line for line in lines if line.startswith('"maria:')).strip('"')
    assert StaffAuthenticator([entry]).authenticate(token).staff_id == "maria"


def test_the_token_script_refuses_invalid_staff_ids() -> None:
    with pytest.raises(SystemExit):
        create_staff_token.main(["not valid!"])


# --- Applying changes -----------------------------------------------------------------


@dataclass
class Setup:
    workflow: TicketWorkflow
    tickets: TicketRepository
    conversations: ConversationService
    session_id: uuid.UUID
    token: str
    maria: str  # a staff token


@pytest.fixture
def setup() -> Iterator[Setup]:
    database = Database("sqlite://")
    database.init_schema()
    conversations = ConversationService(ConversationRepository(database), timedelta(hours=1))
    created = conversations.create_conversation(now=NOW)
    tickets = TicketRepository(database)
    maria = generate_staff_token()
    staff = StaffAuthenticator(
        [staff_entry("maria", maria), staff_entry("sam", generate_staff_token())]
    )
    yield Setup(
        TicketWorkflow(tickets, staff, conversations),
        tickets,
        conversations,
        created.session.session_id,
        created.access_token,
        maria,
    )
    database.dispose()


def new_ticket(setup: Setup) -> uuid.UUID:
    return setup.tickets.create_ticket(
        form(), session_id=setup.session_id, now=NOW
    ).ticket.ticket_id


def test_staff_take_a_ticket_through_the_workflow(setup: Setup) -> None:
    ticket_id = new_ticket(setup)
    flow = setup.workflow

    flow.change_status_as_staff(setup.maria, ticket_id, S.ASSIGNED, assigned_to="sam", now=NOW)
    flow.change_status_as_staff(setup.maria, ticket_id, S.IN_PROGRESS, now=NOW)
    flow.change_status_as_staff(setup.maria, ticket_id, S.RESOLVED, now=NOW)
    closed = flow.change_status_as_staff(setup.maria, ticket_id, S.CLOSED, now=NOW)

    assert (closed.status, closed.assigned_to) == (S.CLOSED, "sam")
    staff_events = setup.tickets.list_events(ticket_id)[1:]
    assert {(e.actor_type, e.actor_id) for e in staff_events} == {(ActorType.STAFF, "maria")}


def test_staff_operations_need_a_staff_token(setup: Setup) -> None:
    ticket_id = new_ticket(setup)

    for token in (None, setup.token, generate_staff_token()):  # a customer token is not one
        with pytest.raises(StaffAuthenticationError):
            setup.workflow.change_status_as_staff(token, ticket_id, S.CLOSED, now=NOW)
    assert setup.tickets.get_ticket(ticket_id).status is S.OPEN


def test_tickets_are_only_assigned_to_configured_staff(setup: Setup) -> None:
    ticket_id = new_ticket(setup)

    with pytest.raises(UnknownAssigneeError):
        setup.workflow.change_status_as_staff(
            setup.maria, ticket_id, S.ASSIGNED, assigned_to="nobody", now=NOW
        )
    with pytest.raises(ValueError, match="assigned_to"):
        setup.workflow.change_status_as_staff(setup.maria, ticket_id, S.ASSIGNED, now=NOW)


def test_staff_cannot_skip_steps_or_reopen_a_closed_ticket(setup: Setup) -> None:
    ticket_id = new_ticket(setup)

    with pytest.raises(TransitionNotAllowedError):
        setup.workflow.change_status_as_staff(setup.maria, ticket_id, S.RESOLVED, now=NOW)
    setup.workflow.change_status_as_staff(setup.maria, ticket_id, S.CLOSED, now=NOW)
    with pytest.raises(TransitionNotAllowedError):
        setup.workflow.change_status_as_staff(setup.maria, ticket_id, S.IN_PROGRESS, now=NOW)


def test_a_customer_can_withdraw_an_open_request(setup: Setup) -> None:
    ticket_id = new_ticket(setup)
    reference = setup.tickets.get_ticket(ticket_id).reference_number

    view = setup.workflow.change_status_as_customer(
        setup.session_id, setup.token, reference, S.CLOSED, now=NOW
    )

    assert view.status is S.CLOSED
    assert not {"assigned_to", "description", "contact_id"} & set(view.model_dump())
    event = setup.tickets.list_events(ticket_id)[-1]
    assert (event.actor_type, event.actor_id) == (ActorType.CUSTOMER, None)


def test_a_customer_answering_staff_hands_the_ticket_back(setup: Setup) -> None:
    ticket_id = new_ticket(setup)
    reference = setup.tickets.get_ticket(ticket_id).reference_number
    for status, extra in ((S.ASSIGNED, {"assigned_to": "maria"}), (S.IN_PROGRESS, {}),
                          (S.WAITING_FOR_CUSTOMER, {})):  # fmt: skip
        setup.workflow.change_status_as_staff(setup.maria, ticket_id, status, now=NOW, **extra)

    view = setup.workflow.change_status_as_customer(
        setup.session_id, setup.token, reference, S.IN_PROGRESS, now=NOW
    )

    assert view.status is S.IN_PROGRESS


@pytest.mark.parametrize("requested", [S.ASSIGNED, S.RESOLVED, S.IN_PROGRESS])
def test_customers_cannot_assign_resolve_or_start_work(
    setup: Setup, requested: TicketStatus
) -> None:
    ticket_id = new_ticket(setup)
    reference = setup.tickets.get_ticket(ticket_id).reference_number

    with pytest.raises((ActorNotPermittedError, TransitionNotAllowedError)):
        setup.workflow.change_status_as_customer(
            setup.session_id, setup.token, reference, requested, now=NOW
        )
    assert setup.tickets.get_ticket(ticket_id).status is S.OPEN


def test_customers_only_reach_their_own_conversations_tickets(setup: Setup) -> None:
    ticket_id = new_ticket(setup)
    reference = setup.tickets.get_ticket(ticket_id).reference_number
    other = setup.conversations.create_conversation(now=NOW)

    with pytest.raises(ConversationNotFoundError):  # wrong token for this conversation
        setup.workflow.change_status_as_customer(
            setup.session_id, other.access_token, reference, S.CLOSED, now=NOW
        )
    with pytest.raises(TicketNotFoundError):  # their own conversation, not their ticket
        setup.workflow.change_status_as_customer(
            other.session.session_id, other.access_token, reference, S.CLOSED, now=NOW
        )
    assert setup.tickets.get_ticket(ticket_id).status is S.OPEN


def test_an_expired_conversation_cannot_change_its_tickets(setup: Setup) -> None:
    ticket_id = new_ticket(setup)
    reference = setup.tickets.get_ticket(ticket_id).reference_number

    with pytest.raises(ConversationExpiredError):
        setup.workflow.change_status_as_customer(
            setup.session_id, setup.token, reference, S.CLOSED, now=NOW + timedelta(hours=2)
        )


def test_ticket_and_conversation_statuses_are_independent(setup: Setup) -> None:
    ticket_id = new_ticket(setup)

    setup.conversations.close_conversation(setup.session_id, setup.token, now=NOW)
    assert setup.tickets.get_ticket(ticket_id).status is S.OPEN

    setup.workflow.change_status_as_staff(
        setup.maria, ticket_id, S.ASSIGNED, assigned_to="maria", now=NOW
    )
    session = setup.conversations.get_conversation(setup.session_id, setup.token, now=NOW)
    assert session.status.value == "closed"  # unchanged by the ticket


def test_a_decision_on_an_outdated_status_is_refused(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket_id = new_ticket(setup)
    stale = setup.tickets.get_ticket(ticket_id)
    setup.workflow.change_status_as_staff(setup.maria, ticket_id, S.CLOSED, now=NOW)
    # Another staff member closed it between this read and the change.
    monkeypatch.setattr(setup.tickets, "get_ticket", lambda _id: stale)

    with pytest.raises(StaleTicketStatusError):
        setup.workflow.change_status_as_staff(
            setup.maria, ticket_id, S.ASSIGNED, assigned_to="sam", now=NOW
        )
