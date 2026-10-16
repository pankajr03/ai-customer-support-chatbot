# Lead capture and human handoff (Stage 10)

This guide covers how the assistant hands a customer over to staff, or captures a sales lead, without guessing and without collecting data the customer did not agree to give. It is for developers working on the backend or building a frontend on the chat API.

> **Status (Stage 10, Tasks 2–8).** Done: the escalation rules and the `escalation_offer` on chat replies and in history; the ticket, contact and event tables; the ticket repository (listing, conversation scope, idempotency, duplicates); the status workflow with staff authentication; the escalation service that turns a confirmed request into a ticket; and the validated request models. **Not done yet:** the HTTP endpoints and the form on the chat page. Until they exist, an offer can be shown but not acted on.

---

## The three ways in

| Path | Example | What the chat does | `ticket_type` |
|------|---------|--------------------|---------------|
| **A. The customer asks for a person** | "I want to speak with a human." | Offers a support request: `escalation_offer.reason = customer_requested` | `support` |
| **B. The assistant cannot resolve it** | A second question in a row with no match; still unclear after a clarifying question; an order number or a problem only staff can check | Offers a support request: `repeated_no_match`, `unresolved_clarification` or `staff_access_needed` | `support` |
| **C. A sales or business inquiry** | "I want to discuss pricing for a business order." | Offers the optional contact form next to the normal reply: `sales_inquiry` | `sales` |

In every case the reply itself is unchanged: the customer still gets the FAQ answer, the clarifying question or the fallback message. The offer is an extra field next to it.

**An offer is not a ticket.** Chatting never creates a ticket and never asks for contact details. A ticket exists only after the customer opens the form, fills it in, ticks the consent box and submits it. Not acting on an offer is how a customer declines it.

---

## The rules

The rules are in [`app/services/escalation_policy.py`](../app/services/escalation_policy.py). They are fixed: no model, scores or randomness are involved, so the same conversation always gets the same offer, and every offer says which rule made it (`reason`). They reuse what each chat turn already decides: this turn's `decision`, plus the decisions and offers of recent replies.

The rules are checked in this order, and the first one that applies wins:

| # | Reason | Applies when | Decisions | Repeated? |
|---|--------|--------------|-----------|-----------|
| 1 | `customer_requested` | The message asks for a person: "speak with a human", "talk to someone", "connect me to customer service", "escalate this", just "agent please" | Any | Yes. It is the customer's explicit wish |
| 2 | `sales_inquiry` | The message is clearly a sales inquiry: "business/bulk/wholesale order or pricing", "get a quote", "become a reseller", "talk to your sales team" | Any | No, while an earlier sales offer is in the recent replies |
| 3 | `staff_access_needed` | The message concerns the customer's own order or account: an order reference ("order #48213"), "charged twice", "locked out of my account", "my parcel never arrived", "damaged item" | `no_match`, `ambiguous` | No, while any earlier support offer is in the recent replies |
| 4 | `unresolved_clarification` | The previous reply asked a clarifying question and this turn still did not resolve it | `no_match`, `ambiguous` | Same as 3 |
| 5 | `repeated_no_match` | This is the second `no_match` in a row | `no_match` | Same as 3 |

**Why not offer on every fallback?** One unanswered question is often fixed by rephrasing, and the no-match message already invites that. Offering a ticket every time would push customers to staff for questions the FAQ answers. A question about a business policy the FAQ does not cover gets an offer once rephrasing has also failed (rule 5), or straight away if it is about the customer's own order or account (rule 3).

**What never escalates on its own:**

- `answered` replies. The question was resolved. (Rules 1 and 2 still apply, so "can I talk to someone about returns?" gets the answer *and* an offer.)
- `retrieval_unavailable` replies. Search failed for a moment, and the fix is to try again. Rules 4 and 5 skip these replies when looking for "the previous reply", so a retry in between does not hide a repeat.
- A first unanswered question, or a first clarifying question.

**What is not treated as a request or a lead.** Questions about the assistant ("are you a human?", "am I talking to a bot?"), refusals ("I don't want to talk to anyone", "without speaking to an agent"), mentions of people ("the delivery person was rude", "do you have staff at weekends?"), ordinary price questions ("how much is this?", "do you have a discount code?"), problems with an existing order ("my business order has not arrived" is a support matter, not a lead) and quantities ("can I order 5000 items?" contains no order number). Matching is deliberately narrow: a missed lead costs less than treating every customer as one.

