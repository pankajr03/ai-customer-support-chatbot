"""A small, fixed FAQ dataset for tests.

Tests use this instead of data/faqs.json, so the production knowledge base can be
replaced with client content without breaking the test suite. The ``faq_file``
fixture writes these records to a temporary JSON file for each test.

Shape of the data (keep these facts in sync with the records below):
- 9 records, 7 active.
- Active categories: account, orders, payments, returns, shipping.
- faq_105 (shipping) is inactive; faq_109 is the only "products" FAQ and is inactive,
  so "products" is not an available category.
"""

from typing import Any

TEST_FAQ_RECORDS: list[dict[str, Any]] = [
    {
        "id": "faq_101",
        "question": "How can I track my order?",
        "answer": "Open My Orders and select the order to see its tracking details.",
        "category": "orders",
        "keywords": ["track", "order status", "where is my order"],
        "is_active": True,
    },
    {
        "id": "faq_102",
        "question": "Can I cancel my order?",
        "answer": "Orders can be cancelled from My Orders while they are still processing.",
        "category": "orders",
        "keywords": ["cancel", "change order"],
        "is_active": True,
    },
    {
        "id": "faq_103",
        "question": "How long does delivery take?",
        "answer": "Standard delivery usually takes 3-5 business days after dispatch.",
        "category": "shipping",
        "keywords": ["delivery time", "shipping time", "how long"],
        "is_active": True,
    },
    {
        "id": "faq_104",
        "question": "Do you ship internationally?",
        "answer": "We ship to selected countries. Import duties may apply on arrival.",
        "category": "shipping",
        "keywords": ["international", "customs", "abroad"],
        "is_active": True,
    },
    {
        "id": "faq_105",
        "question": "Is there free shipping during the holiday sale?",
        "answer": "During the holiday sale, standard shipping is free on all orders.",
        "category": "shipping",
        "keywords": ["holiday", "free shipping", "sale"],
        "is_active": False,
    },
    {
        "id": "faq_106",
        "question": "What is your return policy?",
        "answer": "Most unused items can be returned within 30 days of delivery.",
        "category": "returns",
        "keywords": ["return policy", "send back", "return window"],
        "is_active": True,
    },
    {
        "id": "faq_107",
        "question": "How do I reset my password?",
        "answer": "Select Forgot Password on the sign-in page and follow the emailed link.",
        "category": "account",
        "keywords": ["reset password", "forgot password", "login"],
        "is_active": True,
    },
    {
        "id": "faq_108",
        "question": "What payment methods do you accept?",
        "answer": "We accept major credit and debit cards and selected digital wallets.",
        "category": "payments",
        "keywords": ["payment methods", "credit card", "pay"],
        "is_active": True,
    },
    {
        "id": "faq_109",
        "question": "Do you sell gift hampers?",
        "answer": "Gift hampers are not available at the moment.",
        "category": "products",
        "keywords": ["hamper", "gift basket"],
        "is_active": False,
    },
]

ACTIVE_IDS = [r["id"] for r in TEST_FAQ_RECORDS if r["is_active"]]
INACTIVE_IDS = [r["id"] for r in TEST_FAQ_RECORDS if not r["is_active"]]
AVAILABLE_CATEGORIES = ["account", "orders", "payments", "returns", "shipping"]
ACTIVE_SHIPPING_IDS = ["faq_103", "faq_104"]
PUBLIC_FIELDS = {"id", "question", "answer", "category"}
