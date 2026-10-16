# Streamlit frontend: manual testing checklist

Checks to run by hand before a demo or a release of the Streamlit chat page ([frontend/README.md](../frontend/README.md)). Each check lists the steps, the expected result, and the latest recorded result.

**About the recorded results.** On 2026-10-16 every check below was run by an **automated script driving headless Chrome** (Chrome DevTools Protocol) against a live backend and a live Streamlit server on Windows 10 with Python 3.13.3. The script typed into the page, clicked its buttons, read the page text and the backend's request log, and took screenshots that were then looked at. That is not the same as a person using a real browser, so the **"By hand" box of every check is still unticked**. Tick it when you have done the check yourself.

Not covered by any check so far: other browsers (Firefox, Safari, Edge), a real phone (only a 390 px emulated viewport), screen readers and keyboard-only use, and replies written by the LLM (`LLM_ENABLED=true` with a real key, which makes paid calls).

---

## Before you start

Two PowerShell terminals, both in the project root with the virtual environment active (`.\.venv\Scripts\Activate.ps1`), and the dependencies installed (`pip install -r requirements.txt`).

## 1. Start FastAPI

```powershell
$env:SEMANTIC_INDEX_PRELOAD = "true"   # load the embedding model now, not on the first question
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

- **Expected:** the log ends with `Application startup complete.` after a `Loaded embedding model ... in N s` line. On the test machine N was 90–163 s. http://127.0.0.1:8000/api/v1/health answers `{"status":"healthy",...}`.
- **Recorded:** started with this command (plus `--port 8765` to avoid a server already running), and startup completed with the model preloaded.
- [ ] By hand

## 2. Start Streamlit

```powershell
streamlit run frontend/app.py
```

`python -m streamlit run frontend/app.py` works too. Start it from the project root, so `.streamlit/config.toml` is used.

- **Expected:** `You can now view your Streamlit app in your browser. URL: http://localhost:8501`. Only a local URL is printed, because the server listens on `localhost` only.
- **Recorded:** started with this command (plus `--server.port 8599`). Only the local URL was printed.
- [ ] By hand

## 3. Open the browser

Open http://localhost:8501.

- **Expected:** the "Customer Support" title, the welcome message, three "Popular questions" buttons and an enabled message box. The sidebar shows **Active**, "Expires in 59 min" and **Online**.
- **Recorded:** as expected. The page was ready to type into 1.9–3.3 s after loading, and 7.5 s on the first load after Streamlit started. Once, a first load after starting Streamlit took about 40 s; that could not be reproduced.
- [ ] By hand

## 4. Create a conversation

Nothing to click: opening the page creates it.

- **Expected:** exactly one `POST /api/v1/conversations -> 201` in the backend log per opened tab. Interacting with the page (reruns) creates no more. The token is never shown on the page.
- **Recorded:** one create per page open. The sidebar showed **Active**.
- [ ] By hand

## 5. Send a simple FAQ question

Type `How long does delivery take?` and press Enter.

- **Expected:** a spinner while waiting, then the delivery-times answer. **Sources used** opens to show "How long does delivery take?" and the note "The help articles this reply was based on.", and the caption reads "Answer from our help articles".
- **Recorded:** as expected, including the sources section.
- [ ] By hand

## 6. Send a contextual follow-up

After check 5, type `And how much does express cost?`.

- **Expected:** an answer that uses the earlier question as context. The backend log shows `rewrite=deterministic/rewritten` (with the LLM off).
- **Recorded:** answered, and the backend rewrote the follow-up. **Quality note:** in most of five runs the reply was the delivery-*times* FAQ, and only once the shipping-*costs* one, so the follow-up was understood but often not well matched. That's a backend retrieval limitation, not a frontend fault.
- [ ] By hand

## 7. Test an unrelated question

Type `What is the weather in Paris tomorrow?`.

- **Expected:** the backend's fallback, "Sorry, I couldn't find a reliable answer...", captioned "Automatic message · I couldn't find this in our help articles. Try rephrasing your question, or contact our support team." No sources.
- **Recorded:** as expected. Other unrelated questions ("Tell me a joke", "What is the capital of Australia?") also gave the fallback. **Quality note:** "Do you sell submarines?" was *answered* with the shipping-countries FAQ (a backend false positive).
- [ ] By hand

