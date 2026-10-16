# Lead capture and human handoff (Stage 10)

This guide covers how the assistant hands a customer over to staff, or captures a sales lead, without guessing and without collecting data the customer did not agree to give. It is for developers working on the backend or building a frontend on the chat API.

> **Status (Stage 10, Tasks 2–4).** Done: the escalation rules, the `escalation_offer` on chat replies and in history, the ticket, contact and event tables with a repository, and the validated request models. **Not done yet:** the HTTP endpoint that creates a ticket from the form, and the form itself on the chat page. Until they exist, an offer can be shown but not acted on.

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
| `support_tickets` | `ticket_id`, `reference_number` (e.g. `SUP-7K3M-9QX2`, `SAL-…` for sales), `session_id`, `contact_id`, `ticket_type`, `escalation_reason`, `subject`, `description`, `status`, `priority`, `created_at`, `updated_at`, `resolved_at`, `closed_at` | Links to its conversation by ID only; the transcript is never copied. `description` is the customer's own text from the form. Indexed for staff queues: `(status, created_at)`, `(ticket_type, status, created_at)`, `contact_id`, `session_id` |
| `ticket_events` | `event_id`, `ticket_id`, `sequence`, `event_type` (`created`/`status_changed`), `previous_status`, `new_status`, `created_at` | A ticket's status history in `sequence` order (events can share a timestamp) |

**IDs and times.** Internal IDs are random UUID4s. Reference numbers carry 40 random bits (`secrets`) from an alphabet without I, L, O or U, so they read well over the phone and cannot be guessed from one another. All timestamps are timezone-aware UTC; naive datetimes are refused.

**Constraints in the database**, not only in the code: allowed values for type, status, priority, preference, reason and event type; a reference prefix that matches the ticket type; text lengths; consent must be true; phone required for phone contact; `resolved_at` exactly when resolved (kept on closing, cleared on reopening); `closed_at` exactly when closed; well-formed events; foreign keys; one event per position.

**Statuses.** `open` → `in_progress` → `resolved` → `closed`. A resolved ticket can be reopened, and `closed` is final (`ALLOWED_TRANSITIONS` in `app/models/support.py`). Every change adds a `status_changed` event in the same transaction.

**Deleting data.** When a customer deletes a conversation, or retention cleanup removes an expired one, its tickets stay and their `session_id` becomes `NULL`: the customer's choice to delete the chat is respected, and the ticket remains actionable from its own description. A contact that still has tickets cannot be deleted by accident (`RESTRICT`). Deleting a ticket deletes its events. Retention for tickets and contacts is not implemented yet (see below).

**Repository.** [`SupportRepository`](../app/repositories/support_repository.py) stores a ticket, its contact (reused or new) and its `created` event in one transaction. It also changes statuses with their events and reads tickets, events and contacts. It checks that a linked conversation exists. Whether the caller may link it, meaning they hold that conversation's token, is for the service layer that the ticket endpoint will add.

### Schema changes and existing data

`init_schema()` only adds things: it creates missing tables and indexes and adds missing *nullable* columns with `ALTER TABLE … ADD COLUMN`. On an existing Stage 9 database it creates the three support tables and adds `conversation_messages.escalation_reason` (`NULL` for older replies, meaning no offer). No conversation or message is changed or removed. `tests/test_support_repository.py` upgrades a Stage 9-shaped file and checks this. A column added this way cannot carry a `CHECK` constraint in SQLite, so on upgraded files the allowed values of `escalation_reason` are enforced by the application models; new databases get the constraint too.

Database files stay out of Git (`*.db`, `*.sqlite`, `*.sqlite3` in `.gitignore`).

---

## The request form: validation

[`SupportRequestCreate`](../app/models/support.py) is the only way contact details enter the system:

| Field | Rules |
|-------|-------|
| `ticket_type` | `support` or `sales` (required) |
| `name` | 1–100 characters after trimming, a single line, any script |
| `email` | Syntax-checked with `email-validator` (no DNS lookup, so it is quick and works offline), domain normalised |
| `phone` | Optional, blank counts as none. Digits with spaces, `+`, `-`, `.`, `/`, brackets and an optional extension (`ext. 12`, `x123`); 5–15 digits before any extension. No country rules, so international numbers are not rejected |
| `contact_preference` | `email` (default) or `phone`; `phone` requires a phone number |
| `subject` | 1–150 characters, a single line |
| `message` | 1–2,000 characters, line breaks allowed. This is the description staff read first |
| `consent_given` | **Required, no default, and only the JSON value `true`.** A missing field, `false`, `"true"`, `1` and `"on"` are all refused, because submitting the form is not consent |

Whitespace and invisible characters around each value are removed and everything else is kept as written. Control characters are refused. Unknown fields such as `session_id`, `access_token`, `status` and `priority` are refused: the conversation comes from the URL and its token, and staff set the status and priority.

What the customer gets back, `SupportRequestCreated`, holds the reference number, type, status and creation time. It never includes their contact details. Contact records are for staff tooling only and no public API returns them.

**New dependency:** `email-validator` (the package Pydantic uses for `EmailStr`; it pulls in `dnspython` and `idna`). Nothing else was added, and there is no CRM.

---

## Not done yet

- **Ticket endpoint.** `POST /api/v1/conversations/{id}/support-requests` (conversation token required) and a standalone contact-form route, with idempotent retries and rate limits.
- **Frontend.** The offer button, the form with an unticked consent box, a dismiss action and the confirmation with its reference number.
- **Staff side.** Listing and updating tickets, and notifications such as email.
- **Retention and erasure** for tickets and contacts, plus a way to honour a deletion request for contact data.
- **Tuning the phrase rules** on real (anonymised) conversations, with any misses recorded the way the Stage 9 journey misses are.