**"Recent replies"** means the assistant replies within the conversation history window (`CONVERSATION_HISTORY_MAX_MESSAGES`, 10 messages by default). Once an ignored offer falls out of that window, a rule can offer again.

**Where the rules run.** In `CustomerChatService` (step 8), after the reply has been validated, so the offer is based on the reply the customer actually receives. The offer is stored with the reply (`conversation_messages.escalation_reason`). A refreshed page (`GET .../messages`) and a retried request (`client_message_id`) therefore show the same offer. The turn's log line records `escalation=<reason or none>`, never the message text.

**Tests:** [`tests/test_escalation_policy.py`](../tests/test_escalation_policy.py) has phrase tables for each detector, every rule and its exceptions, and the same flows over HTTP.

---

## The API field

`POST /api/v1/conversations/{id}/chat` responses and each message in `GET .../messages` have an additive `escalation_offer` field:

```json
"escalation_offer": {"reason": "customer_requested", "ticket_type": "support"}
```

It is `null` for most replies and always `null` for customer messages. `ticket_type` follows from `reason` (only `sales_inquiry` is `sales`). A frontend should:

- show a clear action next to the reply, such as **Send a support request** or, for `sales`, **Contact our sales team (optional)**, and an equally easy way to dismiss it;
- open the form only when the customer chooses to;
- never treat the offer as consent or pre-fill consent.

Clients that do not know the field can ignore it. The Streamlit page ignores it for now.

---

## Submitting a request

[`EscalationService.submit`](../app/services/escalation_service.py) is the only path from a form to a ticket. It has no HTTP of its own; the endpoint will pass the validated request in.

1. **Confirmation and consent.** `confirmed` and `consent_given` must both be `true`; they are separate (one says "send this", the other "you may store my details") and both are checked again in the service, so code cannot skip them.
2. **Conversation authorisation** with the conversation's access token. An unknown conversation and a wrong token give the same `ConversationNotFoundError`; an expired conversation is refused. A *closed* conversation may still send a request, because the customer may have closed the chat and then wanted help.
3. **The offer that led here.** The most recent offer in the conversation's recent replies whose ticket type matches is recorded on the ticket (`escalation_reason`); otherwise the request counts as unprompted. If this lookup fails, the request still goes through as unprompted.
4. **Storage**, idempotent and atomic (below).
5. **The response**, `SupportRequestCreated`: reference number, type, status, creation time, `outcome` (`created`, `replayed` or `duplicate`), a confirmation `message` and `expected_response`.

What the confirmation says, and what it never says:

- Support: "Your support request has been recorded. Your reference number is SUP-…; please keep it for your records." Sales: "Thank you for your inquiry. It has been recorded with reference number SAL-…". Duplicate: "You have already sent this request, and it is still open under reference number …".
- It never says that someone was notified (there is no notification integration), never names or invents an assigned person and never mentions staff availability.
- `expected_response` is the business's own statement from `SUPPORT_RESPONSE_TIME_TEXT`, word for word, or `null`. A response time is never estimated.

The conversation is not changed: it stays open and the customer can keep chatting. Logs record the ticket ID, type, outcome and offer, never contact details or text.

The service also lists a conversation's tickets (`list_tickets`, paginated) and reads one by reference (`get_ticket`), returning `CustomerTicketView`: reference, type, status, subject and timestamps. It never returns contact details, the description or staff data. Another conversation's ticket is reported exactly like a missing one.

## Idempotency and duplicate submissions

Every submission carries a client-generated `client_request_id` (a UUID, e.g. `crypto.randomUUID()`), created once per submission and **sent again unchanged on every retry**. This is the same pattern as `client_message_id` in the chat API.

