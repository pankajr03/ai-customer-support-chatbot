"""Grounding validation: practical checks for unsupported claims in generated answers.

FAQ texts are fixed here (modelled on the sample knowledge base) so the tests do not
depend on data/faqs.json.
"""

import pytest

from app.services.grounding_validator import IssueKind, check_grounding

RETURN_POLICY = (
    "You can return most items within 30 days of delivery, as long as they are unused, in "
    "their original condition and in their original packaging. Some items, such as "
    "personalised products, gift cards and opened hygiene products, can't be returned "
    "unless they are faulty."
)
REFUND_TIME = (
    "Once your return arrives at our warehouse, we usually inspect it within 3 business days "
    "and then issue your refund. It can take a further 5-10 business days for the money to "
    "appear in your account, depending on your bank or payment provider."
)
DELIVERY_TIME = (
    "Standard delivery usually takes 3-5 business days after dispatch. Express delivery "
    "usually takes 1-2 business days. Orders are normally dispatched within 1 business day. "
    "Delivery times are estimates and may be longer during busy periods or for remote "
    "addresses."
)
NEXT_DAY = (
    "Express delivery usually arrives within 1-2 business days and is available for most "
    "addresses. Order before 2 pm on a business day for the best chance of next-day "
    "arrival. Available options and cut-off times are shown at checkout."
)
SHIPPING_COST = (
    "Shipping costs depend on the delivery method and your address, and are shown at "
    "checkout before you pay. Standard shipping is free on orders over $50. Express "
    "shipping is available for an additional fee."
)
RETURN_SHIPPING = (
    "Returns are free if the item is faulty, damaged or not what you ordered. For other "
    "returns, a return shipping fee is deducted from your refund. The fee is shown before "
    "you confirm your return."
)
ADD_ITEM = (
    "Items can't be added to an existing order. If your order is still Processing, you can "
    "cancel it and place a new order with all the items you need."
)
RESET_STEPS = "Select Forgot Password on the sign-in page and follow the emailed link."


def kinds(answer: str, primary: str, *supporting: str) -> set[IssueKind]:
    report = check_grounding(answer, [primary, *supporting], primary)
    return {issue.kind for issue in report.issues}


# --- Faithful paraphrases pass ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("primary", "answer"),
    [
        (
            RETURN_POLICY,
            "You can return most unused items within thirty days of delivery in their original "
            "condition and packaging. Personalised products, gift cards and opened hygiene "
            "products can't be returned unless faulty.",
        ),
        (
            DELIVERY_TIME,
            "Standard delivery usually takes 3 to 5 business days after dispatch, and express "
            "delivery 1–2 business days. These are estimates and may be longer when busy.",
        ),
        (
            NEXT_DAY,
            "Express delivery usually arrives within 1-2 business days. Order before 2 pm on a "
            "business day for the best chance of next-day arrival.",
        ),
        (
            SHIPPING_COST,
            "Standard shipping is free on orders over $50. Other costs depend on the method and "
            "address and are shown at checkout before you pay.",
        ),
        (
            ADD_ITEM,
            "Unfortunately items can't be added to an existing order. If it is still "
            "Processing, you can cancel it and place a new order.",
        ),
        (
            RESET_STEPS,
            "Choose Forgot Password on the sign-in page, then follow the link we email you.",
        ),
    ],
)
def test_faithful_paraphrases_pass(primary: str, answer: str) -> None:
    report = check_grounding(answer, [primary], primary)

    assert report.passed, report.issues
    assert report.kinds == ()


# --- Unsupported refund periods ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("primary", "answer"),
    [
        (RETURN_POLICY, "You can return unused items within 60 days of delivery unless faulty."),
        (RETURN_POLICY, "You can return unused items within sixty days unless they are faulty."),
        (RETURN_POLICY, "Unused items can be returned within 30 weeks unless they're faulty."),
        (REFUND_TIME, "Your refund usually reaches your account within 24 hours of inspection."),
        (REFUND_TIME, "Refunds usually take 2-3 business days, depending on your bank."),
    ],
)
def test_changed_refund_periods_are_rejected(primary: str, answer: str) -> None:
    assert IssueKind.UNSUPPORTED_NUMBER in kinds(answer, primary)


def test_dropping_the_refund_period_is_rejected() -> None:
    assert IssueKind.MISSING_LIMIT in kinds(
        "You can return most unused items, unless faulty.", RETURN_POLICY
    )


