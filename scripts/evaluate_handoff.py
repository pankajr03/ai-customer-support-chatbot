"""Run the human handoff and lead capture journeys (data/handoff_eval.json).

Usage (from the project root, virtual environment active):

    python scripts/evaluate_handoff.py                    # all scenarios
    python scripts/evaluate_handoff.py --offline          # never download the model
    python scripts/evaluate_handoff.py --only HO-01 HO-12 --output handoff.json

Each scenario runs the way the Streamlit page runs it: the frontend's ``ChatState``,
``SupportState`` and ``frontend.actions`` with its ``ChatApiClient``, against a fresh copy
of the real backend (FastAPI in-process, a temporary SQLite file, the real FAQ file and
the real embedding model, loaded once and shared). Staff steps call the staff API with a
staff token made for the run. No OpenAI request is ever sent (the LLM is off). Faults
are injected where a step says so: a lost reply after the ticket was stored
(``timeout``) or a failing ticket store (``database``).

Every step records checks under one metric, and the summary counts them:

- escalation_offer: the reply's offer (or none) as the rules require, declining works,
  and the offer that led to a ticket is recorded on it;
- ticket_creation: confirmed requests are created (one ticket each);
- duplicate_prevention: repeats return the original ticket, conflicting IDs are refused;
- authorization: other conversations and non-staff callers are refused;
- invalid_transition: workflow changes that are not allowed are refused;
- status_workflow: allowed staff changes apply;
- contact_validation: invalid details are caught before anything is sent;
- confirmation_accuracy: the confirmation and status shown are true (reference,
  status, no claims of notification, assignment or response times);
- failure_handling: failures leave nothing partial and say what to do.

The numbers come from this run only. Exit status 1 if any check fails.
"""

import argparse
import json
import os
import sys
import tempfile
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Literal

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # allow `python scripts/evaluate_handoff.py`
    sys.path.insert(0, str(PROJECT_ROOT))

from pydantic import BaseModel, ConfigDict, Field  # noqa: E402

DATASET = PROJECT_ROOT / "data" / "handoff_eval.json"
MIN_SCENARIOS = 20
REQUIRED_CATEGORIES = frozenset(
    {
        "human_request",
        "declined_escalation",
        "cannot_answer",
        "ambiguous",
        "refund_assistance",
        "shipping_complaint",
        "sales_inquiry",
        "duplicate_submission",
        "invalid_contact",
        "missing_consent",
        "unauthorized_access",
        "status_change",
        "conversation_expired",
        "database_failure",
        "network_timeout",
    }
)
METRICS = (
    "escalation_offer",
    "ticket_creation",
    "duplicate_prevention",
    "authorization",
    "invalid_transition",
    "status_workflow",
    "contact_validation",
    "confirmation_accuracy",
    "failure_handling",
)
Decision = Literal["answered", "ambiguous", "no_match", "retrieval_unavailable"]
# Never in a confirmation: the page must not claim what did not happen.
FORBIDDEN_CLAIMS = ("notified", "has joined", "agent will", "within", "assigned to")
STAFF_ID = "eval"


# --- The dataset ----------------------------------------------------------------------


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: Literal[
        "chat",
        "decline",
        "open_form",
        "submit",
        "retry",
        "api_conflict",
        "check_status",
        "staff_change",
        "staff_list",
        "staff_detail",
        "other_session",
        "expire",
        "delete_conversation",
        "expect_tickets",
    ]
    message: str | None = None
    decision: list[Decision] | None = None
    offer: str | None | dict[str, str | None] = Field(default="__unset__")
    ticket_type: Literal["support", "sales"] | None = None
    via: Literal["offer", "sidebar"] | None = None
    form: dict[str, Any] = {}
    expect: Literal["created", "replayed", "duplicate", "invalid", "error"] | None = None
    invalid_fields: list[str] = []
    error: str | None = None
    fault: Literal["timeout", "database"] | None = None
    status: str | None = None
    assigned_to: str | None = None
    token: Literal["staff", "customer", "none", "invalid"] = "staff"  # noqa: S105 - a kind
    expect_http: int | None = None
    escalation_reason: str | None = Field(default="__unset__")
    has_conversation: bool | None = None
    count: int | None = None


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^HO-\d{2}$")
    category: str
    description: str
    steps: list[Step] = Field(min_length=1)