| Situation | Result |
|-----------|--------|
| A new ID | A new ticket (`outcome: created`) |
| The same ID and the same details, e.g. a retry after a timeout, a `503` or a lost reply | The original ticket as it is now (`replayed`); nothing new is stored |
| The same ID with different details | `IdempotencyKeyConflictError`; nothing is stored. A new request needs a new ID |
| A new ID with the same details while the earlier ticket is still open, within `TICKET_DUPLICATE_WINDOW_MINUTES` (24 h by default), e.g. pressing Send twice or reloading the form | The existing ticket (`duplicate`) |
| The same details after the earlier ticket was resolved or closed, or after the window | A new ticket |

"Same details" means the same `request_fingerprint`: a SHA-256 of type, name, email, phone, preference, subject and message, ignoring case in the name and email and ignoring spacing. IDs are scoped to the conversation: the same ID in another conversation neither returns nor conflicts with this one, so an ID can never reveal someone else's ticket.

How it holds under concurrency: the ID and fingerprint are stored on the ticket (`support_tickets.idempotency_key`, `request_fingerprint`) in the database, not in memory. The look-up and insert run in one `BEGIN IMMEDIATE` transaction, so concurrent requests, from any number of workers or processes on the same database, are serialised. A unique index on `(session_id, idempotency_key)` backs this up. `tests/test_ticket_repository.py` sends the same submission from 8 threads with separate connections and gets exactly one ticket. A failure after the ticket was stored but before the reply arrived is covered too: the retry is `replayed`.

Without a conversation there is no scope for an ID, so the repository creates a ticket on every call; the service always has a conversation.

---

## Data model

New tables in the existing SQLite database (`app/database/models.py`), created on startup by `Database.init_schema()`:

```
conversation_sessions 1 ─── 0..n support_tickets n ─── 1 customer_contacts
        (ON DELETE SET NULL)          │ (ON DELETE RESTRICT)
                                      1
                                      │ (ON DELETE CASCADE)
                                    0..n
                                 ticket_events
```

| Table | Holds | Notes |
|-------|-------|-------|
| `customer_contacts` | `contact_id`, `name`, `email`, `phone` (optional), `contact_preference` (`email`/`phone`), `consent_given` (must be true), `consent_timestamp` (latest consent), `created_at`, `updated_at`, `identity_hash` | Personal data, in its own table and never in chat messages. Identical details submitted again reuse the row (unique `identity_hash` over the normalised email, name, phone and preference); different details create a new row, so an unauthenticated submission can never overwrite the details staff use for someone else's earlier request |
| `support_tickets` | `ticket_id`, `reference_number` (e.g. `SUP-7K3M-9QX2`, `SAL-…` for sales), `session_id`, `contact_id`, `ticket_type`, `escalation_reason`, `subject`, `description`, `status`, `priority`, `assigned_to`, `created_at`, `updated_at`, `resolved_at`, `closed_at`, `idempotency_key`, `request_fingerprint` | Links to its conversation by ID only; the transcript is never copied. `description` is the customer's own text from the form. Indexed for staff queues `(status, created_at)` and `(ticket_type, status, created_at)`, a conversation's tickets `(session_id, created_at)`, duplicate checks `(session_id, request_fingerprint)`, `contact_id`, and unique `(session_id, idempotency_key)` |
| `ticket_events` | `event_id`, `ticket_id`, `sequence`, `event_type` (`created`/`status_changed`), `previous_status`, `new_status`, `actor_type` (`customer`/`staff`/`system`), `actor_id` (the staff ID), `assigned_to`, `created_at` | A ticket's status history in `sequence` order (events can share a timestamp), with who made each change |

**IDs and times.** Internal IDs are random UUID4s. Reference numbers carry 40 random bits (`secrets`) from an alphabet without I, L, O or U, so they read well over the phone and cannot be guessed from one another; uniqueness is enforced by a unique constraint, and a collision is retried. All timestamps are timezone-aware UTC; naive datetimes are refused.

**Constraints in the database**, not only in the code: allowed values for type, status, priority, preference, reason, event type and actor; a reference prefix that matches the ticket type; text lengths; consent must be true; phone required for phone contact; `assigned_to` required for `assigned`; `resolved_at` exactly when resolved (kept on closing, cleared on reopening); `closed_at` exactly when closed; well-formed events (a staff change names the staff member, only an assignment names an assignee); foreign keys; one event per position.

