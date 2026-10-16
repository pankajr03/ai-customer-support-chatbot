# Streamlit frontend

A customer chat page for demos and internal review. It is a **client of the FastAPI backend and nothing more**: it has no FAQ data, embedding model or OpenAI key, and every answer comes from `POST /api/v1/conversations/{id}/chat` ([integration guide](../docs/chat-api.md)).

> For local and internal use only. The backend has no authentication of client sites or customers yet, and the rate limits are process-local (see the main README, "Stage 9 preview").

## Run it

Two terminals, both from the project root with the virtual environment active:

```powershell
# 1. The backend. SEMANTIC_INDEX_PRELOAD loads the embedding model at startup, so the
#    first customer message doesn't wait for it (it can take minutes on Windows).
$env:SEMANTIC_INDEX_PRELOAD = "true"
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

# 2. The page (opens http://localhost:8501)
streamlit run frontend/app.py            # or: python -m streamlit run frontend/app.py
```

Or both at once, each in its own window: `.\scripts\start-dev.ps1` (it checks the setup and waits for the backend to be ready first). `python scripts/check_setup.py` checks the configuration of both without starting anything.

The **Service** badge in the sidebar comes from the backend's readiness check (`GET /api/v1/ready`, cached for 30 s): **Online**; **Online** with a note that the first answer may be slower while search starts; **Limited** when semantic search failed to load; **Unavailable** when the backend is down or not ready (the page then does not start a conversation). A backend without `/ready` falls back to `/health`.

Start the page from the project root: Streamlit reads [.streamlit/config.toml](../.streamlit/config.toml) from the current directory.

The page talks to the backend server-to-server (Python to Python), not from the browser, so the backend's `CORS_ORIGINS` does **not** need to list it.

## Configuration

Environment variables or the project's `.env`, found from the project root whatever the current directory (the backend's variables in the same file are ignored). All are optional. An invalid value is named on the page and in the log, without the value.

