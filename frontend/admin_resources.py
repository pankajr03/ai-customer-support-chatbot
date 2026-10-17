"""Objects shared by every browser session of the admin Streamlit server (Stage 11).

Only the API client and settings live here: the client holds no token and refuses
cookies. Nothing staff-specific may ever be cached with ``st.cache_data`` or
``st.cache_resource``: those caches are shared by every visitor of the server, so one
staff member's data would be shown to another. Per-session data lives in
``st.session_state`` (server-side, per browser session) only.
"""

import streamlit as st

from frontend.admin_api_client import AdminApiClient
from frontend.config import FrontendSettings, get_frontend_settings


def get_settings() -> FrontendSettings:
    return get_frontend_settings()


@st.cache_resource(show_spinner=False)
def get_admin_client() -> AdminApiClient:
    settings = get_frontend_settings()
    return AdminApiClient(
        settings.base_url,
        api_prefix=settings.backend_api_prefix,
        connect_timeout=settings.backend_connect_timeout_seconds,
        read_timeout=min(settings.backend_read_timeout_seconds, 30.0),
    )