class Dataset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    description: str
    version: int
    scenarios: list[Scenario]


def load_dataset(path: Path = DATASET) -> Dataset:
    return Dataset.model_validate_json(path.read_text(encoding="utf-8"))


def validate_dataset(dataset: Dataset) -> list[str]:
    problems = []
    ids = [s.id for s in dataset.scenarios]
    if len(ids) != len(set(ids)):
        problems.append("scenario IDs repeat")
    if len(ids) < MIN_SCENARIOS:
        problems.append(f"at least {MIN_SCENARIOS} scenarios are needed, found {len(ids)}")
    missing = REQUIRED_CATEGORIES - {s.category for s in dataset.scenarios}
    if missing:
        problems.append(f"missing categories: {sorted(missing)}")
    for scenario in dataset.scenarios:
        for index, step in enumerate(scenario.steps, start=1):
            where = f"{scenario.id} step {index}"
            if step.action == "chat" and (not step.message or step.offer == "__unset__"):
                problems.append(f"{where}: chat needs a message and an expected offer")
            if step.action == "submit" and step.expect is None:
                problems.append(f"{where}: submit needs expect")
            if step.action == "open_form" and (step.ticket_type is None or step.via is None):
                problems.append(f"{where}: open_form needs ticket_type and via")
            if step.action in ("staff_change", "staff_list") and step.expect_http is None:
                problems.append(f"{where}: {step.action} needs expect_http")
    return problems


# --- Running ----------------------------------------------------------------------------


