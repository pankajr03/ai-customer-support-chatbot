from pydantic import BaseModel, Field


class RootResponse(BaseModel):
    """Basic information about the running API."""

    message: str = Field(examples=["AI Customer Support Chatbot API"])
    version: str = Field(examples=["0.1.0"])
