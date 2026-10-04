"""Every exception this package can raise, and the few places it is allowed to.

The SDK sits in the customer's own request path, so it is almost entirely non-raising: a span that cannot
be started, exported or flushed costs the span and never the code it was about. The exceptions here exist
for the moments where silence would be worse than a failure:

* `HajerConfigError` — a setting the developer wrote is not a number / not a boolean. Raised where the
  settings are read, never inside a model call. A *missing* key is not a configuration error: it makes
  the SDK inert on purpose, so a customer's test suite runs unchanged.
* `UnsupportedClientError` — `wrap()` was handed an object with no surface it recognises. Returning
  it uninstrumented would mean silently capturing nothing for the rest of the process.
* `BodyOverBoundError` — internal; the eval upload turns it into a `BODY_OVER_BOUND` receipt. It never
  reaches application code.
* `EvalMetadataError` — `hajer eval` only. Raised inside the eval engine's `beforeAll` hook process
  when a test's `metadata.hajer` is malformed, so the run stops before a single provider call and the
  terminal names the test. That process is the engine's, never the customer's application.
"""

from __future__ import annotations


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


class EvalMetadataError(HajerError):
    """A test's reserved `metadata.hajer` is malformed; the eval did not run. Raised only in the engine's hook process."""

    code = "HAJER_INVALID_EVAL_METADATA"

    def __init__(self, errors: tuple[str, ...]) -> None:
        super().__init__(f"{self.code}: " + "; ".join(errors))
        self.errors = errors