# --- Delivery promises ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (
            "Your order will arrive tomorrow if dispatched within 1 business day.",
            IssueKind.UNSUPPORTED_DATE,
        ),
        ("It should arrive by Friday, within 3-5 business days.", IssueKind.UNSUPPORTED_DATE),
        ("Delivery is guaranteed within 3-5 business days.", IssueKind.UNSUPPORTED_TERM),
        (
            "We offer same-day delivery, usually within 1-2 business days.",
            IssueKind.UNSUPPORTED_PROMISE,
        ),
        ("Standard delivery always takes 3-5 business days.", IssueKind.UNSUPPORTED_PROMISE),
        (
            "Standard delivery usually takes 7 business days after dispatch.",
            IssueKind.UNSUPPORTED_NUMBER,
        ),
        (
            "Delivery is overnight for most addresses within 1 business day.",
            IssueKind.UNSUPPORTED_PROMISE,
        ),
    ],
)
def test_delivery_promises_beyond_the_faq_are_rejected(answer: str, expected: IssueKind) -> None:
    assert expected in kinds(answer, DELIVERY_TIME)


def test_next_day_is_allowed_when_the_faq_says_it() -> None:
    assert (
        kinds(
            "Order before 2 pm for the best chance of next-day arrival within 1-2 business days.",
            NEXT_DAY,
        )
        == set()
    )


def test_an_invented_cut_off_time_is_rejected() -> None:
    assert IssueKind.UNSUPPORTED_NUMBER in kinds(
        "Order before 5 pm for the best chance of next-day arrival within 1-2 business days.",
        NEXT_DAY,
    )


# --- Fabricated fees and charges --------------------------------------------------------------


@pytest.mark.parametrize(
    ("primary", "answer", "expected"),
    [
        (
            RETURN_POLICY,
            "Returns within 30 days carry a $5 restocking fee unless faulty.",
            IssueKind.UNSUPPORTED_NUMBER,
        ),
        (
            RETURN_POLICY,
            "Returns within 30 days carry a small restocking fee unless faulty.",
            IssueKind.UNSUPPORTED_TERM,
        ),
        (
            RETURN_POLICY,
            "Returns within 30 days are free of charge unless they are personalised.",
            IssueKind.UNSUPPORTED_TERM,
        ),
        (
            SHIPPING_COST,
            "Standard shipping is free on orders over $30, shown before you pay.",
            IssueKind.UNSUPPORTED_NUMBER,
        ),
        (
            SHIPPING_COST,
            "Express shipping costs $9.99 more, shown at checkout before you pay.",
            IssueKind.UNSUPPORTED_NUMBER,
        ),
        (
            REFUND_TIME,
            "We refund 100% of the price within 3 business days, depending on your bank.",
            IssueKind.UNSUPPORTED_NUMBER,
        ),
    ],
)
def test_fabricated_fees_and_amounts_are_rejected(
    primary: str, answer: str, expected: IssueKind
) -> None:
    assert expected in kinds(answer, primary)


def test_fees_mentioned_by_the_faq_may_be_repeated() -> None:
    answer = (
        "Returns are free if the item is faulty, damaged or not what you ordered. Otherwise a "
        "return shipping fee is deducted from your refund, and it's shown before you confirm."
    )

    assert kinds(answer, RETURN_SHIPPING) == set()


# --- Dropped restrictions and contradictions ---------------------------------------------------


def test_dropping_the_conditions_of_a_free_return_is_rejected() -> None:
    assert IssueKind.DROPPED_RESTRICTIONS in kinds("Returns are free.", RETURN_SHIPPING)


@pytest.mark.parametrize(
    "answer",
    [
        "Items can be added to an existing order while it is processing.",
        "Yes, you can add items to your order whenever you like.",
    ],
)
def test_reversed_restrictions_are_rejected(answer: str) -> None:
    assert IssueKind.DROPPED_NEGATION in kinds(answer, ADD_ITEM)


def test_dropping_exceptions_from_the_return_policy_is_rejected() -> None:
    found = kinds("All items can be returned within 30 days.", RETURN_POLICY)

    # "within" counts as a kept condition, so only the negation check fires here.
    assert IssueKind.DROPPED_NEGATION in found


# --- Other unsupported specifics ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "detail"),
    [
        ("Your order 4711 can't be changed, but if it is Processing you can cancel it.", "4711"),
        (
            "If your order is Processing, cancel it before December and place a new one; "
            "items can't be added.",
            "december",
        ),
        ("Items can't be added. If it's Processing, you can cancel it at any time.", "any time"),
    ],
)
def test_other_unsupported_specifics_are_rejected(answer: str, detail: str) -> None:
    report = check_grounding(answer, [ADD_ITEM], ADD_ITEM)

    assert not report.passed
    assert any(detail in issue.detail for issue in report.issues)


