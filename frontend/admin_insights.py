"""Dashboard overview and chatbot analytics sections (Stage 11, Tasks 14 and 15).

Every number comes from the backend's aggregate endpoints (``/admin/overview``,
``/admin/analytics``), computed from the stored data: no sample or estimated figures,
no personal data (counts only). Each figure says what it counts and over which period,
and the backend's stated limitations are shown with them.
"""

import streamlit as st
from pydantic import SecretStr

from frontend.admin_api_client import AdminApiClient, ChatAnalytics, TicketAnalytics
from frontend.admin_common import call, when

WINDOWS = {"Last 7 days": 7, "Last 30 days": 30, "Last 90 days": 90}
DECISION_LABELS = {
    "answered": "Answered from an FAQ",
    "ambiguous": "Asked to clarify",
    "no_match": "No matching FAQ",
    "retrieval_unavailable": "Search unavailable",
}


def render_overview(client: AdminApiClient, token: SecretStr) -> None:
    st.header("Dashboard overview")
    overview = call(lambda: client.overview(token))
    if overview is None:
        return
    st.caption(f"Live counts from the database, as of {when(overview.generated_at)}.")

    st.subheader("Knowledge base")
    if overview.faqs is None:
        st.info("FAQs on this server are not stored in the database.")
    else:
        faqs = overview.faqs
        cols = st.columns(4)
        cols[0].metric("Total FAQs", faqs.total)
        cols[1].metric("Active FAQs", faqs.active)
        cols[2].metric("Inactive FAQs", faqs.inactive)
        cols[3].metric("Last FAQ change", when(faqs.last_updated_at))

    st.subheader("Conversations")
    if overview.conversations is not None:
        cols = st.columns(2)
        cols[0].metric("Stored conversations", overview.conversations.stored)
        cols[1].metric("Started in the last 7 days", overview.conversations.started_last_7_days)
        st.caption(
            "Expired conversations are deleted by retention cleanup, so this is the number "
            "currently stored, not a lifetime total."
        )

    st.subheader("Support tickets and leads")
    tickets = overview.tickets
    cols = st.columns(5)
    cols[0].metric("Total tickets", tickets.total)
    cols[1].metric("Open (not resolved or closed)", tickets.open)
    cols[2].metric("Resolved", tickets.resolved)
    cols[3].metric("Closed", tickets.closed)
    cols[4].metric("Sales leads (all time)", tickets.leads)
    st.caption(
        f"All time. {tickets.created_last_7_days} created in the last 7 days; "
        f"{tickets.open_queue} waiting on staff (open, assigned or in progress)."
    )
    if tickets.total:
        st.bar_chart({"tickets": tickets.by_status}, horizontal=True, x_label="tickets")
    else:
        st.info("No tickets yet.")


def render_analytics(client: AdminApiClient, token: SecretStr) -> None:
    st.header("Analytics")
    window = st.selectbox("Period", list(WINDOWS), index=1)
    data = call(lambda: client.analytics(token, days=WINDOWS[window]))
    if data is None:
        return
    st.caption(
        f"{window} (UTC days from {data.since.date().isoformat()}, today included). "
        "Chatbot decisions, not verified customer outcomes."
    )
    render_activity(data)
    render_rates(data)
    render_latency(data)
    render_trends(data)
    with st.expander("Limitations of these figures", expanded=True):
        for limitation in data.limitations:
            st.markdown(f"- {limitation}")
    tickets = call(lambda: client.ticket_analytics(token, days=WINDOWS[window]))
    if tickets is not None:
        render_ticket_analytics(tickets)


