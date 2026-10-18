"""FAQ management section of the staff dashboard (Stage 11, Task 11).

Listing with search, category and status filters and pagination; a create form; an edit
form per FAQ; activate/deactivate (deactivation asks for confirmation); the revision
history. Every operation is a call to the protected admin API through
``AdminApiClient``; nothing here touches the database.

Safety rules:

- Edits send the version they were based on. If someone changed the FAQ meanwhile the
  backend refuses (409), and the page says so, keeps the text the user typed visible for
  copying, and offers to load the latest version: nothing is silently overwritten.
- Success is shown only after the backend returned the saved FAQ; the list then reloads.
- Duplicate submissions: each form's key changes after a confirmed save (a fresh form),
  and the backend refuses a second FAQ with the same question (409).
- Inputs are checked on the page first (required fields, lengths, tags) and the backend
  validates again; its field messages are shown, never submitted values echoed back.
"""

from typing import Any

import streamlit as st
from pydantic import SecretStr

from frontend.admin_api_client import (
    FAQ_CATEGORIES,
    MAX_ANSWER_LENGTH,
    MAX_KEYWORD_LENGTH,
    MAX_KEYWORDS,
    MAX_QUESTION_LENGTH,
    AdminApiClient,
    AdminErrorKind,
    FAQRecord,
    parse_keywords,
)
from frontend.admin_common import (
    SELECTED_FAQ_KEY,
    SESSION_PREFIX,
    call,
    error_message,
    flash,
    forget,
    form_generation,
    next_form_generation,
    opened,
    show_error,
    show_flash,
    try_call,
    when,
)

PAGE_SIZE = 20
CONFIRM_KEY = f"{SESSION_PREFIX}confirm_deactivate"
CONFLICT_KEY = f"{SESSION_PREFIX}faq_conflict"
OPENED_KEY = f"{SESSION_PREFIX}opened_faq"
STATUS_FILTERS = {"All": None, "Active": True, "Inactive": False}


def render_faqs(client: AdminApiClient, token: SecretStr) -> None:
    st.header("FAQ management")
    st.caption(
        "Active FAQs are what the assistant answers with; changes apply to the next "
        "customer question. Deactivated FAQs are kept and can be activated again."
    )
    show_flash()
    browse, create = st.tabs(["Browse and edit", "Add an FAQ"])
    with create:
        render_create_form(client, token)
    with browse:
        render_browser(client, token)


# --- Listing --------------------------------------------------------------------------


def render_browser(client: AdminApiClient, token: SecretStr) -> None:
    search_col, category_col, status_col, page_col = st.columns([4, 2, 2, 1])
    search = search_col.text_input(
        "Search", placeholder="Words in the question, answer or tags, or faq_001"
    )
    category = category_col.selectbox("Category", ["All", *FAQ_CATEGORIES])
    status = status_col.selectbox("Status", list(STATUS_FILTERS))
    page = int(page_col.number_input("Page", min_value=1, value=1, step=1))
    listing = call(
        lambda: client.list_faqs(
            token,
            search=search.strip() or None,
            category=None if category == "All" else category,
            is_active=STATUS_FILTERS[status],
            offset=(page - 1) * PAGE_SIZE,
            limit=PAGE_SIZE,
        )
    )
    if listing is None:
        return
    if not listing.faqs:
        st.info("No FAQs match these filters." if listing.total or page == 1 else "No more pages.")
        return
    last = min(listing.offset + len(listing.faqs), listing.total)
    st.caption(f"FAQs {listing.offset + 1}-{last} of {listing.total}.")
    st.dataframe(
        [
            {
                "FAQ ID": faq.id,
                "Question": faq.question,
                "Category": faq.category,
                "Status": "Active" if faq.is_active else "Inactive",
                "Version": faq.version,
                "Last updated": when(faq.updated_at),
            }
            for faq in listing.faqs
        ],
        hide_index=True,
        width="stretch",
    )
    ids = [faq.id for faq in listing.faqs]
    selected = st.selectbox("Open FAQ", ids, key=SELECTED_FAQ_KEY)
    if selected:
        faq = opened(OPENED_KEY, selected, lambda: client.get_faq(token, selected))
        if faq is not None:
            render_editor(client, token, faq)


