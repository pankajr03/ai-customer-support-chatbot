"""Source references under a reply, and clarification choices."""

from collections.abc import Callable, Sequence

import streamlit as st

from frontend.api_client import SourceReference
from frontend.formatting import plain_markdown


def render_sources(sources: Sequence[SourceReference]) -> None:
    """The approved FAQs an answer is based on, collapsed by default."""
    if not sources:
        return
    label = "Source" if len(sources) == 1 else f"Sources ({len(sources)})"
    with st.expander(label, icon=":material/menu_book:"):
        for source in sources:
            st.markdown(f"- {plain_markdown(source.question)}")
            st.caption(f"Help article {source.faq_id}")


def render_choices(
    sources: Sequence[SourceReference],
    *,
    key_prefix: str,
    on_choose: Callable[[str], None],
    disabled: bool = False,
) -> None:
    """The questions a clarifying reply offers, each a button that asks it."""
    with st.container(horizontal=True, gap="small"):
        for index, source in enumerate(sources):
            st.button(
                plain_markdown(source.question).rstrip(),  # labels support Markdown
                key=f"{key_prefix}-choice-{index}",
                on_click=on_choose,
                args=(source.question,),
                disabled=disabled,
                icon=":material/arrow_forward:",
            )
