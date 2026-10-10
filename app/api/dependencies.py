"""FastAPI dependencies that hand application-wide objects to route handlers.

Strategy: expensive objects (like the FAQ service, which loads and validates the
JSON knowledge base) are built once in ``create_app()`` and stored on
``app.state``. These functions only look them up, so no request ever reloads data.

Tests can replace an object in two ways:
- build the app with ``create_app(settings, faq_service=...)``, or
- set ``app.dependency_overrides[get_faq_service] = lambda: fake_service``.
"""

from typing import Annotated

from fastapi import Depends, Request

from app.services.faq_service import FAQService


def get_faq_service(request: Request) -> FAQService:
    """Return the FAQService created at startup for this application."""
    service = getattr(request.app.state, "faq_service", None)
    if service is None:
        # Only happens if an app is assembled without create_app(); fail loudly.
        raise RuntimeError("FAQService is not initialised; build the app with create_app().")
    return service


FAQServiceDep = Annotated[FAQService, Depends(get_faq_service)]
