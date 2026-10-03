"""Customer-CI dated text prices, including the exact projection of Hajer's own routes.

No aliases are guessed. Unknown models cannot send a paid CI request. These are list-price
estimates, not a provider invoice; the receipt preserves their source and date.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Price:
    input: int
    output: int
    write_5m: int
    write_1h: int
    read: int
    context: int
    source: str
    fetched: str


@dataclass(frozen=True)
class OutputLimit:
    tokens: int
    source: str
    fetched: str


# Optional request caps can be omitted only when a published model maximum bounds the reserve.
# This evidence is separate from the backend price projection; no aliases or default limits are guessed.
OUTPUT_LIMITS = {
    ("openai", "gpt-5.1"): OutputLimit(128_000, "https://developers.openai.com/api/docs/models/gpt-5.1", "2026-09-29"),
    ("openai", "gpt-4o-mini"): OutputLimit(
        16_384, "https://developers.openai.com/api/docs/models/gpt-4o-mini", "2026-09-30"
    ),
    ("openai", "gpt-4o-mini-2024-07-18"): OutputLimit(
        16_384, "https://developers.openai.com/api/docs/models/gpt-4o-mini", "2026-09-30"
    ),
}


ANTHROPIC_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing"
BACKEND_PRICES = {
    ("anthropic", "claude-haiku-4-5-20251001"): Price(
        1_000_000, 5_000_000, 1_250_000, 2_000_000, 100_000, 200_000, ANTHROPIC_SOURCE, "2026-09-26"
    ),
    ("anthropic", "claude-sonnet-5"): Price(
        2_000_000, 10_000_000, 2_500_000, 4_000_000, 200_000, 1_000_000, ANTHROPIC_SOURCE, "2026-09-15"
    ),
    ("anthropic", "claude-opus-5-5"): Price(
        4_000_000, 20_000_000, 5_000_000, 8_000_000, 200_000, 1_000_000, ANTHROPIC_SOURCE, "2026-09-23"
    ),
    ("openai", "gpt-5.1"): Price(
        1_250_000, 10_000_000, 0, 0, 125_000, 400_000, "https://developers.openai.com/api/docs/pricing", "2026-09-15"
    ),
}

# Customer application models do not add authoring routes or change Hajer's own budget catalog.
PRICES = {
    **BACKEND_PRICES,
    ("openai", "gpt-4o-mini"): Price(
        150_000,
        600_000,
        0,
        0,
        75_000,
        128_000,
        "https://developers.openai.com/api/docs/models/gpt-4o-mini",
        "2026-09-30",
    ),
    ("openai", "gpt-4o-mini-2024-07-18"): Price(
        150_000,
        600_000,
        0,
        0,
        75_000,
        128_000,
        "https://developers.openai.com/api/docs/models/gpt-4o-mini",
        "2026-09-30",
    ),
}
