from typing import Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """Result of a liveness check."""

    status: Literal["healthy"] = Field(examples=["healthy"])
    service: str = Field(examples=["ai-customer-support-chatbot"])