## 8. Test an ambiguous question

Type `refund`.

- **Expected:** a clarifying question with one button per offered FAQ ("Where will my refund be sent?", "Why is my refund less than I expected?"), captioned "Clarifying question · Choose the question closest to yours, or type a reply." Clicking a button sends that question and gets its answer.
- **Recorded:** as expected, including the click.
- [ ] By hand

## 9. Test backend unavailability

With the page open and a conversation going, stop the backend (Ctrl+C in terminal 1). Then send `How do I reset my password?`.

- **Expected:** "We can't reach the support service right now. Select Try again in a moment." The message stays on the page, marked "Not answered yet", and the message box stays usable. No traceback, error number or class name is shown. Start the backend again, wait for `Application startup complete.`, and click **Try again**: the answer appears once.
- **Recorded:** passed. See [the details below](#check-9-result).
- [ ] By hand

## 10. Test network timeout behaviour

Use a short read timeout and the backend's slow first model load:

```powershell
# terminal 1: without preloading the model
$env:SEMANTIC_INDEX_PRELOAD = "false"
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
# terminal 2: give up after 5 s
$env:BACKEND_READ_TIMEOUT_SECONDS = "5"
streamlit run frontend/app.py
```

Ask `How long does delivery take?`.

- **Expected:** "This is taking longer than expected. Select Try again to get the reply; your message won't be sent twice." The message stays, marked "Not answered yet", and the message box stays usable. After `Loaded embedding model` appears in the backend log, click **Try again**: the answer appears **once**.
- **Recorded:** as expected. The message appeared 12.6 s after sending, not about 5 s: the CPU was saturated by the model load at the time, which slowed the page itself. The model took 163 s to load. The backend finished the timed-out request on its own, so **Try again** got that stored reply back under the same message ID: two chat requests reached the backend, and the page showed one answer.
- Afterwards: `Remove-Item Env:BACKEND_READ_TIMEOUT_SECONDS, Env:SEMANTIC_INDEX_PRELOAD`.
- [ ] By hand

## 11. Test duplicate submission prevention

Type `How can I track my order?` and press Enter three times quickly.

- **Expected:** one customer bubble, one answer, and one `POST .../chat -> 200` in the backend log.
- **Recorded:** as expected: one bubble and one chat request. The message box is disabled from the moment of submission (`submit_mode="disable"`) until the reply arrives.
- [ ] By hand

## 12. Test conversation deletion

Send any message, then in the sidebar click **Delete conversation**.

- **Expected:** a confirmation ("Permanently delete this conversation and all its messages from our servers? This can't be undone.") and **nothing deleted yet**. Click **Delete permanently**: a "Conversation deleted." toast, one `DELETE /api/v1/conversations/{id} -> 204` in the log, the page back to the welcome message, and the sidebar showing **Not started** with Refresh, End and Delete disabled. No new conversation starts until you write or click **New conversation**.
- **Recorded:** as expected. No DELETE request before the confirmation; exactly one after.
- [ ] By hand

## 13. Test new conversation

During a conversation, click **New conversation** in the sidebar.

- **Expected:** the page clears to the welcome message and the sidebar shows **Active**. The log shows a new `POST /api/v1/conversations -> 201`, then a `POST .../close -> 200` for the old conversation (closed, not deleted).
- **Recorded:** as expected.
- [ ] By hand

## 14. Test mobile-width layout

Open the browser's developer tools, switch to device mode at 390 px wide (or use a phone on the same machine's browser), and send a message.

- **Expected:** the title isn't hidden under the top bar, the bubbles fill the width, the sidebar collapses behind a `>>` button, and there is **no horizontal scrolling**.
- **Recorded:** at an emulated 390 × 844 viewport, the page's scroll width equalled the viewport width (390 px) and the screenshot looked right. Not tried on a real phone.
- [ ] By hand

## 15. Test long message rendering

Send `Order XXXX…` with a 160-character unbroken word, followed by a long URL (`https://example.com/very-long-path/very-long-path/...`).

