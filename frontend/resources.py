"""Objects shared by every browser session of this Streamlit server.

Only things without per-visitor data belong here. The API client is shared so its
connection pool is reused; it holds no token and refuses cookies (see
``frontend.api_client``). Tokens and messages live in ``frontend.state`` only.
"""

import streamlit as st

from frontend.api_client import ApiError, ChatApiClient
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
def backend_is_healthy(_client: ChatApiClient) -> bool:
    """Whether the backend answered its health check (cached briefly for all sessions)."""
    try:
        _client.check_health()
    except ApiError:
        return False
    return True