def test_numbers_from_supporting_faqs_count_as_supported() -> None:
    answer = (
        "Items can't be added to an existing order. If it is still Processing, cancel it and "
        "place a new order; standard shipping is free on orders over $50."
    )

    assert kinds(answer, ADD_ITEM, SHIPPING_COST) == set()
    assert IssueKind.UNSUPPORTED_NUMBER in kinds(answer, ADD_ITEM)  # not supported alone


# --- The report ---------------------------------------------------------------------------------


def test_report_lists_each_issue_and_distinct_kinds_in_order() -> None:
    report = check_grounding(
        "Returns within 60 days or 90 days are free, guaranteed.", [RETURN_POLICY], RETURN_POLICY
    )

    details = [(issue.kind.value, issue.detail) for issue in report.issues]
    assert ("unsupported_number", "60 day") in details
    assert ("unsupported_number", "90 day") in details
    assert report.kinds[0] == "unsupported_number"
    assert len(report.kinds) == len(set(report.kinds))
    assert report.passed is False


def test_one_as_a_pronoun_is_not_a_number() -> None:
    assert (
        kinds(
            "Choose the one that says Forgot Password on the sign-in page and follow the "
            "emailed link.",
            RESET_STEPS,
        )
        == set()
    )


def test_words_containing_number_words_are_not_numbers() -> None:
    assert (
        kinds(
            "Often the emailed link is the fastest: select Forgot Password on the sign-in page.",
            RESET_STEPS,
        )
        == set()
    )


# --- Known limitations (documented, not bugs) --------------------------------------------------


def test_limitation_numbers_are_matched_by_value_and_unit_not_by_meaning() -> None:
    """ "2 business days" passes because the FAQ says "1-2 business days" (for express).

    The checks cannot tell which product or condition a number belongs to.
    """
    assert (
        kinds(
            "Standard delivery usually takes 2 business days, but may take longer.", DELIVERY_TIME
        )
        == set()
    )


def test_limitation_descriptive_negations_make_the_check_conservative() -> None:
    """Leaving out a descriptive "not" ("not what you ordered") is flagged too.

    Such false positives are harmless: the customer gets the approved answer verbatim.
    """
    assert IssueKind.DROPPED_NEGATION in kinds(
        "Returns are free if the item is faulty or damaged; otherwise a return shipping fee "
        "is deducted from your refund.",
        RETURN_SHIPPING,
    )


# --- Claims that an action was taken -----------------------------------------------------------

WE_EMAIL = "Select Forgot Password and we'll email you a link to set a new password."


@pytest.mark.parametrize(
    ("answer", "claim"),
    [
        ("I've cancelled your order. You can place a new one any time.", "i have cancelled"),
        ("Good news: your refund has been processed.", "your refund has been processed"),
        ("I have just issued your refund within 30 days.", "i have just issued"),
        ("I'll send you a return label by email.", "i will send"),
        ("We will refund your order once it arrives.", "we will refund"),
        ("I'm checking your order status now.", "i am checking"),
        ("We are looking into your delivery.", "we are looking into"),
        ("The item was shipped this morning.", "the item was shipped"),
        ("I went ahead and booked a courier pickup.", "i went ahead and booked"),
        ("Your order is now cancelled.", "your order is now cancelled"),
    ],
)
def test_claims_that_an_action_was_taken_are_rejected(answer: str, claim: str) -> None:
    report = check_grounding(answer, [ADD_ITEM], ADD_ITEM)

    assert (IssueKind.UNSUPPORTED_ACTION, claim) in {(i.kind, i.detail) for i in report.issues}


@pytest.mark.parametrize(
    "answer",
    [
        "Select Forgot Password and we will email you a link to set a new password.",
        "Choose Forgot Password; we'll email you a link to set a new password.",
        "You can cancel your order from Orders and place a new one.",  # the customer acts
        "Once you send the item back, we inspect it.",
    ],
)
def test_actions_stated_by_the_faq_or_taken_by_the_customer_pass(answer: str) -> None:
    assert IssueKind.UNSUPPORTED_ACTION not in kinds(answer, WE_EMAIL, ADD_ITEM)


def test_a_claim_is_only_supported_by_the_same_phrase_in_the_faq() -> None:
    faq = "Once your return has been processed, the refund takes 5-10 business days."

    assert IssueKind.UNSUPPORTED_ACTION not in kinds(
        "Once your return has been processed, the refund takes 5-10 business days.", faq
    )
    assert IssueKind.UNSUPPORTED_ACTION in kinds(
        "Your refund has been processed and takes 5-10 business days.", faq
    )
