from fastapi import APIRouter

from app.api.deps import ChatServiceDep
from app.schemas.chat import ChatRequest, ChatResponse

router = APIRouter(tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest, service: ChatServiceDep) -> ChatResponse:
    """Send a customer message and receive the assistant's reply."""
    return service.reply(request)
