"""FastAPI dependencies shared by route handlers."""

from typing import Annotated

from fastapi import Depends, Request

from app.core.config import Settings
from app.services.chat_service import ChatService


def get_app_settings(request: Request) -> Settings:
    """Return the settings the running app was created with."""
    return request.app.state.settings


def get_chat_service() -> ChatService:
    return ChatService()


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
ChatServiceDep = Annotated[ChatService, Depends(get_chat_service)]