- **Expected:** the word and the URL wrap inside the bubble, with no horizontal scrolling at desktop or phone width. The text shows exactly as typed: `<b>` tags or `![image](...)` Markdown appear literally and are not rendered. A bare URL becomes a link whose text is the URL itself.
- **Recorded:** wraps at 1280 px and at 390 px with no horizontal scrolling. HTML and image Markdown showed literally, and the URL was a link showing the URL.
- [ ] By hand

---

## Results summary (automated headless Chrome, 2026-10-16)

| # | Check | Result |
|---|-------|--------|
| 1–4 | Start, open, create a conversation | Passed |
| 5 | Simple FAQ question, with sources | Passed |
| 6 | Contextual follow-up | Passed (answered); answer quality limited, see note |
| 7 | Unrelated question | Passed; one false positive noted ("submarines") |
| 8 | Ambiguous question and choice | Passed |
| 9 | Backend unavailable | Passed (after fixing the stale **Online** badge; see [check 9 result](#check-9-result)) |
| 10 | Network timeout and Try again | Passed |
| 11 | Duplicate submission | Passed |
| 12 | Delete with confirmation | Passed |
| 13 | New conversation | Passed |
| 14 | Mobile width (emulated 390 px) | Passed |
| 15 | Long message | Passed |

### Check 9 result

Passed (6 of 6 automated checks, 2026-10-16). The backend process was stopped and its port confirmed closed. The page then showed "We can't reach the support service right now. Select Try again in a moment." with no traceback, error number or class name. The message stayed, marked "Not answered yet", with **Try again** and a usable message box, and no reply was invented. The sidebar showed **Unavailable**. After the backend was restarted, **Try again** produced the answer once, with exactly one new chat request in the backend log.

The first attempt at this check was invalid: the test harness failed to stop the old `--reload` backend processes, so the backend never actually went down. The same run showed that the sidebar could still say **Online** for up to 30 s while the page reported the backend unreachable (the health result is cached). That was fixed: while that error is shown, the health check is no longer cached. The 6-of-6 result above is from the run after the fix.

The automated scripts used for these runs are not part of the repository. Repeat the checks by hand with the steps above.

---

## Conversation lifecycle (Stage 9)

The whole journey is automated in `tests/test_frontend_lifecycle.py`: the real page (`AppTest`) against the real backend in-process, with a scripted embedding model. It checks one conversation per browser session however many reruns, the first answer and a follow-up resolved from the history, persistence, reruns and **Refresh messages** without loss or duplicates, message order, source references, close, delete, two sessions isolated from each other, and a lost session starting over. What survives a lost session is in [frontend/README.md](../frontend/README.md#what-survives-a-lost-browser-session).

**Live run, 2026-10-16** (real `all-MiniLM-L6-v2` model, LLM off, through the frontend's API client against `uvicorn` with `SEMANTIC_INDEX_PRELOAD=true`; model loaded in 79.5 s):

| Step | Message | Result |
|------|---------|--------|
| First question | "What is your return policy?" | `answered`, `faq_018`, approved FAQ text |
| Follow-up | "What about if it was a gift?" | `ambiguous`: offers `faq_022` (gift returns) and `faq_018` |
| Follow-up | "Do I have to pay to send it back?" | `answered`, `faq_020` (return shipping) |
| Follow-up | "How long does that take?" | `ambiguous`: "refunds or returns?" (`faq_023`, `faq_020`) |
| Retry of the first message, same ID | | Replayed (`Idempotent-Replayed`), not answered or stored again: 8 stored messages, not 10 |
| History | | 8 messages in order, UTC timestamps increasing, each reply with its sources and approved questions |
| New session | | New conversation with 0 messages; the old one gives `404` with any other token |
| Close, then delete | | Closed history still readable (8 messages); after delete, `404` |

Follow-ups are resolved by the conservative rule-based rewrite when the LLM is off. Pronoun follow-ups ("that", "it") were answered or turned into a clarifying question offering the right FAQs; none was answered from an unrelated FAQ.

- [ ] By hand: the same journey in a real browser, including a page reload (a new, empty conversation is expected)
