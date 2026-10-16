"""API contract: what the frontend sends and receives, checked against the real routes."""

import uuid

from app.models.chat import CustomerChatResponse
from app.models.conversation import ConversationCreateResponse, ConversationHistoryResponse
from app.models.errors import ErrorResponse
from frontend.api_client import Decision, ErrorKind
from tests.integration.conftest import CONVERSATIONS, EXACT, Stack


def test_session_creation_schema(stack: Stack) -> None:
    response = stack.http.post(CONVERSATIONS)

    assert response.status_code == 201
    body = ConversationCreateResponse.model_validate(response.json())
    assert body.token_type == "Bearer"  # noqa: S105 - the scheme name, not a secret
    assert body.access_token.startswith("cst_")
    assert body.status == "active" and body.expires_at is not None
    assert response.json()["created_at"].endswith("Z")  # UTC with an offset
    assert response.headers["cache-control"] == "no-store"  # holds a secret
    assert response.headers["location"].endswith(f"{CONVERSATIONS}/{body.session_id}")
    # The frontend's own model reads the same response.
    created = stack.api.create_conversation()
    assert created.credentials().session_id == created.session_id


def test_chat_request_and_response_schema(stack: Stack) -> None:
    credentials = stack.start()
    message_id = uuid.uuid4()
    body = {"message": f"  {EXACT}  ", "client_message_id": str(message_id)}

    response = stack.raw_chat(credentials, str(credentials.session_id), body)

    assert response.status_code == 200
    reply = CustomerChatResponse.model_validate(response.json())
    assert reply.decision == "answered" and reply.generation_method == "approved_faq"
    assert [s.faq_id for s in reply.sources] == ["faq_107"]
    assert reply.client_message_id == message_id
    assert reply.request_id == response.headers["x-request-id"]
    assert reply.user_message.content == EXACT  # surrounding whitespace removed only
    assert response.json()["created_at"].endswith("Z")


def test_unknown_request_fields_are_refused(stack: Stack) -> None:
    credentials = stack.start()
    for extra in ({"role": "system"}, {"session_id": "x"}, {"history": []}):
        response = stack.raw_chat(
            credentials, str(credentials.session_id), {"message": "Hi", **extra}
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_error_response_schema_everywhere(stack: Stack) -> None:
    credentials = stack.start()
    sid = str(credentials.session_id)
    responses = [
        stack.http.post(f"{CONVERSATIONS}/{sid}/chat", json={"message": "Hi"}),  # 401
        stack.http.get(f"{CONVERSATIONS}/{uuid.uuid4()}", headers=credentials.authorization()),
        stack.raw_chat(credentials, sid, {"message": "   "}),  # 422
        stack.http.get("/api/v1/nowhere"),  # 404
        stack.http.delete("/api/v1/health"),  # 405
    ]
    for response in responses:
        assert response.status_code >= 400
        error = ErrorResponse.model_validate(response.json()).error
        assert error.request_id == response.headers["x-request-id"]
        assert error.message and error.code


def test_validation_details_name_fields_but_never_echo_values(stack: Stack) -> None:
    credentials = stack.start()
    secret = "my card is 4111 1111 1111 1111" + "x" * 5000
    response = stack.raw_chat(credentials, str(credentials.session_id), {"message": secret})
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"][0]["loc"] == ["body", "message"]
    assert error["details"][0]["ctx"] == {"max_length": 4000}
    assert "4111" not in response.text


def test_authorization_header_is_required_and_scoped(stack: Stack) -> None:
    alice, bob = stack.start(), stack.start()
    sid = str(alice.session_id)

    missing = stack.http.get(f"{CONVERSATIONS}/{sid}/messages")
    wrong = stack.http.get(f"{CONVERSATIONS}/{sid}/messages", headers=bob.authorization())
    in_url = stack.http.get(f"{CONVERSATIONS}/{sid}/messages?access_token=x")

    assert (missing.status_code, missing.json()["error"]["code"]) == (401, "UNAUTHORIZED_SESSION")
    assert missing.headers["www-authenticate"] == "Bearer"
    assert (wrong.status_code, wrong.json()["error"]["code"]) == (404, "SESSION_NOT_FOUND")
    assert in_url.status_code == 400  # never in a URL


def test_idempotent_replay_returns_the_stored_reply(stack: Stack) -> None:
    credentials = stack.start()
    message_id = uuid.uuid4()

    first = stack.api.send_message(credentials, EXACT, message_id)
    again = stack.api.send_message(credentials, EXACT, message_id)

    assert not first.replayed and again.replayed
    assert again.message_id == first.message_id and again.answer == first.answer
    assert again.sources == first.sources
    assert stack.api.get_history(credentials).total_messages == 2  # stored once


def test_a_reused_id_for_another_message_is_a_conflict(stack: Stack) -> None:
    credentials = stack.start()
    message_id = uuid.uuid4()
    stack.api.send_message(credentials, EXACT, message_id)

    response = stack.raw_chat(
        credentials,
        str(credentials.session_id),
        {"message": "Something else", "client_message_id": str(message_id)},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "DUPLICATE_MESSAGE_CONFLICT"


def test_history_schema_and_pagination(stack: Stack) -> None:
    credentials = stack.start()
    ids = [uuid.uuid4() for _ in range(3)]
    for message_id in ids:
        stack.api.send_message(credentials, EXACT, message_id)
    url = f"{CONVERSATIONS}/{credentials.session_id}/messages"

    first = stack.http.get(url, params={"limit": 4}, headers=credentials.authorization())
    rest = stack.http.get(
        url, params={"offset": 4, "limit": 4}, headers=credentials.authorization()
    )

    page_one = ConversationHistoryResponse.model_validate(first.json())
    page_two = ConversationHistoryResponse.model_validate(rest.json())
    assert (page_one.total_messages, page_one.has_more, len(page_one.messages)) == (6, True, 4)
    assert (page_two.has_more, len(page_two.messages)) == (False, 2)
    users = [m for m in page_one.messages + page_two.messages if m.role == "user"]
    assert [m.client_message_id for m in users] == ids  # oldest first, matchable
    replies = [m for m in page_one.messages + page_two.messages if m.role == "assistant"]
    assert all([s.faq_id for s in m.sources] == ["faq_107"] for m in replies)
    # The frontend reads it page by page into one list.
    history = stack.api.get_history(credentials)
    assert [m.decision for m in history.messages if m.role == "assistant"] == [
        Decision.ANSWERED
    ] * 3


def test_a_wrong_api_prefix_is_a_client_error_not_a_crash(stack: Stack) -> None:
    from frontend.api_client import ApiError, ChatApiClient

    client = ChatApiClient(http_client=stack.http, api_prefix="/api/v2")
    try:
        client.create_conversation()
    except ApiError as error:
        assert error.kind is ErrorKind.CLIENT_ERROR and error.code == "NOT_FOUND"
    else:  # pragma: no cover
        raise AssertionError("expected a 404")