| Variable | Default | Meaning |
|----------|---------|---------|
| `BACKEND_API_URL` | `http://127.0.0.1:8000` | Where the backend is. Use `https://` for anything but a local backend: tokens travel in a header (a warning is logged otherwise) |
| `BACKEND_API_PREFIX` | `/api/v1` | The backend's `API_V1_PREFIX` |
| `BACKEND_CONNECT_TIMEOUT_SECONDS` | `5` | Time allowed to connect |
| `BACKEND_READ_TIMEOUT_SECONDS` | `60` | Time allowed for a reply. A reply can include LLM calls, so keep it above `OPENAI_TIMEOUT_SECONDS × (OPENAI_MAX_RETRIES + 1)` |
| `BACKEND_MAX_RETRIES` | `2` | Automatic retries of one message after a network error, `409 REQUEST_IN_PROGRESS`, `429` or `503` |
| `BACKEND_MAX_RETRY_WAIT_SECONDS` | `5` | Longest `Retry-After` waited for automatically; longer waits are shown to the customer |
| `MAX_MESSAGE_CHARACTERS` | `4000` | Input limit; set it to the backend's `MAX_CUSTOMER_MESSAGE_CHARACTERS` |
| `SUPPORT_CONTACT_URL` | (none) | A "Contact support" link shown under "no match" replies |
| `DEMO_MODE` | `false` | A demo notice: the data is fictional, and how answers are produced right now (from the backend's `/ready`). See [docs/demo-guide.md](../docs/demo-guide.md) |
| `EXAMPLE_QUESTIONS` | three sample questions | JSON list of one-click questions on an empty conversation (up to 6) |

## How it is built

```
frontend/
├── app.py            # The page: layout, and the send flow (see its docstring)
├── api_client.py     # httpx client for the chat API; validated response models; ApiError
├── state.py          # ChatState in st.session_state: credentials, messages, pending message
├── actions.py        # What each control does: API call + state change (no Streamlit)
├── formatting.py     # Safe rendering of message text; customer-facing wording
├── resources.py      # Shared per server: settings, the pooled API client, health check
├── config.py         # FrontendSettings (BACKEND_API_URL, timeouts, ...)
├── components/
│   ├── chat.py       # Welcome, message bubbles, errors, suggestions
│   ├── sources.py    # Source references and clarification choices
│   └── sidebar.py    # Conversation status and controls, service status
└── styles.css        # Only what the theme can't do: bubble styles, long-word wrapping
```

Nothing in `frontend/` imports `app/`, and the reverse is also true. The response models in `api_client.py` mirror the public contract instead of importing `app.models`: importing those would load the backend's services, and the two sides share the HTTP contract, not the code.

**Opening the page.** The first run of a browser session starts one backend conversation (`POST /api/v1/conversations`) and stores its session ID, token, status and expiry in the session. Reruns never create another, and an existing conversation is never replaced automatically. If the backend is known to be down, or creating fails, the page says so without technical details and offers **Try again**. Creating a conversation is not idempotent, so it is never retried automatically.

**Sending a message.** Blank messages are ignored. Over-long ones, and ones with control characters other than tabs and line breaks, are refused before sending (the backend's rules) and stay in the message box. The limit is `MAX_MESSAGE_CHARACTERS`, lowered automatically if the backend reports a smaller one in a `422`. Submitting the input queues the message with a new `client_message_id` and disables the input. The page then sends it inside a spinner and reruns with the reply. Network errors (including a proxy's `502`), `409 REQUEST_IN_PROGRESS`, `429` and `503` are retried automatically with the same ID, at most `BACKEND_MAX_RETRIES` times and only for a `Retry-After` of up to `BACKEND_MAX_RETRY_WAIT_SECONDS`, so there is never a retry loop. A timeout is not retried automatically: the page has already waited the full read timeout, so it shows **Try again** instead, which resends the same ID. The backend then returns the stored reply if the first attempt finished, so a message is never answered twice. A message the backend refused (too long, a closed or expired conversation) is taken off the page and put back into the input, so nothing typed is lost. A reply that arrives twice (a replay) is shown once.

**One logical message, one ID.** The `client_message_id` is created when the customer submits a message and reused by every automatic retry, **Try again** and **Send again**. A new ID is used only for a new submission: a new message, **Ask again** after `retrieval_unavailable` (the first one was answered, with "try again later"), or a message re-sent from the message box after the backend refused it (its ID can't be reused: `DUPLICATE_MESSAGE_CONFLICT`).

**Unknown outcomes.** A timeout (ours, or a proxy's `504`), a broken connection or a `409` means the backend may have stored and answered the message. The page never assumes either way. It offers **Try again** with the same ID, which returns the stored reply if there is one. If the customer writes something else instead, the message is marked "May not have been sent" and the history is re-read after the next reply to find out. **Refresh messages** settles it at any time.

**Replies** follow the backend's `decision`; the page never re-judges a reply or shows scores or confidence figures. `answered` shows the answer with a collapsed **Sources used** section: the FAQ question titles in the backend's order, without duplicates, with an FAQ ID only when two titles are identical. Sources the page doesn't know the title of are left out, never invented. `ambiguous` shows the clarifying question with each offered FAQ as a button; the customer can also type a reply. `no_match` shows the backend's fallback message unchanged, plus the optional support link. `retrieval_unavailable` shows the backend's message and an **Ask again** button. That button sends the question as a new message, because resending the old ID would only replay the stored "unavailable" reply. A caption says how each reply was produced (`approved_faq`, `llm_grounded`, ...).

**Errors.** Each `ErrorKind` has its own customer wording (`formatting.py`) that says what to do next, and the backend's request ID is shown as a reference. Exception messages, tracebacks and server responses are never shown. A bug in the page itself is caught: it is logged on the server, and the customer sees "Something went wrong on this page" with a **Start over** button that resets this browser session's chat state. Streamlit's own error box (`showErrorDetails = "none"`) is only the last resort.

## Loading and error states

While a message is being answered, a spinner shows in an assistant bubble and the input is disabled, so it can't be submitted twice. After a recoverable failure the page stays usable: the customer can retry, keep typing or start over. Retrying never sends a message twice, because it reuses the message's `client_message_id`.

| Situation | Backend | What the customer sees | What they can do |
|-----------|---------|------------------------|------------------|
| Backend offline | connection refused | "We can't reach the support service right now." | **Try again**: starts the conversation, or resends the message |
| Network timeout | no reply in `BACKEND_READ_TIMEOUT_SECONDS`, or `504` from a proxy | "This is taking longer than expected." The message stays, marked "Not answered yet" | **Try again**: the same ID returns the reply if it was answered meanwhile |
| Connection broken, unreadable reply | transport error, `502` from a proxy, reply that breaks the contract | "The connection was interrupted." / "We received an unexpected reply." | **Try again** (same ID) |
| Request already processing | `409 REQUEST_IN_PROGRESS` | Retried automatically after `Retry-After`; if still busy: "Your previous message is still being answered." | **Try again** (same ID) |
| Rate limiting | `429` | Retried automatically for a short `Retry-After`; otherwise "...a little too quickly... try again in about 30 s." | **Try again** (same ID) after the wait |
| Temporary server failure | `503` | Retried automatically, then "temporarily unavailable" | **Try again** (same ID) |
| Unexpected server error | `500` | "Something went wrong on our side." Nothing was stored, so the message goes back into the message box | Send it again |
| Validation error | `422` (too long, blank, control characters), `413` | The specific problem, e.g. "too long (at most 4,000 characters)". The text goes back into the message box, and the page adopts the backend's limit | Edit and send |
| Duplicate request conflict | `422 DUPLICATE_MESSAGE_CONFLICT` | "It's back in the message box: please send it again." | Send it again (a new ID) |
| Invalid session | `404`, `401`, `403` | "This conversation can no longer be used." Input stops; the token is dropped | **Start a new conversation**: the unsent message comes back in the message box |
| Expired session | `410`, or `expires_at` passed | "This conversation has expired." Input stops; the token is kept so it can still be deleted | **Start a new conversation** (message kept), or **Delete** |
| Closed session | `409 SESSION_CLOSED`, or **End conversation** | "This conversation has ended." Messages stay readable | **Start a new conversation** (message kept) |
| Missing FAQ answer | `decision: no_match` | The backend's fallback message, the note "Not found in our help articles. Try rephrasing your question." and the optional **Contact support** link | Rephrase |
| Search unavailable | `decision: retrieval_unavailable` | The backend's message: "briefly unavailable", with the request ID as a reference (a normal reply, but a temporary failure) | **Ask again** (sent as a new message) |
| LLM unavailable | none (the backend answers with the approved FAQ text) | A normal answer, captioned "Answer from our help articles" | Nothing to do |
| Sidebar action fails temporarily | any temporary failure of New, End, Delete or Refresh | The error; the current conversation stays usable | **Try again** repeats that action |
| A bug in the page | any exception | "Something went wrong on this page." | **Start over** |

`tests/test_frontend_errors.py` covers every failure kind while sending a message and the page-level flows above.

## Look and feel

The design comes from Streamlit's theme in [.streamlit/config.toml](../.streamlit/config.toml): a light, neutral slate palette with one blue accent, subtle borders, pill-shaped buttons, smaller headings and comfortable top spacing. The `centered` layout keeps a readable column width (46rem) and narrows with the screen. There is no custom HTML; `styles.css` adds only what the theme can't do:

- **Who said what.** Customer messages are tinted blue and assistant messages are outlined. The avatars and screen-reader labels mark them too, so colour isn't the only cue.
- **Long words wrap.** Long words, URLs and order numbers break inside the bubble instead of widening the page.

The CSS uses only Streamlit's `data-testid` attributes, never generated class names, and sets no fixed widths. If an upgrade renames a test ID, the page still works, just without those touches.

**Accessibility** (WCAG 2.x AA needs 4.5:1 for normal text), measured in Chrome at 1280 px and 390 px:

| Element | Contrast |
|---------|----------|
| Message and body text | 17.9:1 |
| Captions (text at 60% opacity) | 4.6–4.7:1 |
| Links and primary buttons (blue on white) | 6.7:1 |
| Status badges (for example "Active") | 6.3:1 |

At 390 px the page has no horizontal scroll, even with a 160-character word and a long URL in a message. Links are underlined. Disabled buttons are paler by design, which WCAG exempts. `tests/test_frontend_styling.py` checks these contrast ratios from the theme colours, and that the CSS keeps to the rules above.

## Human support and sales contact

Stage 10 ([docs/lead-capture-and-handoff.md](../docs/lead-capture-and-handoff.md)). Code: [support.py](support.py) (state and validation, no Streamlit), [components/support.py](components/support.py) (the offer, form and confirmation), `submit_support_request` and `check_ticket_status` in [actions.py](actions.py).

**When it appears.** Only when relevant or asked for: under the latest reply when the backend offers it (`escalation_offer`, e.g. after "I want to speak with a human", a second unanswered question or a sales inquiry), with a **No thanks** button that hides the offer; and in the sidebar, always, as **Contact human support** and **Sales inquiry**. The form never opens on its own.

**The form** (`st.form`): name, email, optional phone, preferred contact method, a summary and description, the sales topic for sales inquiries, and an unticked consent checkbox ("not used for marketing"). Nothing is pre-filled. **Send request** is the explicit confirmation, sent as `confirmed: true`.

| Situation | What the page does |
|-----------|--------------------|
| Missing or invalid fields | Checked on the page with the backend's rules; nothing is sent; the fields are marked |
| Sending | A spinner, and the button is disabled: the request cannot be sent twice |
| Timeout, broken connection, `503`, `429` | Retried automatically (up to `BACKEND_MAX_RETRIES`), then **Try again**, always with the same `client_request_id` while the details are unchanged, so at most one ticket is created |
| The details are changed after a failure | A new request with a new ID |
| `422` | The fields to check; the next send uses a new ID |
| The conversation has expired or is gone | Says so: a new conversation is needed to contact support |
| Confirmed by the backend | The reference number, subject and status ("Recorded" until the team picks it up), with **Check status** |

**Tracking.** **Check status** reads the request through the conversation's own token (`GET /support/tickets/{id}?session_id=…`); the backend refuses any other conversation's request, so a reference number on its own reveals nothing. Statuses are shown as Recorded, Assigned to our team, Being handled, Waiting for your reply, Resolved and Closed, with the backend's explanation of what each means. The page never names a person, never says that someone joined the chat ("Nobody joins this chat. Any reply from our team comes by your preferred contact method.") and shows a response time only if the business configured one. There are no staff controls on the page.

**Scope.** Requests belong to the conversation on the page: a new or deleted conversation starts without them, and they are lost with the browser session (like the conversation itself); the customer keeps the reference number.

## Security choices

- **Text is rendered, never interpreted.** Customer messages and replies (which may be an LLM's rewrite) pass through `plain_markdown`, which escapes every Markdown character. HTML, images, hidden links, headings, colours and LaTeX all show as typed. Bare URLs still become links, showing the URL itself. `unsafe_allow_html` is never used. The only HTML on the page is the static `styles.css`.
- **The token stays in this session's memory.** It is a `SecretStr` (shown as `**********` if printed), sent only as `Authorization: Bearer`, never put in a URL, the page, a cache or a log line.
- **One shared HTTP client, no shared secrets.** The pooled `httpx.Client` (`st.cache_resource`) holds no token, refuses all cookies and does not follow redirects, so nothing leaks between visitors and the `Authorization` header is never sent elsewhere.
- **Logs** record method, route name, status, duration and the backend's request ID. They never record message text, tokens or response bodies.
- **Streamlit hardening** ([.streamlit/config.toml](../.streamlit/config.toml)): listens on `localhost` only, no error details for visitors, developer menu hidden, "magic" output off (so a stray expression can't print the state), no usage statistics, XSRF protection on.

## Session state and its limits

Each browser tab has its own `st.session_state` (one per WebSocket session) on the Streamlit server. The page keeps a single `ChatState` there ([state.py](state.py)) holding the conversation's session ID and access token, its status and expiry, the messages shown, the request status, the pending message with its `client_message_id`, and the last error. It survives Streamlit's reruns (every click and message), so the backend conversation is created once, when the page first opens, and never on a rerun.

**This is not durable authentication.** Session state is server memory tied to one tab's connection:

| What happens | Result |
|--------------|--------|
| A rerun (any interaction) | Nothing is lost |
| A brief network drop that Streamlit reconnects | Usually nothing is lost (same session) |
| Reloading the page, closing the tab, opening a new tab | A new, empty session: the token is gone |
| Streamlit server restart or redeploy | Every session is gone |
| Several Streamlit servers behind a load balancer without sticky sessions | Sessions are lost when a reconnect lands on another server |

The backend returns an access token only once and never again, so **a conversation whose token is lost cannot be reopened**, and its history can no longer be loaded or deleted from the page. The new browser session starts a new, empty conversation when the page opens. The old conversation and its messages stay on the backend, readable only with the old token (which nobody holds any more), until they expire after `CONVERSATION_TTL_MINUTES` and are removed by the backend's cleanup. Nothing is written to the URL, browser storage or cookies to survive reloads: that would expose the token to browser history, other scripts and other users of the device.

### What survives a lost browser session

Do not count on a browser refresh keeping `st.session_state`: assume any reload loses it.

| | Same browser session (reruns, clicks, **Refresh messages**, a brief reconnect) | Lost browser session (reload, closed tab, Streamlit restart) |
|---|---|---|
| The conversation on the page | Kept: same conversation, messages in the backend's order, each shown once | **Not recoverable**: the page starts a new, empty conversation |
| Stored messages and replies | On the backend, reloadable with **Refresh messages** | On the backend, but **unreachable**: the token is gone. Deleted at expiry |
| Source references of replies | Kept. A reply the page never received (e.g. after a timeout) comes back from the backend **with** its approved FAQ questions | Not shown (the conversation can't be opened) |
| A message being sent when the session ended | **Try again** resends it with the same ID; the backend answers it at most once | **Lost from the page**. If the backend received it, it is stored in the old conversation only |
| Text typed but not sent | Kept in the input (also after a refused message) | **Lost** |
| Deleting the old conversation | **Delete conversation** | **Not possible** from the page. It expires on its own after `CONVERSATION_TTL_MINUTES` |
| Other visitors' conversations | Never visible: each session has its own token, and every history load is checked against it | Never visible: a new session gets a new conversation, never an old one |

Covered by `tests/test_frontend_lifecycle.py`: one conversation per browser session however many reruns, nothing lost or duplicated by reruns and refreshes, message order and source references kept, two simultaneous sessions isolated, and a lost session starting over without access to the old conversation.

There is deliberately **no session-recovery mechanism**. Recovering a conversation would mean storing its token where it outlives the tab (URL, cookie, `localStorage`), or issuing a new token for an old conversation to whoever asks. The first exposes the token to browser history, logs, other scripts and other users of the device. The second would hand a conversation to anyone who knows its session ID. Recovery needs real customer identity first (Stage 9).

**Recovery that does work:**

- **A lost or timed-out reply:** **Try again** resends the same message with the same ID, and the backend returns the stored reply.
- **A page that may be out of date** (for example after an interrupted request): **Refresh messages** in the sidebar reloads the stored history with the same token.
- **An expired conversation** (`410`, or its `expires_at` has passed): input stops, the messages stay visible, and the page offers **Start a new conversation**. The token is kept, because the backend still accepts `DELETE` after expiry. A conversation the backend no longer knows (`404`) loses its token.
- **New conversation** creates the new backend conversation *first*. If that fails, the current conversation and its messages stay and the error is shown. On success the old conversation is closed on the backend (best effort): closed, not deleted.
- **Delete conversation** (with a confirmation) calls `DELETE` on the backend and only then clears the token and messages from the session. No new conversation is started until the customer asks for one or writes again. There is no "clear the screen" button, because hiding messages would look like deleting them while the backend still stores them.

Real customer identity, resuming a conversation across devices or reloads, and where a browser widget should keep its token are open design questions for Stage 9 (see the main README).

**Rate limits.** The backend limits new conversations per client IP (`RATE_LIMIT_NEW_CONVERSATIONS_PER_MINUTE`, 20 by default). Every visitor reaches the backend through the Streamlit server, so they all share that one IP and that one allowance. That's fine for demos. For more visitors, raise the limit, or put the page behind authentication that identifies each visitor (Stage 9).

## Conversation history

The backend's stored history is the authoritative copy of a conversation. The messages the page shows are only a view of it, rebuilt from the backend when that copy may differ:

- **Refresh messages** (sidebar) reloads the history.
- **Automatically after a reply**, when an earlier message was abandoned after a timeout, a broken connection or a `409`. Such a message is marked "May not have been sent", because the backend may have stored and answered it anyway. The history then shows what really happened.
- **Automatically after a replayed reply** (a message sent again that the backend had already answered), so it is shown where the backend stored it.

**Reconciliation strategy** when the page and the backend disagree:

| Page | Backend | Result |
|------|---------|--------|
| A message or reply | Has it (same stable ID) | Shown once, as the backend has it, in the backend's order |
| — | Has a message or reply the page doesn't | Shown (with its sources). The backend is the record of what was saved |
| A customer message waiting for **Try again** | Has it (its reply was lost on the way) | Settled: shown as sent with the stored reply. Nothing is resent |
| A customer message not sent, or not known to be | Doesn't have it | **Kept**, after the stored conversation, with its mark and **Send again** |
| A sent message | Doesn't have it (only beyond the newest 1,000 loaded) | Not shown; counted as "earlier messages aren't shown" |

Stable IDs: a customer message is matched by its `client_message_id` (the history returns it), a reply by its `message_id`. With a backend older than Stage 9, whose history has no `client_message_id`, an unsaved customer message is matched by its exact text instead, each at most once.

**Send again** appears on a customer message marked "Not sent" or "May not have been sent". It sends that message again with its **original** `client_message_id`: if the backend did store it, its stored reply comes back (shown where it belongs), and it is never answered twice.

How it is loaded and shown:

- Only through the authorized history endpoint, `GET /api/v1/conversations/{id}/messages`, with the token in the `Authorization` header.
- Page by page, 100 messages at a time (the backend's maximum), oldest first, as the backend orders them.
- At most the newest 1,000 messages are loaded. Above that, the page says how many earlier messages aren't shown.
- A message that appears on two pages (one was stored while paging) is kept once.
- Every page must belong to the requested conversation, or the history is rejected. A history is only applied if the page still shows the conversation it was loaded for, so histories of different conversations never mix.
- The history has no `generation_method`, so it is kept from the page's own copy of each reply. Sources are kept as the page showed them. A reply the page never received gets the sources the backend lists for it (`sources`: FAQ ID and approved question; FAQs withdrawn since are left out). Nothing is invented.
- A message still waiting for **Try again** stays at the end with its `client_message_id`, unless the history shows it was stored (see the table).
- An expired (`410`) or unknown (`404`) conversation is handled as described above. A temporary failure leaves the page as it was and shows the error.

After a rerun the page shows the same state from session state; nothing is fetched again.

## Tests

```powershell
pytest (Get-ChildItem tests	est_frontend_*.py)
```

287 tests in `tests/test_frontend_*.py` (97% line coverage of `frontend/`), plus the end-to-end suite in `tests/integration/`:

- **API client** (`test_frontend_api_client.py`): against the real backend in-process (Starlette's `TestClient` as the transport) and against scripted transports. Covers session creation, messages, history, close and delete, the `Authorization` header, timeouts, broken connections, every error status, retries with the same `client_message_id`, and malformed replies.
- **State and actions** (`test_frontend_state.py`): initial state, persistence across reruns, start-on-open, validation, retries, deduplication, expiry, new conversation, delete, and history sync.
- **The page** (`test_frontend_app.py`): runs headless with Streamlit's `AppTest` against the in-process backend. Covers the welcome screen, messages, sources, fallbacks, clarifications, delete confirmation, new conversation and refresh.
- **Error states** (`test_frontend_errors.py`): every failure kind while sending, invalid-session recovery, sidebar retries, the unexpected-error fallback.
- **Settings and styling** (`test_frontend_config.py`, `test_frontend_styling.py`).
- **The page with mocked HTTP responses** (`test_frontend_page.py`): initial load, the loading state, an interrupted send, input refused while a reply is awaited, answers with sources and follow-ups, error states, expiry, new conversation and delete confirmation.
- **Whole journeys and synchronisation** (`test_frontend_lifecycle.py`, `test_frontend_sync.py`, and `tests/integration/`): against the real backend in-process, with requests or replies lost on purpose.
- **Human support and sales contact** (`test_frontend_support.py`): form validation against the backend's limits, the request state (one ID per request across retries), the actions against the real backend (including a lost reply), and the page with mocked HTTP: the offer, declining it, an incomplete form, a confirmed success, a retry after `503`, a sales inquiry, status checks and the wording rules.

All tests run offline.

**What these tests cannot show.** Streamlit's `AppTest` runs the page's script and records the elements it produces. There is no browser. So these tests do **not** verify:

- how the page looks, its CSS, layout or mobile width;
- the spinner animation;
- the browser disabling the input on submit (`submit_mode="disable"`);
- real clicks: callbacks are invoked directly;
- page reloads and WebSocket reconnects: a "lost session" is simulated by opening a new `AppTest`;
- timing between the browser and the server.

The loading state is checked by stopping a run while its request is in flight (`st.stop()` from inside a mocked backend). That leaves the elements the browser shows while it waits, but not the spinner, which `AppTest` does not record. AppTest refuses to type into a disabled input, as a browser does. That is checked too.

Real-browser behaviour was checked separately, by an automated headless-Chrome run against live servers (screenshots looked at; see the [manual testing checklist](../docs/frontend-manual-testing.md)). That is still not a person using a real browser: the checklist's "By hand" boxes stay unticked until someone does. They never call OpenAI or download the embedding model (a scripted encoder stands in for it).

For checks in a real browser, see the [manual testing checklist](../docs/frontend-manual-testing.md).
