"""Every exception this package can raise, and the two places it is allowed to.

The SDK sits in the customer's own request path, so it is almost entirely non-raising: `verify`
returns an `unavailable` assessment where another library would raise, and `observe` never raises at
all. The exceptions here exist for the two moments where silence would be worse than a failure:

* `HajerConfigError` — a setting the developer wrote is not a number / not a boolean. Raised while
  the client is being constructed, never during a call. A *missing* key is not a configuration
  error: it makes the client inert on purpose, so a customer's test suite runs unchanged.
* `UnsupportedClientError` — `wrap()` was handed an object with no surface it recognises. Returning
  it uninstrumented would mean silently capturing nothing for the rest of the process.
* `AssessmentUnavailableError` — opt-in only (`Hajer(raise_on_unavailable=True)`), for a caller who
  would rather see a stack trace than an `unavailable` status.
* `BodyOverBoundError` — internal; the client turns it into `unavailable{BODY_OVER_BOUND}`. It
  never reaches application code through `verify` or `observe`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hajer._models import Assessment


class HajerError(Exception):
    """Base class: one `except hajer.HajerError` catches everything this package raises."""


class HajerConfigError(HajerError):
    """A setting is invalid; never retain or print its possibly secret supplied value."""

    code = "HAJER_INVALID_CONFIGURATION"

    def __init__(self, variable: str, value: str, expected: str) -> None:
        # Keep the constructor and attribute for callers, but not their original content.
        del value
        super().__init__(f"{self.code}: {variable} is not {expected}; supplied value withheld")
        self.variable = variable
        self.value = "[withheld]"
        self.expected = expected


class UnsupportedClientError(HajerError):
    """`wrap()` found no OpenAI or Anthropic surface on the object it was handed."""

    def __init__(self, client: object) -> None:
        super().__init__(
            f"hajer.wrap() does not recognise {type(client).__module__}.{type(client).__qualname__}: "
            "it instruments openai.OpenAI / AsyncOpenAI (chat.completions, responses) and "
            "anthropic.Anthropic / AsyncAnthropic (messages)"
        )
        self.client = client


class BodyOverBoundError(HajerError):
    """The serialized body is larger than `HAJER_BODY_MAX_BYTES`; nothing was sent."""

    def __init__(self, size: int, limit: int) -> None:
        super().__init__(f"body is {size} bytes, over the {limit}-byte bound; nothing was sent")
        self.size = size
        self.limit = limit


class AssessmentUnavailableError(HajerError):
    """Raised instead of returning `unavailable`, only when the caller asked for it."""

    def __init__(self, assessment: Assessment) -> None:
        super().__init__(f"assessment unavailable: {assessment.reason}")
        self.assessment = assessment
