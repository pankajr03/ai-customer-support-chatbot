"""A scripted demo against a running backend, through the frontend's own API client.

Usage (backend running, e.g. after scripts/start-demo.ps1):

    python scripts/demo_walkthrough.py
    python scripts/demo_walkthrough.py --url http://127.0.0.1:8001 --keep

It shows, with the backend's real replies:

1. how answers are produced right now (the readiness check: LLM off, on or paused);
2. grounded answers with their source FAQs, and a contextual follow-up;
3. a clarifying question for an ambiguous message, and the answer to the chosen option;
4. the safe fallback for a question the help articles don't cover;
5. persistence: the stored history read back, also by a second, independent client;
6. an idempotent retry (same client_message_id: the stored reply, not a new one);
7. a request with a wrong token refused like an unknown conversation;
8. deleting the conversation (skipped with ``--keep``).

Every reply is labelled with how it was produced (approved FAQ text, or reworded by the
LLM). Nothing is simulated: with the LLM off, no reply claims to be AI-written. The
access token is never printed. Exit status 1 if a step did not behave as described.
"""

import argparse
import sys
import textwrap
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # allow `python scripts/demo_walkthrough.py`
    sys.path.insert(0, str(PROJECT_ROOT))

from frontend.api_client import (  # noqa: E402
    ApiError,
    ChatApiClient,
    ChatReply,
    Credentials,
    ErrorKind,
)
from frontend.config import get_frontend_settings  # noqa: E402

METHODS = {
    "approved_faq": "approved FAQ text, word for word",
    "llm_grounded": "reworded by the LLM from the approved FAQ, and validated",
    "clarification": "clarifying question built from approved FAQ questions",
    "fallback": "configured safe message",
}
LLM_MODES = {
    "disabled": "LLM off: every answer is the approved FAQ text.",
    "enabled": "LLM on: answers may be reworded by OpenAI (paid requests); each says so.",
    "paused": "LLM paused after errors: answers are the approved FAQ text for now.",
}

failures: list[str] = []


def check(ok: bool, what: str) -> None:
    print(f"  {'ok ' if ok else 'NO '} {what}")
    if not ok:
        failures.append(what)


def show(label: str, text: str, reply: ChatReply) -> None:
    print(f"\nCustomer: {text}")
    answer = textwrap.fill(reply.answer, width=88, subsequent_indent="           ")
    print(f"Assistant: {answer}")
    print(f"  decision: {reply.decision}   produced as: {METHODS.get(reply.generation_method)}")
    for source in reply.sources:
        print(f"  source:   {source.faq_id}  {source.question}")
    if label:
        print(f"  ({label})")


def sources_of(reply: ChatReply) -> set[str]:
    return {source.faq_id for source in reply.sources}


def say(client: ChatApiClient, credentials: Credentials, text: str, label: str = "") -> ChatReply:
    reply = client.send_message(credentials, text, uuid.uuid4())
    show(label, text, reply)
    return reply


def main(argv: list[str] | None = None) -> int:
    settings = get_frontend_settings()
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", default=settings.base_url, help="the backend's base URL")
    parser.add_argument("--keep", action="store_true", help="don't delete the conversation")
    args = parser.parse_args(argv)
    client = ChatApiClient(args.url, api_prefix=settings.backend_api_prefix, read_timeout=180)

    print(f"Backend: {args.url}")
    try:
        readiness = client.check_readiness()
    except ApiError as error:
        print(f"The backend is not reachable ({error.kind}). Start it first: docs/demo-guide.md")
        return 1
    print(f"Ready: {readiness.status}. {LLM_MODES.get(readiness.checks.llm, 'LLM mode unknown.')}")
    if readiness.checks.semantic_search != "ready":
        print("Search model not loaded yet: the first answer can take a minute.")

    print("\n== 1. A conversation is created (the token stays in this process) ==")
    created = client.create_conversation()
    credentials = created.credentials()
    print(f"session_id {created.session_id}, expires {created.expires_at:%H:%M} UTC")

    print("\n== 2. Grounded answers with sources, and a contextual follow-up ==")
    first = say(client, credentials, "What is your return policy?")
    check(sources_of(first) == {"faq_018"}, "answered from the return-policy FAQ")
    delivery = say(client, credentials, "How long does delivery take?")
    check(sources_of(delivery) == {"faq_006"}, "answered from the delivery-time FAQ")
    follow = say(client, credentials, "What about express?", "a follow-up: express delivery")
    check(
        follow.decision in ("answered", "ambiguous")
        and bool(sources_of(follow) & {"faq_006", "faq_011"}),
        "the follow-up is answered from a delivery FAQ",
    )

    print("\n== 3. An ambiguous message gets a clarifying question ==")
    clarify = say(client, credentials, "I want my money back")
    check(clarify.decision == "ambiguous" and len(clarify.sources) > 1, "a clarifying question")
    if clarify.sources:
        offered = clarify.sources[0]
        chosen = say(
            client, credentials, offered.question, "the customer picks an offered question"
        )
        check(
            sources_of(chosen) == {offered.faq_id}, "the chosen question is answered from its FAQ"
        )

    print("\n== 4. A question the help articles don't cover: the safe fallback ==")
    fallback = say(client, credentials, "Do you price match competitors?")
    check(
        fallback.decision == "no_match" and not fallback.sources,
        "no invented answer, no sources",
    )

    print("\n== 5. Persistence: the stored conversation, read back ==")
    history = client.get_history(credentials)
    print(f"{history.total_messages} messages stored, oldest first:")
    for message in history.messages:
        print(f"  {message.role:9} {' '.join(message.content.split())[:70]}")
    other_client = ChatApiClient(args.url, api_prefix=settings.backend_api_prefix)
    again = other_client.get_history(credentials)
    check(again.messages == history.messages, "a second, independent client reads the same history")

    print("\n== 6. An idempotent retry: the same client_message_id ==")
    message_id = uuid.uuid4()
    original = client.send_message(credentials, "How can I track my order?", message_id)
    retried = client.send_message(credentials, "How can I track my order?", message_id)
    check(
        retried.replayed and retried.message_id == original.message_id, "the stored reply replayed"
    )
    after = client.get_history(credentials).total_messages
    check(after == history.total_messages + 2, "stored once, not twice")

    print("\n== 7. A wrong token is refused like an unknown conversation ==")
    wrong = Credentials(session_id=credentials.session_id, access_token="cst_" + "x" * 43)  # type: ignore[arg-type]
    try:
        client.get_history(wrong)
        check(False, "refused")
    except ApiError as error:
        check(error.kind is ErrorKind.SESSION_NOT_FOUND, f"refused ({error.code})")

    if not args.keep:
        print("\n== 8. Deleting the conversation ==")
        client.delete_conversation(credentials)
        try:
            client.get_history(credentials)
            check(False, "gone after deletion")
        except ApiError as error:
            check(error.kind is ErrorKind.SESSION_NOT_FOUND, "gone after deletion")

    print(
        "\nAll steps behaved as described."
        if not failures
        else f"\n{len(failures)} step(s) did not."
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