**Deleting data.** When a customer deletes a conversation, or retention cleanup removes an expired one, its tickets stay and their `session_id` becomes `NULL`: the customer's choice to delete the chat is respected, and the ticket remains actionable from its own description. A contact that still has tickets cannot be deleted by accident (`RESTRICT`). Deleting a ticket deletes its events. Retention for tickets and contacts is not implemented yet (see below).

**Repository.** [`TicketRepository`](../app/repositories/ticket_repository.py) holds all ticket persistence, ORM queries only, with no rules about who may do what:

| Method | Does |
|--------|------|
| `create_ticket` | Contact (reused or new), ticket and `created` event in one transaction, with idempotency and duplicate detection (above) |
| `get_ticket`, `get_ticket_by_reference` | Staff look-ups |
| `get_session_ticket`, `list_session_tickets` | Only one conversation's tickets (the caller authorises the conversation) |
| `list_tickets` | Filters (statuses, type, priority, conversation, assignee, created from/before), pages of up to 100, ordered by `created_at` then `ticket_id` so pages are stable |
| `change_status` | Applies a change and records its event in one transaction; refuses a change the workflow does not have, or one based on an outdated status (`expected_status`) |
| `list_events`, `get_contact` | History; contact details for staff tooling only |

Tickets never carry contact details (`SupportTicket` has only `contact_id`). Database failures become `TicketStoreError` (`TicketStoreBusyError` when locked), logged with the error type only.

### Schema changes and existing data

`init_schema()` upgrades an existing database on startup without losing anything:

- **Conversation tables are only extended**: missing tables, indexes and *nullable* columns are added (`ALTER TABLE … ADD COLUMN`). On a Stage 9 database this adds `conversation_messages.escalation_reason` (`NULL` for older replies, meaning no offer); no conversation or message is changed. A column added this way cannot carry a `CHECK` constraint in SQLite, so on upgraded files `escalation_reason` is checked by the application models; new databases get the constraint too.
- **Support tables are rebuilt when their definition changes**, because SQLite cannot change a `CHECK` constraint in place (for example the new statuses). The rebuild follows SQLite's documented procedure in one transaction: rename the old table, create the new one, copy every row, drop the old one, check foreign keys. Any problem, such as a row the new rules refuse or a reference the rebuild would break, rolls everything back, keeps the old table intact and stops startup with a clear error. Events stored before actors were recorded are attributed to `system`. Broken references that already existed do not block an upgrade.

`tests/test_ticket_repository.py` upgrades a Stage 9 file and a file with the first Stage 10 support tables, and checks a rollback. Database files stay out of Git (`*.db`, `*.sqlite`, `*.sqlite3` in `.gitignore`).

---

## Ticket status workflow

| Status | Meaning |
|--------|---------|
| `open` | Submitted; nobody has picked it up |
| `assigned` | A named staff member owns it (`assigned_to`) |
| `in_progress` | Staff are working on it |
| `waiting_for_customer` | Staff asked the customer something |
| `resolved` | Staff consider it answered |
| `closed` | Finished; final |

Allowed changes (`ALLOWED_TRANSITIONS` in `app/models/support.py`):

```
open ──► assigned ──► in_progress ◄──► waiting_for_customer
  ▲          │             │                  │
  └──────────┘ (unassign)  ├──► resolved ◄────┘
                           │       │  ▲
                           │       ▼  │ (reopen: resolved → in_progress only)
any unfinished status ─────┴──► closed (final)
```

In words: open → assigned or closed; assigned → in_progress, open (unassigned) or closed; in_progress → waiting_for_customer, resolved or closed; waiting_for_customer → in_progress, resolved or closed; resolved → closed or in_progress; closed → nothing.

**Reopening** is explicit and narrow: only `resolved` → `in_progress`, keeping the assignee. `resolved_at` is cleared and the event history keeps when it was resolved. A closed ticket is never reopened; a new request is a new ticket.

**Who may do what** ([`app/services/ticket_workflow.py`](../app/services/ticket_workflow.py)):

| Actor | May | Authenticated by |
|-------|-----|------------------|
| Staff | Every allowed change. Assignment must name a configured staff member; nobody is ever assigned automatically | A personal staff token (below) |
| Customer | Only withdraw an open request (open → closed) and answer staff (waiting_for_customer → in_progress). **Never assign or resolve** | The conversation's access token, for that conversation's tickets only |
| System | Nothing. No change happens automatically, so a chatbot answer never resolves a ticket | — |