# --- Create ---------------------------------------------------------------------------


def check_fields(question: str, answer: str, keywords: list[str]) -> list[str]:
    """The page's own checks (the backend checks again)."""
    problems = []
    if not question.strip():
        problems.append("Enter a question.")
    if not answer.strip():
        problems.append("Enter an answer.")
    if not keywords:
        problems.append("Enter at least one tag.")
    if len(keywords) > MAX_KEYWORDS:
        problems.append(f"Use at most {MAX_KEYWORDS} tags.")
    if any(len(k) > MAX_KEYWORD_LENGTH for k in keywords):
        problems.append(f"Each tag can have at most {MAX_KEYWORD_LENGTH} characters.")
    return problems


def render_create_form(client: AdminApiClient, token: SecretStr) -> None:
    generation = form_generation("new_faq")
    key = f"new-faq-{generation}"
    with st.form(key):
        question = st.text_input("Question", max_chars=MAX_QUESTION_LENGTH, key=f"{key}-q")
        answer = st.text_area("Answer", max_chars=MAX_ANSWER_LENGTH, height=160, key=f"{key}-a")
        category = st.selectbox("Category", FAQ_CATEGORIES, key=f"{key}-c")
        tags = st.text_input(
            "Tags (comma-separated)", placeholder="track, order status", key=f"{key}-t"
        )
        active = st.checkbox(
            "Active: customers can be answered with it right away", value=True, key=f"{key}-on"
        )
        submitted = st.form_submit_button("Create FAQ", type="primary")
    if not submitted:
        return
    keywords = parse_keywords(tags)
    if problems := check_fields(question, answer, keywords):
        for problem in problems:
            st.error(problem)
        return
    created, error = try_call(
        lambda: client.create_faq(
            token,
            question=question,
            answer=answer,
            category=category,
            keywords=keywords,
            is_active=active,
        )
    )
    if error is not None:
        show_error(error)
        return
    next_form_generation("new_faq")  # a fresh form: a second click submits nothing
    flash(f"Created {created.id} ({'active' if created.is_active else 'inactive'}).")
    st.rerun()


# --- Edit -----------------------------------------------------------------------------


def faq_changes(
    faq: FAQRecord, question: str, answer: str, category: str, keywords: list[str]
) -> dict[str, Any]:
    """Only the fields that differ from the loaded version."""
    changes: dict[str, Any] = {}
    if " ".join(question.split()) != faq.question:
        changes["question"] = question
    if answer.strip() != faq.answer:
        changes["answer"] = answer
    if category != faq.category:
        changes["category"] = category
    if list(dict.fromkeys(k.lower() for k in keywords)) != list(faq.keywords):
        changes["keywords"] = keywords
    return changes


def render_editor(client: AdminApiClient, token: SecretStr, faq: FAQRecord) -> None:
    status = "active" if faq.is_active else "inactive"
    st.subheader(f"{faq.id} · {status} · version {faq.version}")
    st.caption(
        f"Created {when(faq.created_at)} by {faq.created_by or 'JSON import'}; last changed "
        f"{when(faq.updated_at)} by {faq.updated_by or 'JSON import'}."
    )
    render_conflict(faq)
    if st.button("Reload latest version", key=f"{SESSION_PREFIX}reload_faq"):
        forget(OPENED_KEY)
        st.rerun()
    # The key includes the version: a reloaded FAQ shows its new content.
    with st.form(f"faq-{faq.id}-{faq.version}"):
        question = st.text_input("Question", faq.question, max_chars=MAX_QUESTION_LENGTH)
        answer = st.text_area("Answer", faq.answer, max_chars=MAX_ANSWER_LENGTH, height=200)
        category = st.selectbox(
            "Category", FAQ_CATEGORIES, index=FAQ_CATEGORIES.index(faq.category)
        )
        tags = st.text_input("Tags (comma-separated)", ", ".join(faq.keywords))
        saved = st.form_submit_button("Save changes", type="primary")
    if saved:
        save_edit(client, token, faq, question, answer, category, parse_keywords(tags))
    render_activation(client, token, faq)
    with st.expander("History"):
        render_history(client, token, faq.id)