class Journey:
    """One scenario's page, backend and the checks it recorded."""

    def __init__(self, http: Any, app: Any, database: Any, staff_token: str) -> None:
        from frontend.api_client import ChatApiClient
        from frontend.state import ChatState
        from frontend.support import SupportState

        self.http, self.app, self.database = http, app, database
        self.staff_token = staff_token
        self.client = ChatApiClient(http_client=http, sleep=lambda seconds: None)
        self.state = ChatState()
        self.support = SupportState()
        self.checks: list[tuple[str, bool, str]] = []
        self.request_ids: list[uuid.UUID] = []
        self.first_ticket_id: uuid.UUID | None = None
        self.last_form: Any = None
        real_create = self.client.create_ticket

        def recording(credentials: Any, body: dict[str, Any], request_id: uuid.UUID) -> Any:
            self.request_ids.append(request_id)
            return real_create(credentials, body, request_id)

        self.client.create_ticket = recording  # type: ignore[method-assign]
        self._real_create = recording

    def check(self, metric: str, ok: bool, detail: str) -> None:
        self.checks.append((metric, ok, detail))

    # -- helpers -----------------------------------------------------------------------

    def tickets(self) -> int:
        from sqlalchemy import text

        with self.database.read() as db:
            return int(db.execute(text("SELECT count(*) FROM support_tickets")).scalar() or 0)

    def newest_stored_ticket(self) -> uuid.UUID | None:
        """The ticket the backend stored last (to recognise a replay of a lost reply)."""
        from sqlalchemy import text

        with self.database.read() as db:
            value = db.execute(
                text("SELECT ticket_id FROM support_tickets ORDER BY created_at DESC LIMIT 1")
            ).scalar()
        return uuid.UUID(value) if value else None

    def headers(self, kind: str) -> dict[str, str]:
        if kind == "staff":
            return {"Authorization": f"Bearer {self.staff_token}"}
        if kind == "customer" and self.state.credentials is not None:
            return self.state.credentials.authorization()
        if kind == "invalid":
            return {"Authorization": "Bearer stf_" + "x" * 43}
        return {}

    def latest_ticket_id(self) -> uuid.UUID | None:
        latest = self.support.latest
        return latest.ticket_id if latest is not None else self.first_ticket_id

    def form(self, overrides: dict[str, Any]) -> Any:
        from frontend.support import SupportForm

        values: dict[str, Any] = {
            "ticket_type": self.support.form_type or "support",
            "name": "Evaluation Customer",
            "email": "customer@example.com",
            "phone": "",
            "contact_preference": "email",
            "subject": "Help with my order",
            "description": "I need help from your support team with my order.",
            "consent": True,
            "lead_category": "general_sales" if self.support.form_type == "sales" else None,
        }
        values.update(overrides)
        return SupportForm(**values)

    # -- steps -------------------------------------------------------------------------

    def run_step(self, step: Step) -> None:
        getattr(self, f"_{step.action}")(step)

    def _chat(self, step: Step) -> None:
        from frontend import actions

        if self.state.queue_message(step.message or "", max_chars=4000) is None:
            self.check("escalation_offer", False, f"{step.message!r} was not sent")
            return
        actions.send_pending(self.state, self.client)
        reply = self.state.messages[-1]
        decision = reply.decision.value if reply.decision else None
        if step.decision is not None and decision not in step.decision:
            # A retrieval result outside the scenario's assumption: reported, not hidden.
            self.check("escalation_offer", False, f"decision {decision}, expected {step.decision}")
        offer = reply.escalation_offer.reason if reply.escalation_offer else None
        expected = step.offer.get(decision or "") if isinstance(step.offer, dict) else step.offer
        self.check(
            "escalation_offer",
            offer == expected,
            f"{step.message!r}: decision {decision}, offer {offer}, expected {expected}",
        )

    def _decline(self, step: Step) -> None:
        reply = self.state.messages[-1]
        offer = reply.escalation_offer
        self.support.dismiss(reply.message_id)
        visible = offer is not None and self.support.offer_visible(
            reply.message_id, offer.ticket_type
        )
        self.check(
            "escalation_offer",
            offer is not None and not visible and not self.support.form_open,
            "declining hides the offer and opens nothing",
        )

    def _open_form(self, step: Step) -> None:
        if step.via == "offer":
            reply = self.state.messages[-1] if self.state.messages else None
            offer = reply.escalation_offer if reply else None
            ok = (
                reply is not None
                and offer is not None
                and offer.ticket_type == step.ticket_type
                and self.support.offer_visible(reply.message_id, offer.ticket_type)
            )
            self.check("escalation_offer", ok, f"a visible {step.ticket_type} offer to accept")
        self.support.open_form(step.ticket_type or "support")

    def _submit(self, step: Step, *, retry: bool = False) -> None:
        from frontend import actions
        from frontend.api_client import ErrorKind

        before = self.tickets()
        if retry:
            if not self.support.retry():
                self.check("failure_handling", False, "Try again was not available")
                return
        else:
            self.last_form = self.form(step.form)
            if self.support.queue_submission(self.last_form) is None:
                problems = set(self.support.form_problems)
                ok = step.expect == "invalid" and set(step.invalid_fields) <= problems
                self.check("contact_validation", ok, f"refused on the page: {sorted(problems)}")
                self.check("contact_validation", self.tickets() == before, "nothing was stored")
                return
            if step.expect == "invalid":
                self.check("contact_validation", False, "invalid details were sent")
        restore = self._inject(step.fault)
        try:
            actions.submit_support_request(self.state, self.support, self.client)
        finally:
            restore()
        after = self.tickets()
        error = self.support.last_error
        if step.expect == "error":
            kind = error.kind.value if error else None
            self.check(
                "failure_handling", kind == step.error, f"error {kind}, expected {step.error}"
            )
            stored_anyway = step.fault == "timeout"  # the reply was lost, not the ticket
            if stored_anyway:
                self.first_ticket_id = self.newest_stored_ticket()
            self.check(
                "failure_handling",
                after == before + (1 if stored_anyway else 0) and self.support.latest is None,
                "no reference shown; nothing partial stored",
            )
            self.check(
                "failure_handling",
                self.support.can_retry == (error is not None and error.kind in (
                    ErrorKind.TIMEOUT, ErrorKind.SERVICE_UNAVAILABLE)),
                "Try again offered exactly when it is safe",
            )  # fmt: skip
            return
        latest = self.support.latest
        if latest is None:
            kind = error.kind.value if error else "none"
            self.check("ticket_creation", False, f"no ticket (error {kind})")
            return
        if step.expect == "created":
            self.check("ticket_creation", after == before + 1, "exactly one new ticket")
            self.first_ticket_id = self.first_ticket_id or latest.ticket_id
            self._check_confirmation(latest)
        else:  # replayed or duplicate
            same = latest.ticket_id == self.first_ticket_id
            self.check("duplicate_prevention", same and after == before,
                       f"{step.expect}: the original ticket, nothing new stored")  # fmt: skip
            if step.expect == "duplicate":
                self.check("confirmation_accuracy", latest.already_sent,
                           "says the request was already sent")  # fmt: skip
        if retry and len(set(self.request_ids)) != 1:
            self.check("duplicate_prevention", False, "the retry used a new client_request_id")

    def _retry(self, step: Step) -> None:
        self._submit(step, retry=True)

    def _check_confirmation(self, ticket: Any) -> None:
        prefix = "SAL-" if ticket.ticket_type == "sales" else "SUP-"
        message = ticket.message.lower()
        claims = [claim for claim in FORBIDDEN_CLAIMS if claim in message]
        self.check(
            "confirmation_accuracy",
            ticket.reference_number.startswith(prefix)
            and ticket.reference_number in ticket.message
            and ticket.status == "open"
            and "recorded" in ticket.status_message
            and not claims
            and ticket.expected_response is None,
            f"{ticket.reference_number}: recorded, status open, no claims {claims or ''}",
        )
        if ticket.ticket_type == "sales":
            expected = self.last_form.lead_category if self.last_form else None
            self.check("ticket_creation", ticket.lead_category == expected,
                       f"lead category {ticket.lead_category}")  # fmt: skip

    def _inject(self, fault: str | None) -> Any:
        from frontend.api_client import ApiError, ErrorKind

        if fault == "timeout":

            def lost_reply(credentials: Any, body: dict[str, Any], request_id: uuid.UUID) -> Any:
                self._real_create(credentials, body, request_id)  # stored ...
                raise ApiError(ErrorKind.TIMEOUT)  # ... but the reply never arrives

            self.client.create_ticket = lost_reply  # type: ignore[method-assign]
            return lambda: setattr(self.client, "create_ticket", self._real_create)
        if fault == "database":
            from app.repositories.ticket_repository import TicketStoreError

            repository = self.app.state.ticket_repository
            real = repository.create_ticket

            def failing(*args: Any, **kwargs: Any) -> Any:
                raise TicketStoreError("ticket storage is unavailable (injected)")

            repository.create_ticket = failing
            return lambda: setattr(repository, "create_ticket", real)
        return lambda: None

    def _api_conflict(self, step: Step) -> None:
        body = self.last_form.body() | {"subject": "Something different"}
        body |= {
            "session_id": str(self.state.credentials.session_id),  # type: ignore[union-attr]
            "client_request_id": str(self.request_ids[-1]),
        }
        response = self.http.post(
            "/api/v1/support/tickets", json=body, headers=self.headers("customer")
        )
        code = response.json().get("error", {}).get("code")
        self.check(
            "duplicate_prevention",
            response.status_code == 422 and code == "TICKET_REQUEST_CONFLICT",
            f"same ID, other details -> {response.status_code} {code}",
        )

    def _check_status(self, step: Step) -> None:
        from frontend import actions

        ticket_id = self.latest_ticket_id()
        if ticket_id is None:
            self.check("confirmation_accuracy", False, "no ticket to check")
            return
        actions.check_ticket_status(self.state, self.support, self.client, ticket_id)
        latest = self.support.latest
        status = latest.status if latest else None
        self.check(
            "confirmation_accuracy",
            status == step.status and self.support.status_error is None,
            f"customer sees status {status}, expected {step.status}",
        )

    def _staff_change(self, step: Step) -> None:
        ticket_id = self.latest_ticket_id()
        payload: dict[str, Any] = {"status": step.status}
        if step.assigned_to:
            payload["assigned_to"] = step.assigned_to
        response = self.http.patch(
            f"/api/v1/support/tickets/{ticket_id}/status",
            json=payload,
            headers=self.headers(step.token),
        )
        metric = {409: "invalid_transition", 422: "invalid_transition", 200: "status_workflow"}
        name = metric.get(step.expect_http or 0, "authorization")
        self.check(
            name,
            response.status_code == step.expect_http,
            f"{step.token} -> {step.status}: {response.status_code}, expected {step.expect_http}",
        )

    def _staff_list(self, step: Step) -> None:
        response = self.http.get("/api/v1/support/tickets", headers=self.headers(step.token))
        self.check(
            "authorization",
            response.status_code == step.expect_http,
            f"staff list with {step.token} token: {response.status_code}",
        )

    def _staff_detail(self, step: Step) -> None:
        response = self.http.get(
            f"/api/v1/support/tickets/{self.latest_ticket_id()}", headers=self.headers("staff")
        )
        detail = response.json() if response.status_code == 200 else {}
        if step.escalation_reason != "__unset__":
            self.check(
                "escalation_offer",
                detail.get("escalation_reason") == step.escalation_reason,
                f"offer recorded on the ticket: {detail.get('escalation_reason')}",
            )
        if step.status is not None or step.has_conversation is not None:
            ok = (step.status is None or detail.get("status") == step.status) and (
                step.has_conversation is None
                or detail.get("has_conversation") == step.has_conversation
            )
            self.check("failure_handling", ok, "the ticket survives the conversation's deletion")

    def _other_session(self, step: Step) -> None:
        from frontend.api_client import ApiError

        ticket_id = self.latest_ticket_id()
        other = self.client.create_conversation().credentials()
        try:
            self.client.get_ticket(other, ticket_id)  # type: ignore[arg-type]
            kind = "allowed"
        except ApiError as error:
            kind = error.kind.value
        self.check("authorization", kind == "ticket_not_found", f"own session: {kind}")
        borrowed = self.http.get(
            f"/api/v1/support/tickets/{ticket_id}",
            params={"session_id": str(self.state.credentials.session_id)},  # type: ignore[union-attr]
            headers=other.authorization(),
        )
        self.check("authorization", borrowed.status_code == 404,
                   f"borrowed session ID: {borrowed.status_code}")  # fmt: skip

    def _expire(self, step: Step) -> None:
        from sqlalchemy import text

        with self.database.write() as db:
            db.execute(
                text(
                    "UPDATE conversation_sessions SET created_at = '2000-01-01 00:00:00', "
                    "updated_at = '2000-01-01 00:00:00', expires_at = '2000-01-01 01:00:00'"
                )
            )

    def _delete_conversation(self, step: Step) -> None:
        from frontend import actions

        deleted = actions.delete_conversation(self.state, self.client)
        self.check("failure_handling", deleted, "the conversation was deleted")

    def _expect_tickets(self, step: Step) -> None:
        count = self.tickets()
        self.check(
            "duplicate_prevention" if (step.count or 0) else "ticket_creation",
            count == step.count,
            f"{count} ticket(s) stored, expected {step.count}",
        )


