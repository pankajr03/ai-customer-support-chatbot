"""Practical, conservative checks that a generated answer stays within its FAQ evidence.

What this is (and is not)
-------------------------
A small set of deterministic heuristics run on every generated answer before it is
shown. They catch the most damaging, most common kinds of drift in support answers:
changed numbers, invented fees or promises, and dropped restrictions. They are **not**
a fact checker: an answer can pass every check and still be wrong (a paraphrase that
reverses a condition in words, an invented step without numbers, ...). Source-ID
validation alone is not enough either; it only proves which FAQs were supplied.
Passing these checks means "no obvious problem found", never "verified".

When a check fails, or a claim cannot be verified, the caller returns the exact
approved FAQ answer instead: the conservative choice whenever reliable validation is
not possible.

Checks
------
1. ``unsupported_number``: every quantity in the answer (durations, amounts,
   percentages, times of day, bare numbers, number words before a unit such as
   "thirty days") must appear in the cited FAQs with the same unit. A changed refund
   period (30 -> 60 days), an invented fee ($5) or a repeated order number all fail.
2. ``unsupported_date``: weekdays, months and relative dates ("tomorrow", "today",
   "this week") must appear in the cited FAQs.
3. ``unsupported_term``: sensitive commitments must be backed by the cited FAQs:
   "free"/"no charge", fees and surcharges, guarantees and warranties, refunds,
   discounts and codes, receipts, compensation (Stage 12).
4. ``unsupported_promise``: delivery and service promises ("next-day", "same-day",
   "overnight", "instantly", "any time", "no questions asked", ...) must appear
   word for word in the cited FAQs.
5. ``missing_limit``: if the matched FAQ states a numeric limit (duration, amount or
   percentage), at least one of its limits must survive in the answer.
6. ``dropped_restrictions`` / ``dropped_negation``: if the matched FAQ contains
   conditions ("only", "unless", "if", "within", ...) or negations ("can't", "don't",
   "no longer", ...), the answer must keep at least one of each kind. This catches
   "Items can't be added" turning into "Items can be added".

7. ``unsupported_action``: the assistant cannot do anything (look up, change, cancel,
   refund, send ...), so an answer must not say that an action was, is being or will be
   taken: "I've cancelled your order", "your refund has been processed", "I'll send you
   a label". Such a phrase is allowed only if it appears in the cited FAQs, e.g. an FAQ
   saying "We'll email you a link" supports "we will email you a link".

Added in Stage 12 (security audit), against content a manipulated model could append
to an otherwise faithful answer:

8. ``prompt_disclosure``: text copied from protected instructions (any run of
   ``PROTECTED_WINDOW`` words from ``protected``, e.g. the system prompt), or talk about
   instructions, prompts or "developer mode".
9. ``secret_disclosure``: anything shaped like a credential (API keys, access tokens,
   long hex strings) or naming one ("API key", "access token", "bearer").
10. ``unsupported_authority``: claims to be staff or an administrator, to have access to
    the customer's records, or about the status of the customer's own account, order,
    refund or ticket ("your account is in good standing").
11. ``unsupported_payment``: payment methods (Bitcoin, PayPal, cash on delivery, ...).
12. ``unsupported_code``: code-like tokens mixing capitals and digits ("SAVE20").
13. ``unsupported_link``: web addresses and email addresses (a phishing link, an
    invented contact address).

Each phrase of checks 8-13 is allowed when the cited FAQs contain it.

Checks 1-4 and 7-13 compare against all cited FAQs (question and answer text); checks
5-6 compare against the matched (primary) FAQ only.

Known limitations
-----------------
- Numbers are matched by value and unit, not meaning: "standard delivery takes 2 days"
  passes if the FAQ says "express takes 1-2 days".
- Wording is not understood: a condition reversed without numbers, negations or the
  listed words, or an invented step or channel ("reset it in our app"), passes.
- The checks err on the side of caution: dropping a merely descriptive "not" ("not what
  you ordered") is flagged too. Such false positives are harmless, because the customer
  then gets the approved answer verbatim.
- Checks 8-13 are phrase and pattern lists. A paraphrased leak ("my guidance says to
  rely on the store's articles"), a discount code in plain letters without the word
  "code" ("use SAVEBIG"), or an invented policy in neutral words ("you can return it to
  any shop") passes. They narrow what a manipulated model can show; they cannot prove an
  answer is safe.
- English only, like the rest of the pipeline.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum


class IssueKind(StrEnum):
    UNSUPPORTED_NUMBER = "unsupported_number"
    UNSUPPORTED_DATE = "unsupported_date"
    UNSUPPORTED_TERM = "unsupported_term"
    UNSUPPORTED_PROMISE = "unsupported_promise"
    MISSING_LIMIT = "missing_limit"
    DROPPED_RESTRICTIONS = "dropped_restrictions"
    DROPPED_NEGATION = "dropped_negation"
    UNSUPPORTED_ACTION = "unsupported_action"
    PROMPT_DISCLOSURE = "prompt_disclosure"
    SECRET_DISCLOSURE = "secret_disclosure"  # noqa: S105 - an issue name, not a secret
    UNSUPPORTED_AUTHORITY = "unsupported_authority"
    UNSUPPORTED_PAYMENT = "unsupported_payment"
    UNSUPPORTED_CODE = "unsupported_code"
    UNSUPPORTED_LINK = "unsupported_link"


@dataclass(frozen=True, slots=True)
class GroundingIssue:
    kind: IssueKind
    detail: str  # what was found, e.g. "60 day" (internal diagnostics only)


@dataclass(frozen=True, slots=True)
class GroundingReport:
    issues: tuple[GroundingIssue, ...]

    @property
    def passed(self) -> bool:
        """True when no obvious problem was found. Not proof of correctness."""
        return not self.issues

    @property
    def kinds(self) -> tuple[str, ...]:
        """Distinct issue kinds, in order: safe to log (no values)."""
        return tuple(dict.fromkeys(issue.kind.value for issue in self.issues))


# --- Vocabulary -------------------------------------------------------------------------------

_NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fourteen": 14,
    "fifteen": 15, "twenty": 20, "twenty-four": 24, "thirty": 30, "forty": 40,
    "forty-five": 45, "fifty": 50, "sixty": 60, "ninety": 90, "hundred": 100,
}  # fmt: skip
_UNITS = {
    "minute": "minute", "minutes": "minute", "min": "minute", "mins": "minute",
    "hour": "hour", "hours": "hour", "hr": "hour", "hrs": "hour",
    "day": "day", "days": "day", "week": "week", "weeks": "week",
    "month": "month", "months": "month", "year": "year", "years": "year",
    "percent": "percent", "per cent": "percent", "%": "percent",
    "dollar": "money", "dollars": "money", "usd": "money", "eur": "money", "euro": "money",
    "euros": "money", "gbp": "money", "pound": "money", "pounds": "money",
    "am": "time", "pm": "time", "a.m.": "time", "p.m.": "time",
}  # fmt: skip
_DATE_WORDS = (
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "weekend", "january", "february", "march", "april", "june", "july", "august",
    "september", "october", "november", "december", "today", "tonight", "tomorrow",
    "yesterday", "this week", "next week", "this month", "next month",
)  # fmt: skip
# Each group counts as supported if ANY of its phrases appears in the evidence.
_TERM_GROUPS = {
    "free": ("free", "free of charge", "no charge", "no cost", "at no cost", "complimentary"),
    "fee": ("fee", "fees", "surcharge", "surcharges", "restocking"),
    "guarantee": ("guarantee", "guaranteed", "guarantees", "warranty", "warranties"),
    "refund": (
        "refund", "refunds", "refunded", "refundable", "money back", "reimburse",
        "reimbursed", "reimbursement",
    ),
    "discount": (
        "discount", "discounts", "coupon", "coupons", "voucher", "vouchers", "promo",
        "promotion", "promotional", "code", "codes",
    ),
    "receipt": ("receipt", "receipts", "proof of purchase"),
    "compensation": ("compensation", "compensate", "goodwill", "store credit", "credit note"),
}  # fmt: skip
# Each phrase must appear itself in the evidence.
_PROMISES = (
    "next-day", "next day", "same-day", "same day", "overnight", "instantly", "instant",
    "immediately", "right away", "any time", "anytime", "at any time", "no questions asked",
    "unlimited", "regardless", "promise", "promised", "definitely", "always",
)  # fmt: skip
_RESTRICTIONS = (
    "only", "unless", "except", "as long as", "provided", "if", "must", "within",
    "before", "after", "up to", "at least", "at most", "maximum", "minimum", "until",
    "once", "while", "depending", "depend", "depends", "may", "subject to",
)  # fmt: skip
_NEGATIONS = ("not", "n't", "cannot", "never", "no longer", "no ")

# Stage 12: content that only a manipulated or confused model would add.
PROTECTED_WINDOW = 8  # words copied in a row from protected text count as a disclosure
_DISCLOSURE = (
    "system prompt", "system message", "my instructions", "my prompt", "the prompt",
    "previous instructions", "prior instructions", "above instructions",
    "these instructions", "ignore all", "ignore your", "ignore the", "ignore previous",
    "disregard", "developer mode", "jailbreak", "i was instructed", "i am instructed",
    "i have been instructed", "i was told to", "i am programmed", "my programming",
    "my rules", "hidden rules", "faq_reference_data", "customer_question", "role=",
)  # fmt: skip
_SECRET_WORDS = (
    "api key", "api-key", "apikey", "access token", "secret key", "private key",
    "bearer", "password is", "credentials",
)  # fmt: skip
_SECRET_PATTERN = re.compile(
    r"(?<![a-z0-9])(?:sk-[a-z0-9_-]{8,}|(?:cst|stf)_[a-z0-9_-]{8,}|[0-9a-f]{32,})(?![a-z0-9])"
)
_AUTHORITY = (
    "administrator", "admin", "admin mode", "staff member", "store manager", "manager",
    "supervisor", "store owner", "i am authorized", "i am authorised", "authorized to",
    "authorised to", "i have authority", "i have access", "i can see your",
    "i can access", "in our system", "on our records", "on file", "good standing",
)  # fmt: skip
# A status for this customer's own records, which the assistant cannot see.
_STATUS_CLAIM = re.compile(
    r"(?<![a-z])your (?:account|order|refund|ticket|payment|case|request|subscription|"
    r"parcel|package|return) (?:is|has been|was) (?:now |already |currently |still )?"
    r"(?:verified|approved|active|eligible|on its way|out for delivery|delivered|on hold|"
    r"suspended|blocked|under review|pending|paid|in good standing|valid|accepted)(?![a-z])"
)
# Terse status with no verb, as in a headline: "Refund approved.", "Order cancelled".
_TERSE_STATUS = re.compile(
    r"(?<![a-z])(?:refund|order|return|cancellation|request|ticket|payment|exchange|"
    r"replacement|claim) (?:approved|processed|confirmed|cancell?ed|canceled|completed|"
    r"issued|resolved|refunded|accepted|granted|shipped|dispatched)(?![a-z])"
)
_PAYMENT_METHODS = (
    "bitcoin", "crypto", "cryptocurrency", "ethereum", "paypal", "klarna", "afterpay",
    "apple pay", "google pay", "cash on delivery", "cash", "bank transfer",
    "wire transfer", "cheque", "money order", "gift card", "gift cards", "visa",
    "mastercard", "american express", "amex", "credit card", "debit card", "venmo",
    "western union",
)  # fmt: skip
# "SAVE20", "WELCOME10": capitals and digits together, as discount codes are written.
_CODE_TOKEN = re.compile(
    r"(?<![A-Za-z0-9])(?=[A-Z0-9]*[0-9])(?=[A-Z0-9]*[A-Z])[A-Z0-9]{4,}(?![A-Za-z0-9])"
)
# Web and email addresses: "https://...", "www.example.com", "shop-refunds.net", "a@b.com".
_LINK = re.compile(
    r"https?://[^\s<>\"')]+|www\.[^\s<>\"')]+"
    r"|[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}"
    r"|(?<![a-z0-9@.-])[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|net|org|io|co|uk|de|shop|store|"
    r"info|biz|xyz|app|example)(?![a-z0-9])"
)
_LIMIT_UNITS = frozenset({"minute", "hour", "day", "week", "month", "year", "money", "percent"})

# Actions a support assistant might falsely claim. Past participles go with "have" and
# passives ("has been refunded"); base forms with "will"; -ing forms with "am"/"are".
_DONE = (
    "processed", "cancelled", "canceled", "refunded", "issued", "initiated", "started",
    "submitted", "scheduled", "rescheduled", "arranged", "updated", "changed", "reset",
    "checked", "looked up", "looked into", "tracked", "escalated", "forwarded", "sent",
    "emailed", "credited", "approved", "booked", "placed", "reviewed", "verified",
    "confirmed", "located", "found", "contacted", "notified", "opened", "created",
    "logged", "reported", "flagged", "completed", "fixed", "resolved", "unlocked",
    "removed", "added", "applied", "waived", "reversed", "shipped", "dispatched",
    "replaced", "restored", "taken care of", "handled", "ordered", "deleted", "closed",
)  # fmt: skip
_TO_DO = (
    "process", "cancel", "refund", "issue", "initiate", "start", "submit", "schedule",
    "reschedule", "arrange", "update", "change", "reset", "check", "look up", "look into",
    "track", "escalate", "forward", "send", "email", "credit", "approve", "book", "place",
    "review", "verify", "confirm", "contact", "notify", "open", "create", "log", "report",
    "flag", "complete", "fix", "resolve", "unlock", "remove", "add", "apply", "waive",
    "reverse", "ship", "dispatch", "replace", "restore", "take care of", "handle",
    "delete", "close",
)  # fmt: skip
_DOING = (
    "processing", "cancelling", "canceling", "refunding", "issuing", "initiating",
    "submitting", "scheduling", "arranging", "updating", "changing", "resetting",
    "checking", "looking up", "looking into", "tracking", "escalating", "forwarding",
    "sending", "emailing", "crediting", "approving", "booking", "reviewing", "verifying",
    "confirming", "contacting", "notifying", "fixing", "resolving", "handling",
    "replacing", "restoring", "shipping", "dispatching",
)  # fmt: skip
_ACTION_OBJECTS = (
    "order", "orders", "refund", "refunds", "return", "returns", "request", "requests",
    "account", "payment", "payments", "cancellation", "exchange", "ticket", "case",
    "password", "item", "items", "package", "parcel", "subscription", "address",
    "delivery", "shipment", "replacement", "label", "claim", "complaint", "it", "this",
    "that",
)  # fmt: skip
_CONTRACTIONS = (
    ("i've", "i have"), ("we've", "we have"), ("i'll", "i will"), ("we'll", "we will"),
    ("i'm", "i am"), ("we're", "we are"), ("i'd", "i would"), ("we'd", "we would"),
    ("it's", "it is"),
)  # fmt: skip


def _alternatives(phrases: Iterable[str]) -> str:
    return "|".join(sorted((re.escape(p) for p in phrases), key=len, reverse=True))


_ADVERBS = (
    r"(?:(?:just|now|already|also|successfully|personally|gone ahead and|went ahead and)\s+)*"
)
_ACTION_CLAIMS = re.compile(
    # "I have cancelled", "we've just refunded", "I went ahead and booked"
    rf"(?<![a-z])(?:i|we)\s+(?:have\s+|had\s+)?{_ADVERBS}(?:{_alternatives(_DONE)})(?![a-z])"
    # "I will send", "we'll process", "I can now refund", "I'm going to check"
    rf"|(?<![a-z])(?:i|we)\s+(?:will|would|can|shall|am going to|are going to)\s+{_ADVERBS}"
    rf"(?:{_alternatives(_TO_DO)})(?![a-z])"
    # "I am processing", "we are looking into"
    rf"|(?<![a-z])(?:i|we)\s+(?:am|are)\s+{_ADVERBS}(?:{_alternatives(_DOING)})(?![a-z])"
    # "your refund has been processed", "the order is now cancelled", "it was shipped"
    rf"|(?<![a-z])(?:(?:your|the)\s+)?(?:{_alternatives(_ACTION_OBJECTS)})\s+"
    rf"(?:has|have|is|are|was|were)\s+{_ADVERBS}(?:been\s+)?{_ADVERBS}"
    rf"(?:{_alternatives(_DONE)})(?![a-z])"
)

_DASHES = str.maketrans({"–": "-", "—": "-", "−": "-", "’": "'"})
_NUMBER = r"\d+(?:[.,]\d+)?"
_WORD_NUMBER = "|".join(sorted((re.escape(w) for w in _NUMBER_WORDS), key=len, reverse=True))
_UNIT = "|".join(sorted((re.escape(u) for u in _UNITS), key=len, reverse=True))
# Whole numbers and whole words only: "ten" never matches inside "often".
_VALUE = rf"(?<![a-z0-9])(?:{_NUMBER}|{_WORD_NUMBER})(?![a-z])"
# "$50", "3-5 business days", "3 to 5 days", "two weeks", "10%", "2 pm".
_QUANTITY = re.compile(
    rf"(?P<currency>[$€£])?\s*(?P<first>{_VALUE})"
    rf"(?:\s*(?:-|to|and|or)\s*(?P<second>{_VALUE}))?"
    rf"(?:\s*(?:business|working|calendar)?\s*(?P<unit>{_UNIT})(?![a-z]))?",
)


@dataclass(frozen=True, slots=True)
class _Quantity:
    value: float
    unit: str | None  # None = no unit (a bare number)


def check_grounding(
    answer: str, evidence: Iterable[str], primary: str, *, protected: Iterable[str] = ()
) -> GroundingReport:
    """Check a generated ``answer`` against the cited FAQ texts.

    Args:
        answer: The generated customer-facing answer.
        evidence: Question and answer texts of every FAQ the reply cites.
        primary: The approved answer of the FAQ Stage 4 matched.
        protected: Texts that must never be repeated to a customer (the system prompt).
    """
    evidence = list(evidence)
    answer_text = _normalise(answer)
    evidence_text = _normalise(" \n ".join(evidence))
    primary_text = _normalise(primary)
    issues: list[GroundingIssue] = []

    supported = _quantities(evidence_text)
    supported_values = {q.value for q in supported}
    for quantity in sorted(_quantities(answer_text), key=lambda q: (q.value, q.unit or "")):
        if quantity.unit is None:
            ok = quantity.value in supported_values
        else:
            ok = quantity in supported
        if not ok:
            issues.append(GroundingIssue(IssueKind.UNSUPPORTED_NUMBER, _describe(quantity)))

    for word in _DATE_WORDS:
        if _has(answer_text, word) and not _has(evidence_text, word):
            issues.append(GroundingIssue(IssueKind.UNSUPPORTED_DATE, word))
    for group, phrases in _TERM_GROUPS.items():
        if any(_has(answer_text, p) for p in phrases) and not any(
            _has(evidence_text, p) for p in phrases
        ):
            issues.append(GroundingIssue(IssueKind.UNSUPPORTED_TERM, group))
    for phrase in _PROMISES:
        if _has(answer_text, phrase) and not _has(evidence_text, phrase):
            issues.append(GroundingIssue(IssueKind.UNSUPPORTED_PROMISE, phrase))

    limits = {q for q in _quantities(primary_text) if q.unit in _LIMIT_UNITS}
    if limits and not limits & _quantities(answer_text):
        issues.append(GroundingIssue(IssueKind.MISSING_LIMIT, f"{len(limits)} limit(s)"))
    if _any(primary_text, _RESTRICTIONS) and not _any(answer_text, _RESTRICTIONS):
        issues.append(GroundingIssue(IssueKind.DROPPED_RESTRICTIONS, "conditions"))
    if _any(primary_text, _NEGATIONS) and not _any(answer_text, _NEGATIONS):
        issues.append(GroundingIssue(IssueKind.DROPPED_NEGATION, "negation"))

    claims_evidence = _expand_contractions(evidence_text)
    for claim in _action_claims(_expand_contractions(answer_text)):
        if claim not in claims_evidence:
            issues.append(GroundingIssue(IssueKind.UNSUPPORTED_ACTION, claim))

    issues.extend(_manipulation_issues(answer, answer_text, evidence_text, protected))
    return GroundingReport(tuple(issues))


def _manipulation_issues(
    answer: str, answer_text: str, evidence_text: str, protected: Iterable[str]
) -> list[GroundingIssue]:
    """Checks 8-12: disclosures, secrets, authority claims, payments and codes."""
    issues = []
    copied = _windows(answer_text) & _protected_windows(protected, evidence_text)
    if copied:
        issues.append(GroundingIssue(IssueKind.PROMPT_DISCLOSURE, f"{len(copied)} copied run(s)"))
    for kind, phrases in (
        (IssueKind.PROMPT_DISCLOSURE, _DISCLOSURE),
        (IssueKind.SECRET_DISCLOSURE, _SECRET_WORDS),
        (IssueKind.UNSUPPORTED_AUTHORITY, _AUTHORITY),
        (IssueKind.UNSUPPORTED_PAYMENT, _PAYMENT_METHODS),
    ):
        for phrase in phrases:
            if _has(answer_text, phrase) and not _has(evidence_text, phrase):
                issues.append(GroundingIssue(kind, phrase))
    for pattern in (_STATUS_CLAIM, _TERSE_STATUS):
        for match in pattern.finditer(answer_text):
            if match.group(0) not in evidence_text:
                issues.append(GroundingIssue(IssueKind.UNSUPPORTED_AUTHORITY, match.group(0)))
    for match in _SECRET_PATTERN.finditer(answer_text):
        if match.group(0) not in evidence_text:
            issues.append(GroundingIssue(IssueKind.SECRET_DISCLOSURE, "credential-like value"))
    for match in _CODE_TOKEN.finditer(answer.translate(_DASHES)):
        if match.group(0).casefold() not in evidence_text:
            issues.append(GroundingIssue(IssueKind.UNSUPPORTED_CODE, "code-like token"))
    for match in _LINK.finditer(answer_text):
        if match.group(0).rstrip(".,;:!?") not in evidence_text:
            issues.append(GroundingIssue(IssueKind.UNSUPPORTED_LINK, "address"))
    return issues


def _windows(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9']+", text)
    return {
        " ".join(words[i : i + PROTECTED_WINDOW]) for i in range(len(words) - PROTECTED_WINDOW + 1)
    }


def _protected_windows(protected: Iterable[str], evidence_text: str) -> set[str]:
    """Runs of protected text that the evidence does not also contain."""
    allowed = _windows(evidence_text)
    return {w for text in protected for w in _windows(_normalise(text))} - allowed


# --- Helpers ---------------------------------------------------------------------------------


def _normalise(text: str) -> str:
    return " ".join(text.translate(_DASHES).casefold().split())


def _has(text: str, phrase: str) -> bool:
    if phrase.endswith(" "):  # "no " = the word "no" followed by another word
        return re.search(rf"(?<![a-z]){re.escape(phrase)}", text) is not None
    if phrase.startswith("n'"):  # contraction suffix: "can't", "don't"
        return phrase in text
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text) is not None


def _expand_contractions(text: str) -> str:
    for short, long in _CONTRACTIONS:
        text = re.sub(rf"(?<![a-z]){re.escape(short)}(?![a-z])", long, text)
    return text


def _action_claims(text: str) -> list[str]:
    """Distinct claimed actions, in order of appearance."""
    return list(dict.fromkeys(" ".join(m.group(0).split()) for m in _ACTION_CLAIMS.finditer(text)))


def _any(text: str, phrases: Iterable[str]) -> bool:
    return any(_has(text, phrase) for phrase in phrases)


def _value(token: str) -> float:
    if token in _NUMBER_WORDS:
        return float(_NUMBER_WORDS[token])
    return float(token.replace(",", ""))


def _quantities(text: str) -> set[_Quantity]:
    found: set[_Quantity] = set()
    for match in _QUANTITY.finditer(text):
        first, second = match.group("first"), match.group("second")
        unit = "money" if match.group("currency") else _UNITS.get(match.group("unit") or "")
        is_word = first in _NUMBER_WORDS
        if is_word and unit is None:
            continue  # "one" alone is usually a pronoun, not a quantity
        for token in (first, second):
            if token is not None:
                found.add(_Quantity(_value(token), unit))  # unit None = a bare number
    return found


def _describe(quantity: _Quantity) -> str:
    value = f"{quantity.value:g}"
    return f"{value} {quantity.unit}" if quantity.unit else value
