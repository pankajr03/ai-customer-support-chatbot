from fastapi import APIRouter, Request

from app.models.health import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse, summary="Health check")
def health(request: Request) -> HealthResponse:
    """Report that the service is running.

    Used by load balancers, uptime monitors and deploy scripts. It deliberately
    does no expensive work, so it stays fast and cheap to call often.
    """
    return HealthResponse(status="healthy", service=request.app.state.settings.service_name)