class Runner:
    """A fresh backend per scenario around one shared FAQ service and model."""

    def __init__(self, faq_service: Any, workdir: Path) -> None:
        from app.core.config import Settings
        from app.core.staff_auth import generate_staff_token, hash_staff_token
        from app.services.embedding_service import EmbeddingService
        from app.services.semantic_search_service import SemanticSearchService

        self.staff_token = generate_staff_token()  # for this run only, never stored
        self.settings = Settings(
            _env_file=None,
            query_rewrite_use_llm=False,
            llm_enabled=False,
            staff_token_hashes=[f"{STAFF_ID}:{hash_staff_token(self.staff_token)}"],
        )
        self.faq_service = faq_service
        self.workdir = workdir
        self.semantic = SemanticSearchService(
            faq_service.list_active_faqs,
            EmbeddingService(self.settings.embedding_model_name),
            default_min_score=self.settings.semantic_min_similarity,
        )
        self.count = 0

    def run(self, scenario: Scenario) -> dict[str, Any]:
        from fastapi.testclient import TestClient

        from app.database.connection import Database
        from app.main import create_app
        from frontend import actions

        self.count += 1
        database = Database(f"sqlite:///{(self.workdir / f'{self.count}.db').as_posix()}")
        app = create_app(
            self.settings,
            faq_service=self.faq_service,
            semantic_search_service=self.semantic,
            database=database,
        )
        http = TestClient(app)
        http.__enter__()
        journey = Journey(http, app, database, self.staff_token)
        try:
            actions.start_conversation(journey.state, journey.client)
            journey.support.follow(journey.state.credentials.session_id)  # type: ignore[union-attr]
            for step in scenario.steps:
                journey.run_step(step)
        finally:
            http.__exit__(None, None, None)
            database.dispose()
        failures = [detail for _, ok, detail in journey.checks if not ok]
        return {
            "id": scenario.id,
            "category": scenario.category,
            "passed": not failures,
            "failures": failures,
            "checks": [
                {"metric": metric, "passed": ok, "detail": detail}
                for metric, ok, detail in journey.checks
            ],
        }


