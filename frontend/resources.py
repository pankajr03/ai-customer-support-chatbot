"""Objects shared by every browser session of this Streamlit server.

Only things without per-visitor data belong here. The API client is shared so its
connection pool is reused; it holds no token and refuses cookies (see
``frontend.api_client``). Tokens and messages live in ``frontend.state`` only.
"""

import streamlit as st

from frontend.api_client import BackendStatus, ChatApiClient
from frontend.config import FrontendSettings, get_frontend_settings

HEALTH_CACHE_SECONDS = 30


def get_settings() -> FrontendSettings:
    return get_frontend_settings()


@st.cache_resource(show_spinner=False)
def get_api_client() -> ChatApiClient:
    settings = get_frontend_settings()
    return ChatApiClient(
        settings.base_url,
        api_prefix=settings.backend_api_prefix,
        connect_timeout=settings.backend_connect_timeout_seconds,
        read_timeout=settings.backend_read_timeout_seconds,
        max_retries=settings.backend_max_retries,
        max_retry_wait=settings.backend_max_retry_wait_seconds,
    )


@st.cache_data(ttl=HEALTH_CACHE_SECONDS, show_spinner=False)
def backend_status(_client: ChatApiClient) -> BackendStatus:
    """The backend's readiness (cached briefly for all sessions; see ``GET /ready``)."""
    return _client.backend_status()