def save_edit(
    client: AdminApiClient,
    token: SecretStr,
    faq: FAQRecord,
    question: str,
    answer: str,
    category: str,
    keywords: list[str],
) -> None:
    if problems := check_fields(question, answer, keywords):
        for problem in problems:
            st.error(problem)
        return
    changes = faq_changes(faq, question, answer, category, keywords)
    if not changes:
        st.info("Nothing changed, so nothing was saved.")
        return
    updated, error = try_call(lambda: client.update_faq(token, faq.id, faq.version, **changes))
    if error is None:
        st.session_state.pop(CONFLICT_KEY, None)
        forget(OPENED_KEY)  # show the saved version
        flash(f"Saved {updated.id}: now version {updated.version}.")
        st.rerun()
    if error.kind is AdminErrorKind.CONFLICT and error.code == "FAQ_VERSION_CONFLICT":
        # Keep what the user typed, to copy into the latest version.
        st.session_state[CONFLICT_KEY] = {"faq_id": faq.id, "version": faq.version, **changes}
        forget(OPENED_KEY)  # load the version someone else saved
        st.rerun()
    show_error(error)


def render_conflict(faq: FAQRecord) -> None:
    conflict = st.session_state.get(CONFLICT_KEY)
    if not conflict or conflict.get("faq_id") != faq.id:
        return
    st.warning(
        f"Not saved: someone else changed {faq.id} after you opened version "
        f"{conflict['version']}. The form now shows the latest version "
        f"({faq.version}); your changes are below to copy back if they still apply."
    )
    with st.expander("Your unsaved changes", expanded=True):
        for field, value in conflict.items():
            if field not in ("faq_id", "version"):
                st.text_area(field, str(value), disabled=True, key=f"{SESSION_PREFIX}c_{field}")
    if st.button("Dismiss", key=f"{SESSION_PREFIX}dismiss_conflict"):
        st.session_state.pop(CONFLICT_KEY, None)
        st.rerun()


def render_activation(client: AdminApiClient, token: SecretStr, faq: FAQRecord) -> None:
    if not faq.is_active:
        if st.button(f"Activate {faq.id}", key=f"activate-{faq.id}-{faq.version}"):
            set_active(client, token, faq, active=True)
        return
    pending = st.session_state.get(CONFIRM_KEY)
    if pending != (faq.id, faq.version):
        if st.button(f"Deactivate {faq.id}…", key=f"deactivate-{faq.id}-{faq.version}"):
            st.session_state[CONFIRM_KEY] = (faq.id, faq.version)
            st.rerun()
        return
    st.warning(
        f"Deactivate {faq.id}? Customers will no longer be answered with it, starting with "
        "the next question. It stays stored and can be activated again."
    )
    yes, no = st.columns(2)
    if yes.button("Yes, deactivate", type="primary", key=f"confirm-{faq.id}"):
        st.session_state.pop(CONFIRM_KEY, None)
        set_active(client, token, faq, active=False)
    if no.button("Cancel", key=f"cancel-{faq.id}"):
        st.session_state.pop(CONFIRM_KEY, None)
        st.rerun()


def set_active(client: AdminApiClient, token: SecretStr, faq: FAQRecord, *, active: bool) -> None:
    changed, error = try_call(
        lambda: client.set_faq_active(token, faq.id, faq.version, active=active)
    )
    if error is not None:
        st.error(error_message(error))
        return
    forget(OPENED_KEY)
    flash(f"{changed.id} is now {'active' if changed.is_active else 'inactive'}.")
    st.rerun()


def render_history(client: AdminApiClient, token: SecretStr, faq_id: str) -> None:
    history = call(lambda: client.faq_revisions(token, faq_id))
    if history is None:
        return
    if not history.revisions:
        st.caption("No recorded changes (imported before history was kept).")
        return
    for revision in history.revisions:
        who = revision.changed_by or "JSON import"
        st.markdown(
            f"**Version {revision.version}** · {revision.action} by {who} · "
            f"{when(revision.changed_at)}"
        )
        for field in revision.changed_fields:
            after = revision.new_values.get(field, "—")
            if revision.previous_values is None:
                st.caption(f"{field}: {after}")
            else:
                st.caption(f"{field}: {revision.previous_values.get(field, '—')} → {after}")
    if history.has_more:
        st.caption(f"Showing the latest {len(history.revisions)} of {history.total} changes.")