def summarise(results: list[dict[str, Any]]) -> str:
    totals: dict[str, Counter[str]] = {metric: Counter() for metric in METRICS}
    for result in results:
        for check in result["checks"]:
            totals[check["metric"]]["passed" if check["passed"] else "failed"] += 1
    lines = [f"{'metric':24} passed/checks"]
    for metric in METRICS:
        counts = totals[metric]
        total = counts["passed"] + counts["failed"]
        rate = f"{100 * counts['passed'] / total:.0f}%" if total else "-"
        lines.append(f"{metric:24} {counts['passed']}/{total} ({rate})")
    passed = sum(r["passed"] for r in results)
    lines.append(f"{'scenarios':24} {passed}/{len(results)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--only", nargs="*", help="scenario IDs to run")
    parser.add_argument("--offline", action="store_true", help="never download the model")
    parser.add_argument("--output", type=Path, help="write every check as JSON")
    args = parser.parse_args(argv)
    if args.offline:
        os.environ["HF_HUB_OFFLINE"] = "1"

    from app.core.config import Settings
    from app.database.faq_repository import FAQRepository
    from app.services.faq_service import FAQService

    dataset = load_dataset(args.dataset)
    problems = validate_dataset(dataset)
    if problems:
        print("The dataset is invalid:\n  " + "\n  ".join(problems))
        return 2
    repository = FAQRepository.from_json_file(Settings(_env_file=None).faq_data_path)
    scenarios = [s for s in dataset.scenarios if not args.only or s.id in args.only]
    with tempfile.TemporaryDirectory() as workdir:
        runner = Runner(FAQService(repository), Path(workdir))
        print("Loading the embedding model and FAQ index (once)...")
        started = time.perf_counter()
        runner.semantic.build_index()
        print(f"Ready in {time.perf_counter() - started:.1f} s.\n")
        results = []
        for scenario in scenarios:
            result = runner.run(scenario)
            results.append(result)
            print(f"{'PASS' if result['passed'] else 'FAIL'} {scenario.id} "
                  f"{scenario.category:22} {scenario.description}")  # fmt: skip
            for failure in result["failures"]:
                print(f"       - {failure}")
    print("\n" + summarise(results))
    if args.output:
        args.output.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"\nWrote {args.output}")
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
