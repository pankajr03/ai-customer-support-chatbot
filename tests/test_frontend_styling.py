"""The frontend's look (Stage 8, Task 12): theme contrast, CSS rules, no unsafe HTML.

These are static checks of .streamlit/config.toml, frontend/styles.css and the frontend
code, so they stay fast and offline. The rendered page was also checked in a browser
at desktop and phone widths (see frontend/README.md, "Look and feel").
"""

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
THEME = tomllib.loads((ROOT / ".streamlit" / "config.toml").read_text(encoding="utf-8"))["theme"]
CSS = (ROOT / "frontend" / "styles.css").read_text(encoding="utf-8")
FRONTEND_CODE = {
    path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8")
    for path in (ROOT / "frontend").rglob("*.py")
}

AA = 4.5  # WCAG 2.x AA minimum for normal-size text
CAPTION_OPACITY = 0.6  # Streamlit draws captions in the text colour at 60% opacity
BADGE_TINT = 0.1  # badge backgrounds are their colour at 10% opacity


def rgb(color: str) -> tuple[float, float, float]:
    value = color.lstrip("#")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def mix(front: tuple[float, ...], back: tuple[float, ...], alpha: float) -> tuple[float, ...]:
    return tuple(alpha * f + (1 - alpha) * b for f, b in zip(front, back, strict=True))


def luminance(color: tuple[float, ...]) -> float:
    def channel(c: float) -> float:
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in color)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


WHITE = (255.0, 255.0, 255.0)


# --- Theme ----------------------------------------------------------------------------


def test_the_theme_is_a_light_neutral_palette() -> None:
    assert THEME["base"] == "light"
    assert THEME["backgroundColor"].lower() == "#ffffff"


@pytest.mark.parametrize("background", ["backgroundColor", "secondaryBackgroundColor"])
def test_text_and_captions_have_aa_contrast(background: str) -> None:
    text, back = rgb(THEME["textColor"]), rgb(THEME[background])
    assert contrast(text, back) >= AA
    assert contrast(mix(text, back, CAPTION_OPACITY), back) >= AA
    sidebar = rgb(THEME.get("sidebar", {}).get("backgroundColor", THEME[background]))
    assert contrast(mix(text, sidebar, CAPTION_OPACITY), sidebar) >= AA


@pytest.mark.parametrize("key", ["primaryColor", "linkColor"])
def test_accent_colour_has_aa_contrast_with_white(key: str) -> None:
    # Links on the white page, and white labels on primary buttons.
    assert contrast(rgb(THEME[key]), WHITE) >= AA


@pytest.mark.parametrize(
    ("text", "base"),
    [
        ("greenTextColor", "#21c354"),
        ("orangeTextColor", "#ff8700"),
        ("redTextColor", "#ff2b2b"),
        ("grayTextColor", "#31333f"),
    ],
)
def test_status_badges_have_aa_contrast(text: str, base: str) -> None:
    # Badges sit in the sidebar on their own colour at 10% opacity.
    page = rgb(THEME["sidebar"]["backgroundColor"])
    badge = mix(rgb(base), page, BADGE_TINT)
    assert contrast(rgb(THEME[text]), badge) >= AA


def test_links_are_underlined() -> None:
    assert THEME["linkUnderline"] is True  # not distinguished by colour alone


# --- CSS ------------------------------------------------------------------------------


def css_selectors(css: str) -> list[str]:
    without_comments = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    blocks = re.findall(r"([^{}]+)\{", without_comments)
    return [selector.strip() for block in blocks for selector in block.split(",")]


def test_css_uses_no_generated_class_names() -> None:
    selectors = css_selectors(CSS)
    assert selectors
    for selector in selectors:
        assert not re.search(r"(^|[\s>+~])\.[A-Za-z_-]", selector), selector
        assert "[data-testid=" in selector, selector


def test_css_sets_no_fixed_widths_and_loads_nothing() -> None:
    assert not re.search(r"(^|[\s;{])(min-|max-)?width\s*:\s*[\d.]+px", CSS)
    assert "@import" not in CSS and "url(" not in CSS
    assert "!important" not in CSS


def test_long_words_wrap_inside_messages() -> None:
    rule = re.search(r'\[data-testid="stChatMessageContent"\]\s*\{([^}]*)\}', CSS)
    assert rule is not None
    assert "overflow-wrap: anywhere" in rule.group(1) and "min-width: 0" in rule.group(1)


# --- No unsafe HTML -------------------------------------------------------------------


def test_no_customer_text_can_reach_html() -> None:
    for name, code in FRONTEND_CODE.items():
        assert "unsafe_allow_html" not in code, name
        assert "components.html" not in code and "unsafe_allow_javascript" not in code, name
        for call in re.findall(r"st\.html\(([^)]*)\)", code):
            assert call.strip() == "STYLES", f"{name}: st.html({call})"  # the static CSS only


def test_message_text_is_rendered_through_plain_markdown() -> None:
    chat = FRONTEND_CODE["frontend/components/chat.py"]
    assert "st.markdown(plain_markdown(message.content))" in chat
    assert "st.markdown(message.content)" not in chat


def test_choice_buttons_wrap_instead_of_cutting_off_the_question() -> None:
    # On a phone a clarification choice is longer than the screen is wide.
    selector = r'\[data-testid="stChatMessageContent"\] \[data-testid="stButton"\] button \*'
    rule = re.search(selector + r"\s*\{([^}]*)\}", CSS)
    assert rule is not None
    assert "white-space: normal" in rule.group(1) and "text-overflow: clip" in rule.group(1)