`check_transition` is the pure rule (tested over every status pair and actor) and `allowed_next` lists the changes an actor may make, e.g. to decide which buttons to show. `TicketWorkflow` applies changes. It records each one with its actor in the same transaction, and only if the ticket is still in the status the decision was based on, so two staff members cannot overwrite each other's change. A ticket's status is independent of its conversation: closing or expiring the conversation leaves its tickets alone, and vice versa.

**Staff tokens.** Staff authenticate with a personal bearer token (`stf_` + 32 random bytes), handled like conversation tokens: shown once, stored only as a SHA-256 hash and compared in constant time against every configured hash. Create one with `python -m scripts.create_staff_token maria`, which prints the token for that person and the entry for `STAFF_TOKEN_HASHES` (a JSON list of `staff_id:hash`). With no entries, nobody is staff and every staff operation is refused. Revoke a token by removing its entry and restarting. This is a prototype scheme for a small team, with no staff login, expiry or roles yet.

---

## The request form: validation

[`SupportRequestCreate`](../app/models/support.py) is the only way contact details enter the system:

| Field | Rules |
|-------|-------|
| `client_request_id` | Required. A canonical UUID, generated by the client once per submission and repeated on retries (see [Idempotency](#idempotency-and-duplicate-submissions)) |
| `ticket_type` | `support` or `sales` (required) |
| `name` | 1–100 characters after trimming, a single line, any script |
| `email` | Syntax-checked with `email-validator` (no DNS lookup, so it is quick and works offline), domain normalised |
| `phone` | Optional, blank counts as none. Digits with spaces, `+`, `-`, `.`, `/`, brackets and an optional extension (`ext. 12`, `x123`); 5–15 digits before any extension. No country rules, so international numbers are not rejected |
| `contact_preference` | `email` (default) or `phone`; `phone` requires a phone number |
| `subject` | 1–150 characters, a single line |
| `message` | 1–2,000 characters, line breaks allowed. This is the description staff read first |
| `consent_given` | **Required, no default, and only the JSON value `true`.** A missing field, `false`, `"true"`, `1` and `"on"` are all refused, because submitting the form is not consent |
| `confirmed` | **Required, no default, only `true`**: the customer explicitly confirmed that the request should be sent |

Whitespace and invisible characters around each value are removed and everything else is kept as written. Control characters are refused. Unknown fields such as `session_id`, `access_token`, `status`, `priority` and `assigned_to` are refused: the conversation comes from the URL and its token, and only staff set the status, priority and assignee.

What the customer gets back never includes their contact details. Contact records are for staff tooling only and no public API returns them.

**Dependencies:** `email-validator` (the package Pydantic uses for `EmailStr`; it pulls in `dnspython` and `idna`). Nothing else was added: no CRM, external ticketing integration, queue or cache.

---

## Settings

| Variable | Default | Meaning |
|----------|---------|---------|
| `STAFF_TOKEN_HASHES` | `[]` | Staff allowed to change ticket statuses, as `staff_id:sha256` entries |
| `SUPPORT_RESPONSE_TIME_TEXT` | empty | The business's approved response-time statement, shown after submitting; empty shows none |
| `TICKET_DUPLICATE_WINDOW_MINUTES` | `1440` | How long the same open request counts as a duplicate (1–43,200) |

---

## Not done yet

- **HTTP endpoints.** `POST /api/v1/conversations/{id}/support-requests` and the listing and reading routes over `EscalationService` (conversation token required), the staff routes over `TicketWorkflow` (staff token required), error codes and rate limits.
- **Frontend.** The offer button, the form with unticked consent and confirmation boxes, a dismiss action and the confirmation with its reference number.
- **Staff tooling.** Viewing tickets and contacts, and real notifications such as email. Until one exists, nothing claims that anyone was notified.
- **Retention and erasure** for tickets and contacts, plus a way to honour a deletion request for contact data.
- **Tuning the phrase rules** on real (anonymised) conversations, with any misses recorded the way the Stage 9 journey misses are.
