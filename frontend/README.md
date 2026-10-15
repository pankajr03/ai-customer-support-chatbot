# Streamlit frontend

A customer chat page for demos and internal review. It is a **client of the FastAPI backend and nothing more**: it has no FAQ data, embedding model or OpenAI key, and every answer comes from `POST /api/v1/conversations/{id}/chat` ([integration guide](../docs/chat-api.md)).

> For local and internal use only. The backend has no authentication of client sites or customers yet, and the rate limits are process-local (see the main README, "Stage 8 preview").

## Run it

Two terminals, both from the project root with the virtual environment active:

```powershell
# 1. The backend. SEMANTIC_INDEX_PRELOAD loads the embedding model at startup, so the
#    first customer message doesn't wait for it (it can take minutes on Windows).
$env:SEMANTIC_INDEX_PRELOAD = "true"
uvicorn app.main:app

# 2. The page (opens http://localhost:8501)
python -m streamlit run frontend/app.py
```

Start the page from the project root: Streamlit reads [.streamlit/config.toml](../.streamlit/config.toml) from the current directory.

The page talks to the backend server-to-server (Python to Python), not from the browser, so the backend's `CORS_ORIGINS` does **not** need to list it.

## Configuration

Environment variables or the project's `.env` (the backend's variables in the same file are ignored). All are optional.

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
└── styles.css        # A few light touches on top of the theme
```

Nothing in `frontend/` imports `app/`, and the reverse is also true. The response models in `api_client.py` mirror the public contract instead of importing `app.models`: importing those would load the backend's services, and the two sides share the HTTP contract, not the code.

**Sending a message.** Submitting the input queues the message with a new `client_message_id` and disables the input. The page then sends it inside a spinner and reruns with the reply. Network errors, `409 REQUEST_IN_PROGRESS`, `429` and `503` are retried automatically with the same ID. A timeout is not retried automatically: the page has already waited the full read timeout, so it shows **Try again** instead, which resends the same ID. The backend then returns the stored reply if the first attempt finished, so a message is never answered twice. A message that cannot be sent (too long, or a closed conversation) is marked "Not sent".

**Replies.** `answered` shows the answer with a collapsed **Source** list. `ambiguous` shows the clarifying question with each offered FAQ as a button. `no_match` and `retrieval_unavailable` are never shown as answers: they get a note and the optional support link. A caption says how each reply was produced (`approved_faq`, `llm_grounded`, ...).

**Errors.** Each `ErrorKind` has its own customer wording (`formatting.py`), and the backend's request ID is shown as a reference. Exception messages, tracebacks and server responses are never shown. Unexpected bugs show a generic Streamlit error, because `showErrorDetails = "none"`.

## Security choices

- **Text is rendered, never interpreted.** Customer messages and replies (which may be an LLM's rewrite) pass through `plain_markdown`, which escapes every Markdown character. HTML, images, hidden links, headings, colours and LaTeX all show as typed. Bare URLs still become links, showing the URL itself. `unsafe_allow_html` is never used. The only HTML on the page is the static `styles.css`.
- **The token stays in this session's memory.** It is a `SecretStr` (shown as `**********` if printed), sent only as `Authorization: Bearer`, never put in a URL, the page, a cache or a log line.
- **One shared HTTP client, no shared secrets.** The pooled `httpx.Client` (`st.cache_resource`) holds no token, refuses all cookies and does not follow redirects, so nothing leaks between visitors and the `Authorization` header is never sent elsewhere.
- **Logs** record method, route name, status, duration and the backend's request ID. They never record message text, tokens or response bodies.
- **Streamlit hardening** ([.streamlit/config.toml](../.streamlit/config.toml)): listens on `localhost` only, no error details for visitors, developer menu hidden, "magic" output off (so a stray expression can't print the state), no usage statistics, XSRF protection on.

## Session state and its limits

Each browser tab has its own `st.session_state` (one per WebSocket session) on the Streamlit server. The page keeps a single `ChatState` there ([state.py](state.py)) holding the conversation's session ID and access token, its status and expiry, the messages shown, the request status, the pending message with its `client_message_id`, and the last error. It survives Streamlit's reruns (every click and message), so the backend conversation is created once, with the first message, and never on a rerun or page load.

**This is not durable authentication.** Session state is server memory tied to one tab's connection:

| What happens | Result |
|--------------|--------|
| A rerun (any interaction) | Nothing is lost |
| A brief network drop that Streamlit reconnects | Usually nothing is lost (same session) |
| Reloading the page, closing the tab, opening a new tab | A new, empty session: the token is gone |
| Streamlit server restart or redeploy | Every session is gone |
| Several Streamlit servers behind a load balancer without sticky sessions | Sessions are lost when a reconnect lands on another server |

The backend returns an access token only once and never again, so **a conversation whose token is lost cannot be reopened**. The page simply starts a new one with the next message. The old conversation expires on the backend after `CONVERSATION_TTL_MINUTES` and is deleted by the backend's cleanup. Nothing is written to the URL, browser storage or cookies to survive reloads: that would expose the token to browser history, other scripts and other users of the device.

**Recovery that does work:**

- **A lost or timed-out reply:** **Try again** resends the same message with the same ID, and the backend returns the stored reply.
- **A page that may be out of date** (for example after an interrupted request): **Refresh messages** in the sidebar reloads the stored history with the same token.
- **An expired or deleted conversation** (`410`, or `404` when the token no longer matches): the credentials are cleared, the messages stay visible, and the page offers **Start a new conversation**.
- **Delete conversation** deletes it on the backend and clears the credentials and messages from the session. **New conversation** closes the old one on the backend first (best effort).

Real customer identity, resuming a conversation across devices or reloads, and where a browser widget should keep its token are open design questions for Stage 8 (see the main README).

## Tests

```powershell
pytest tests/test_frontend_api_client.py tests/test_frontend_state.py tests/test_frontend_app.py
```

- **API client** (`test_frontend_api_client.py`): runs against the real backend in-process, with Starlette's `TestClient` as the transport, and against scripted transports for timeouts, broken connections, every error status, retries and malformed replies.
- **State and actions** (`test_frontend_state.py`).
- **The page** (`test_frontend_app.py`): runs headless with Streamlit's `AppTest` against the in-process backend.

All tests run offline.
