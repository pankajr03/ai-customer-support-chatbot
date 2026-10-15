"""Configurable model pricing for usage cost estimates.

No prices are built in: OpenAI's prices change, and a wrong hardcoded price is worse than
none. Configure the rates you pay with ``LLM_PRICING`` (see ``.env.example``); for models
without a configured price, the estimated cost is ``None``.
"""

from pydantic import BaseModel, ConfigDict, Field


class ModelPricing(BaseModel):
    """Prices for one model, per one million tokens, in ``LLM_PRICING_CURRENCY``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_per_million_tokens: float = Field(ge=0, allow_inf_nan=False)
    output_per_million_tokens: float = Field(ge=0, allow_inf_nan=False)
    # Optional discounted rate for cached input tokens; the input rate is used if unset.
    cached_input_per_million_tokens: float | None = Field(default=None, ge=0, allow_inf_nan=False)
