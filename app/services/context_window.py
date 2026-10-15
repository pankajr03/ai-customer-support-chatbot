"""Token budgeting for prompts that include conversation history.

Character limits and token limits are different things:

- **Character limits** (``*_CHARS`` settings) count characters of message text. They are
  exact, cheap and model-independent, and bound how much text is loaded and kept.
- **Token limits** (``CONVERSATION_CONTEXT_MAX_TOKENS``) bound what a model request may
  use: the model's context window is measured in tokens, and one token is anything from
  one character to a whole word depending on the language and the text.

How tokens are counted (``CONTEXT_TOKENIZER``):

- ``estimate`` (default): UTF-8 bytes / 3, rounded up. No dependency, no download.
  English averages about 4 characters (bytes) per token, so this usually *over*-counts,
  which keeps budgets safe; for scripts with multi-byte characters it approximates one
  token per character. It is an estimate, not a guarantee.
- ``tiktoken``: OpenAI's tokenizer with ``TIKTOKEN_ENCODING`` (``o200k_base`` for current
  GPT models): exact counts for those models. Its encoding file is downloaded and cached
  on first use; if it cannot be loaded, the estimate is used and a warning is logged.

``ContextWindow.fit`` reserves room first and gives history only what is left::

    max_context_tokens
      - instructions          (system prompt)
      - evidence              (FAQ evidence, when the prompt has any)
      - latest message        (the customer's new message)
      - reserved output       (the request's max_output_tokens)
      = budget for history    (most recent turns that fit; older ones are omitted)

If the fixed parts alone exceed the budget, no history is used and ``fits`` is False:
the caller must not send that prompt.
"""

import logging
import math
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Generic, Literal, Protocol, TypeVar

logger = logging.getLogger(__name__)

TokenizerKind = Literal["estimate", "tiktoken"]
ESTIMATE_BYTES_PER_TOKEN = 3
DEFAULT_TIKTOKEN_ENCODING = "o200k_base"

T = TypeVar("T")


class Tokenizer(Protocol):
    name: str

    def count(self, text: str) -> int: ...


class EstimatedTokenizer:
    """Model-independent estimate: UTF-8 bytes / 3, rounded up (see module doc)."""

    name = "estimate"

    def count(self, text: str) -> int:
        return math.ceil(len(text.encode("utf-8")) / ESTIMATE_BYTES_PER_TOKEN)


class TiktokenTokenizer:
    """Exact counts with a tiktoken encoding, loaded on first use.

    Falls back to ``EstimatedTokenizer`` (with one warning) if tiktoken is not installed
    or the encoding cannot be loaded, e.g. offline before its first download.
    """

    def __init__(
        self,
        encoding_name: str = DEFAULT_TIKTOKEN_ENCODING,
        *,
        loader: Callable[[str], Any] | None = None,
    ) -> None:
        self.encoding_name = encoding_name
        self._loader = loader or _load_tiktoken_encoding
        self._encoding: Any = None
        self._fallback: EstimatedTokenizer | None = None
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return "estimate" if self._fallback else f"tiktoken:{self.encoding_name}"

    def count(self, text: str) -> int:
        encoding = self._get_encoding()
        if encoding is None:
            return EstimatedTokenizer().count(text)
        # Special-token text such as "<|endoftext|>" is counted as ordinary text.
        return len(encoding.encode(text, disallowed_special=()))

    def _get_encoding(self) -> Any:
        if self._encoding is not None or self._fallback is not None:
            return self._encoding
        with self._lock:
            if self._encoding is None and self._fallback is None:
                try:
                    self._encoding = self._loader(self.encoding_name)
                except Exception as exc:  # noqa: BLE001 - any failure: estimate instead
                    logger.warning(
                        "tiktoken encoding %s unavailable (%s); using token estimates",
                        self.encoding_name,
                        type(exc).__name__,
                    )
                    self._fallback = EstimatedTokenizer()
        return self._encoding


def _load_tiktoken_encoding(name: str) -> Any:
    import tiktoken  # optional dependency, imported only when configured

    return tiktoken.get_encoding(name)


def build_tokenizer(
    kind: TokenizerKind, encoding_name: str = DEFAULT_TIKTOKEN_ENCODING
) -> Tokenizer:
    if kind == "tiktoken":
        return TiktokenTokenizer(encoding_name)
    return EstimatedTokenizer()


@dataclass(frozen=True)
class FittedContext(Generic[T]):  # noqa: UP046 - Python 3.11 compatible
    """The history items that fit, oldest first, and how the budget was used."""

    items: tuple[T, ...]
    fits: bool  # False: instructions + evidence + message + output exceed the limit
    omitted: int  # items left out (always the oldest)
    fixed_tokens: int  # instructions + evidence + message + reserved output
    history_tokens: int
    max_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.fixed_tokens + self.history_tokens


class ContextWindow:
    """Fits recent history into a token budget after reserving the fixed parts."""

    def __init__(self, max_tokens: int, tokenizer: Tokenizer | None = None) -> None:
        if max_tokens < 1:
            raise ValueError("max_tokens must be at least 1")
        self.max_tokens = max_tokens
        self.tokenizer = tokenizer or EstimatedTokenizer()

    def fit(
        self,
        history: Sequence[T],
        render: Callable[[T], str],
        *,
        instructions: str,
        message: str,
        reserved_output_tokens: int,
        evidence: str = "",
    ) -> FittedContext[T]:
        """The most recent history items whose rendered text fits the remaining budget.

        ``render`` must return an item exactly as it appears in the prompt (with its
        markup), so the count matches what is sent. Items are counted newest first and
        only until the budget is used up, never the whole history; the window has no
        gaps (an older item is never kept after a newer one was dropped).
        """
        count = self.tokenizer.count
        fixed = count(instructions) + count(evidence) + count(message) + reserved_output_tokens
        if fixed > self.max_tokens:
            return FittedContext((), False, len(history), fixed, 0, self.max_tokens)
        remaining = self.max_tokens - fixed
        kept: list[T] = []
        used = 0
        for item in reversed(history):
            cost = count(render(item))
            if used + cost > remaining:
                break
            kept.append(item)
            used += cost
        kept.reverse()
        return FittedContext(
            tuple(kept), True, len(history) - len(kept), fixed, used, self.max_tokens
        )