def render_activity(data: ChatAnalytics) -> None:
    st.subheader("Activity")
    cols = st.columns(4)
    cols[0].metric("Conversations started", data.conversations_started)
    cols[1].metric("Customer messages", data.customer_messages)
    cols[2].metric("Chatbot responses", data.chatbot_responses)
    cols[3].metric("Human-support offers", data.escalation_offers)
    cols = st.columns(4)
    for col, (decision, label) in zip(cols, DECISION_LABELS.items(), strict=True):
        col.metric(label, data.decisions.get(decision, 0))
    cols = st.columns(3)
    cols[0].metric("Support requests", data.tickets.support)
    cols[1].metric("Sales inquiries", data.tickets.sales)
    cols[2].metric("Requests after a chat offer", data.tickets.after_chat_offer)


def render_rates(data: ChatAnalytics) -> None:
    st.subheader("Rates")
    st.dataframe(
        [
            {
                "Rate": r.name,
                "Value": f"{r.percent:.1f}%" if r.percent is not None else "—",
                "Count": f"{r.numerator} of {r.denominator}",
                "Formula": r.formula,
                "Note": r.note or "",
            }
            for r in data.rates
        ],
        hide_index=True,
        width="stretch",
    )
    st.caption(
        f"Rates need at least {data.min_sample} chatbot responses. The answered response "
        "rate counts replies given from an approved FAQ; it is not a resolution rate."
    )


def render_latency(data: ChatAnalytics) -> None:
    st.subheader("Response time")
    latency = data.latency
    if not latency.samples:
        st.info("No answered exchanges in this period.")
        return

    def ms(value: float | None) -> str:
        return f"{value:,.0f} ms" if value is not None else "—"

    cols = st.columns(4)
    cols[0].metric("Median", ms(latency.median_ms))
    cols[1].metric("Average", ms(latency.average_ms))
    cols[2].metric("90th percentile", ms(latency.p90_ms))
    cols[3].metric("Slowest", ms(latency.max_ms))
    st.caption(f"{latency.definition} {latency.samples} exchanges.")


def render_trends(data: ChatAnalytics) -> None:
    st.subheader("Per day")
    if not any(
        p.chatbot_responses or p.tickets_created or p.conversations_started for p in data.daily
    ):
        st.info("No activity recorded in this period.")
        return
    days = [p.day.isoformat() for p in data.daily]
    st.line_chart(
        {
            "chatbot responses": dict(
                zip(days, (p.chatbot_responses for p in data.daily), strict=True)
            ),
            "answered": dict(zip(days, (p.answered for p in data.daily), strict=True)),
            "fallbacks": dict(zip(days, (p.fallbacks for p in data.daily), strict=True)),
        },
        y_label="responses",
    )
    st.bar_chart(
        {
            "conversations started": dict(
                zip(days, (p.conversations_started for p in data.daily), strict=True)
            ),
            "tickets created": dict(
                zip(days, (p.tickets_created for p in data.daily), strict=True)
            ),
        },
        stack=False,
    )


def render_ticket_analytics(data: TicketAnalytics) -> None:
    st.subheader("Tickets")
    cols = st.columns(4)
    cols[0].metric("Created", data.created)
    cols[1].metric("Resolved", data.resolved)
    cols[2].metric("Closed", data.closed)
    cols[3].metric("Created after a chat offer", data.created_after_chat_offer)
    hours = data.time_to_resolution
    if hours.samples:
        cols = st.columns(3)
        cols[0].metric("Median time to resolution", f"{hours.median:.1f} h")
        cols[1].metric("90th percentile", f"{hours.p90:.1f} h")
        cols[2].metric("Resolutions measured", hours.samples)
        st.caption(hours.definition)
    else:
        st.info("No tickets resolved in this period.")
    backlog = data.backlog
    cols = st.columns(3)
    cols[0].metric("Backlog now", backlog.total)
    cols[1].metric("Unassigned in backlog", backlog.unassigned)
    cols[2].metric("Oldest open since", when(backlog.oldest_created_at))
    if data.created:
        st.bar_chart({"tickets": data.created_by_current_status}, horizontal=True)
    with st.expander("Limitations of the ticket figures"):
        for limitation in data.limitations:
            st.markdown(f"- {limitation}")
