"""Source references under a reply, and clarification choices.

Only what the backend returned is shown, in its order and without duplicates: the FAQ
question titles (and an FAQ ID only when two titles are the same). Nothing about how
the FAQs were retrieved or scored is shown, and the wording does not claim that a
source guarantees the reply is correct.
"""

from collections.abc import Callable, Sequence

import streamlit as st

from frontend.api_client import SourceReference
from frontend.formatting import (
    SOURCES_LABEL,
    SOURCES_NOTE,
    plain_markdown,
    source_labels,
    unique_sources,
)


def render_sources(sources: Sequence[SourceReference]) -> None:
    """The approved FAQs a reply is based on, collapsed. Omitted when there are none."""
    labels = source_labels(sources)
    if not labels:
        return
    with st.expander(SOURCES_LABEL, icon=":material/menu_book:"):
        st.caption(SOURCES_NOTE)
        lines = [
            f"- {plain_markdown(question).rstrip()}"
            + (f" \\({plain_markdown(faq_id).rstrip()}\\)" if faq_id else "")
            for question, faq_id in labels
        ]
        st.markdown("\n".join(lines))


def render_choices(
    sources: Sequence[SourceReference],
    *,
    key_prefix: str,
    on_choose: Callable[[str], None],
) -> None:
    """The questions a clarifying reply offers, each a button that asks it."""
    with st.container(horizontal=True, gap="small"):
        for index, source in enumerate(unique_sources(sources)):
            st.button(
                plain_markdown(source.question).rstrip(),  # labels support Markdown
                key=f"{key_prefix}-choice-{index}",
                on_click=on_choose,
                args=(source.question,),
                icon=":material/arrow_forward:",
            )
