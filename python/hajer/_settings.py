"""`HajerSettings` — every bound the SDK obeys, each one a field with an environment variable.

Nothing in this package spells a limit as a literal anywhere but here: nothing
is hard-coded. Each default is explained in `README.md` under *Settings*, with what would make
you change it. `from_env` is the **only** place in the SDK that reads the process environment; the
ruff ban on `os.environ` / `os.getenv` (`pyproject.toml`) keeps it that way.

**Inert mode is the important behaviour here.** Without `HAJER_API_KEY` and `HAJER_TEAM_ID` — or with
`HAJER_DISABLED=1` — the client is inert: `verify` returns `unavailable{reason: DISABLED}` and
`observe` is a no-op receipt. Neither raises, neither opens a socket. That is what lets the pull
request that installs the SDK land in a repository whose test suite has no Hajer credentials and still pass
unchanged, and it is why a missing key is not a `HajerConfigError`.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from hajer._errors import HajerConfigError

#: The public base URL of the hosted service. Overridden with `HAJER_BASE_URL` for a self-hosted
#: deployment, a staging environment, or a local backend (`http://localhost:8000`).
DEFAULT_BASE_URL: Final[str] = "https://api.hajer.ai"

#: The four budgets the client-side redaction pass runs under (`_redact.py`). They are constants here
#: rather than in that module because this file is the one place the SDK spells a limit, and they are
#: *constants* rather than settings fields because they bound a pass that must not be able to spend an
#: unbounded amount of a customer's request inside a customer's request path: a team that could raise them
#: from the environment could turn a 1 MB document into a 100 MB one and blame the SDK for the latency.
#: A `HAJER_*` variable for each is the decision to make when somebody has a document that needs one.
#:
#: 1 MiB, because that is the largest provider response body the wire already admits
#: (`WrappedCallCaptureIn.response_body`), so a document larger than this could not be sent anyway.
REDACT_MAX_BYTES: Final[int] = 1_048_576
#: 50,000 values. A tuple with more nodes than this is not the bounded shape the inline lane supports, and
#: an unbounded node count is a scan cost nobody can reason about before the call.
REDACT_MAX_NODES: Final[int] = 50_000
#: 64 levels. Well above the ingest depth bound the service applies to evidence (6) and well below
#: CPython's own recursion limit, which this walk does not use anyway.
REDACT_MAX_DEPTH: Final[int] = 64
#: 65,536 characters of one value — the same number the service's own `MAX_SCAN_BYTES` uses, for the same
#: reason: past it the scan costs more than the value is worth and the honest answer is "not scanned".
REDACT_MAX_STRING_CHARS: Final[int] = 65_536

#: What one environment tag may be (`HAJER_ENVIRONMENT`): lower-case letters, digits and dashes, a letter or digit
#: first, at most 64 characters — the backend's own pattern for `VerifyIn.environment`, so a tag this process sends is
#: one the service stores. A value outside it is dropped rather than sent: the service would refuse the whole flush.
ENVIRONMENT_PATTERN: Final[str] = r"^[a-z0-9][a-z0-9-]{0,63}$"
#: The environment every replay `verify` is tagged with (`hajer.replay`): CI traffic, never a test input.
CI_ENVIRONMENT: Final[str] = "ci"
#: What `doctor` prints for a `HAJER_ENVIRONMENT` that was set and dropped.
ENVIRONMENT_IGNORED: Final[str] = "ignored: invalid"

_TRUE = frozenset({"1", "true", "t", "yes", "y", "on"})
_FALSE = frozenset({"0", "false", "f", "no", "n", "off"})


class HajerSettings(BaseModel):
    """Everything the SDK reads from configuration, validated once, then frozen."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: CI suite runs (`hajer.pytest_plugin`). How often a live case repeats is the qualification rule,
    #: shared with the platform (`pytest_plugin/_qualification.py`), not a setting.
    ci_case_timeout_seconds: int = Field(default=60, gt=0)
    #: Optional fixed measurement cap; it never enables extra attempts by itself.
    ci_fixed_repeat_limit: int = Field(default=20, gt=0)
    #: A zero ceiling (the default) admits no paid customer-CI request. One ledger is shared by every case/repeat.
    ci_budget_microusd: int = Field(default=0, ge=0)
    ci_budget_file: str | None = None
    ci_input_deadline_ms: int = Field(default=30_000, gt=0)
    ci_input_max_bytes: int = Field(default=1_048_576, gt=0)
    #: `python -m hajer verify-adapters`: how many fake model answers each model endpoint serves one adapter's call
    #: (a workflow that calls the model in a loop gets that many before a further call is refused).
    adapter_check_answers: int = Field(default=16, gt=0)
    ci_commit_sha: str | None = None
    #: The branch a CI run tested: a pull request's head branch, else the pushed branch. Only runs on the
    #: repository's default branch feed a check's variance-floor baseline on Hajer's side.
    ci_branch: str | None = None

    # ── identity ────────────────────────────────────────────────────────────────────────────────
    #: `HAJER_API_KEY` — a team API key. Absent → inert.
    api_key: str | None = None
    #: `HAJER_TEAM_ID` — the team every route is scoped to. Absent → inert.
    team_id: str | None = None
    #: `HAJER_BASE_URL`
    base_url: str = DEFAULT_BASE_URL
    #: `HAJER_ENVIRONMENT` — the environment this process runs in (`production`, `staging`, `dev`), sent on every
    #: observation. The team chooses which environments' recorded calls may become test inputs, so a process that
    #: names none is never one. Folded to lower case; a value that is still not `ENVIRONMENT_PATTERN` is dropped —
    #: never raised, because a tag is a hint and must not cost the call it rides on — and `doctor` says so.
    environment: str | None = None

    # ── verify ──────────────────────────────────────────────────────────────────────────────────
    #: `HAJER_DEADLINE_MS_DEFAULT` — the deadline a `verify` call uses when it names none. It covers
    #: the whole SDK operation: serialisation, network, and the server's own work.
    deadline_ms_default: int = Field(default=1_500, gt=0)

    # ── observe ─────────────────────────────────────────────────────────────────────────────────
    #: `HAJER_OBSERVE_QUEUE_MAX` — observations held in memory before the newest is refused.
    observe_queue_max: int = Field(default=1_000, gt=0)
    #: `HAJER_OBSERVE_FLUSH_INTERVAL_MS` — how long a partial batch waits before it is sent anyway.
    observe_flush_interval_ms: int = Field(default=2_000, gt=0)
    #: `HAJER_OBSERVE_BATCH_MAX` — observations in one flush request.
    observe_batch_max: int = Field(default=32, gt=0)
    #: `HAJER_OBSERVE_FLUSH_DEADLINE_MS` — the deadline of one flush request.
    observe_flush_deadline_ms: int = Field(default=5_000, gt=0)
    #: `HAJER_OBSERVE_BACKOFF_INITIAL_MS` — the first wait after a failed flush.
    observe_backoff_initial_ms: int = Field(default=200, gt=0)
    #: `HAJER_OBSERVE_BACKOFF_MAX_MS` — the ceiling that doubling backoff never exceeds.
    observe_backoff_max_ms: int = Field(default=30_000, gt=0)
    #: `HAJER_OBSERVE_SINK` — a directory every settled wrapped call is written to instead of sent.
    #: Absent (the default) means no sink. Set, it opens no socket and reads no credential: it is how a
    #: suite that patches its transports session-wide still exports what its own workflows asked for
    #: (`hajer/_observe_sink.py`).
    observe_sink: str | None = None

    # ── payload ─────────────────────────────────────────────────────────────────────────────────
    #: `HAJER_BODY_MAX_BYTES` — a body larger than this is refused locally, before the socket.
    body_max_bytes: int = Field(default=65_536, gt=0)
    #: `HAJER_CAPTURE_CONTENT` — on by default. With it off, `wrap` records no message text, no tool
    #: arguments and no tool results: shapes, names, counts, timing and usage only.
    capture_content: bool = True
    #: `HAJER_CAPTURE_RAW` — ship the provider's own request and response document for each wrapped
    #: call, so the service reads the call itself into a model-call receipt rather than taking this
    #: SDK's summary of it. Strictly more disclosing than `capture_content`, so it *requires* it: raw
    #: bytes contain every message, argument and result, and a flag that quietly widened disclosure
    #: would be the dishonest part. Refused at construction when `capture_content` is off.
    capture_raw: bool = False
    #: `HAJER_CAPTURE_CALL_SITE` — **on by default.** A wrapped call's summary carries where it was made
    #: (up to `MAX_CALLER_FRAMES` application frames: module, qualified name, file, line, and for a file
    #: under the project root the SHA-256 digest of its bytes — never its text — while it is unchanged since
    #: the process started) and the `host:port` of the client's endpoint. A frame's file is only ever relative to the project root
    #: (`HAJER_PROJECT_ROOT`, else the working directory); a file outside it is sent as
    #: `<outside>/<basename>`, and a home directory (`/home/<user>`, `/Users/<user>`,
    #: `C:\\Users\\<user>`) is never part of what is sent, whatever the root. These are locations and a
    #: host name, never values, which is why they need no content consent: the service uses them to link
    #: the call to the workflow that made it. `HAJER_CAPTURE_CALL_SITE=0` sends neither. A raw capture (`HAJER_CAPTURE_RAW`) carries these
    #: same frames under the same rule, and none with this off.
    capture_call_site: bool = True
    #: `HAJER_CAPTURE_HTTP` — off by default. With it on, `wrap`, `attach` and `instrument` also patch
    #: httpx's own default transports, so every outbound HTTP request the application makes — a search API,
    #: a data vendor — is recorded as a call (`provider="http"`, the method, scheme and host, the path as a
    #: template with ids, tokens and catalog matches replaced, the status and where it was made; never a
    #: header, the query string or a body). A request a wrapped model call makes is not recorded again, and
    #: the SDK's own never.
    capture_http: bool = False
    #: `HAJER_PROJECT_ROOT` — the directory call-site frames are made relative to. Unset means the working
    #: directory; a process started from `/` (a systemd unit, say) sends every frame as `<outside>` until
    #: this names the application's checkout.
    project_root: str | None = None
    #: `HAJER_WRAPPED_CALL_MAX_BYTES` — what one raw capture may carry: the request document plus the
    #: response document, and what one summary's content may carry. Past it the longest texts are clipped
    #: in the middle — head and tail kept, their full length and SHA-256 marked between them — and the
    #: capture says it was clipped; only a document whose shape alone does not fit ships as a summary.
    #: Well below `HAJER_BODY_MAX_BYTES` on purpose: several captures and the tuple itself have to fit
    #: inside one body.
    wrapped_call_max_bytes: int = Field(default=32_768, gt=0)
    #: `HAJER_WRAPPED_CALLS_MAX` — wrapped calls one task accumulates before further ones are
    #: counted and dropped rather than kept.
    wrapped_calls_max: int = Field(default=32, gt=0)
    #: `HAJER_BOUNDARY_BODY_MAX_BYTES` — bytes of one recorded non-model response body kept verbatim
    #: inside `hajer.record_boundaries()`; the digest and size always cover the whole body.
    boundary_body_max_bytes: int = Field(default=16_384, gt=0)
    #: `HAJER_BOUNDARY_RESPONSES_MAX` — recorded non-model responses one submission carries; later ones
    #: are counted as `dropped`, never silently lost.
    boundary_responses_max: int = Field(default=32, gt=0)
    #: `HAJER_RECORD_REPLY_READS` — record which fields of the model's answer the application reads,
    #: by handing the caller a delegating view of the reply (`_reads.py`). **Off by default and
    #: deliberately so**: a view is not the provider's own object, so an `isinstance` in the
    #: application's own code answers differently about it, and a switch that changed what a
    #: customer's code receives without being asked for would be the dishonest part. Requires
    #: `HAJER_CAPTURE_CONTENT`, because a read path names a field of the answer.
    record_reply_reads: bool = False

    # ── client-side redaction ───────────────────────────────────────────────────────────────────
    #: `HAJER_REDACT_CLIENT` — **on by default** (accepted CEO row X3). Card numbers, IBANs, VINs,
    #: national ids, account-like runs, credentials, emails and phone numbers are removed from the
    #: request, the output, the evidence and every wrapped call *before* the body is serialised, under the
    #: same catalog the service uses (`_rules.py`, generated from it). `HAJER_REDACT_CLIENT=0` turns it
    #: off, which is a decision a team makes when a check of theirs needs a value the rules would remove —
    #: and the narrower answer to that is `ClientRedactionPolicy(paths_exempt=…)`, which keeps the rest.
    redact_client: bool = True

    #: `HAJER_PROXY_TIMEOUT_S` — how long `python -m hajer proxy` waits for the upstream before it answers
    #: 502 with a structured body. 60 seconds because a long completion is a minute of silence and a proxy
    #: that gave up sooner would break the very call it was turned on to observe; `--timeout` overrides it
    #: for one process.
    proxy_timeout_s: int = Field(default=60, gt=0)

    # ── attach mode ─────────────────────────────────────────────────────────────────────────────
    #: `HAJER_ATTACH` — the switch the import-time hook reads. With it on, `import hajer.autoattach`
    #: (the one line, or the `sitecustomize` shim in `hajer/_bootstrap/`, which is that same import)
    #: instruments the provider clients this process builds and every model call made outside a
    #: `hajer.scope()` becomes one `observe` observation with **no verifier**. Off by default: an
    #: observation is customer data leaving a customer's process, so it is opted into (plan question 2,
    #: assumed no). `hajer.attach()` called in code does not consult it — a developer who wrote the line
    #: has already said yes.
    attach: bool = False
    #: `HAJER_TAIL_INTERVAL_MS` — how often `python -m hajer tail --follow` asks for newer rows. A poll
    #: interval on a read-only route, so it is politeness rather than a measurement: one second is what a
    #: person watching a terminal reads as "live" without the route answering the same question ten times a
    #: second.
    tail_interval_ms: int = Field(default=1_000, gt=0)
    #: `HAJER_TAIL_LIMIT` — how many rows one `tail` page asks for. The route clips it to its own
    #: `INGEST_OBSERVATIONS_PAGE_MAX`, so this is the smaller of the two intentions.
    tail_limit: int = Field(default=50, gt=0)

    # ── kill switch ─────────────────────────────────────────────────────────────────────────────
    #: `HAJER_DISABLED` — inert regardless of the key.
    disabled: bool = False

    @field_validator("environment", mode="before")
    @classmethod
    def an_invalid_environment_is_dropped(cls, value: object) -> str | None:
        """Lower-case and in the pattern, or None: the one field whose bad value is dropped rather than refused."""
        if not isinstance(value, str):
            return None
        folded = value.strip().lower()
        return folded if re.fullmatch(ENVIRONMENT_PATTERN, folded, flags=re.ASCII) else None

    @model_validator(mode="after")
    def raw_capture_cannot_outrun_content_capture(self) -> Self:
        """Raw bytes are a superset of content, so the narrower switch has to be on for the wider one.

        `HajerConfigError` is not a `ValueError`, so pydantic lets it through unwrapped: the developer
        gets the sentence and the fix, not a validation report. One place enforces the rule, and it is
        enforced whether the settings came from the environment or from a constructor.
        """
        if self.record_reply_reads and not self.capture_content:
            raise HajerConfigError(
                "HAJER_RECORD_REPLY_READS",
                "1",
                "usable while HAJER_CAPTURE_CONTENT is off: a recorded read path names a field of "
                "the model's answer, which is content. Set HAJER_CAPTURE_CONTENT=1 as well, or "
                "leave reply-read recording off",
            )
        if self.capture_raw and not self.capture_content:
            raise HajerConfigError(
                "HAJER_CAPTURE_RAW",
                "1",
                "usable while HAJER_CAPTURE_CONTENT is off: raw capture ships the provider's own "
                "request and response bytes, which carry every message, tool argument and tool result. "
                "Set HAJER_CAPTURE_CONTENT=1 as well, or leave raw off",
            )
        return self

    @property
    def inert(self) -> bool:
        """True when the client must do nothing: no socket, no exception, no assessment."""
        return self.disabled or not self.api_key or not self.team_id

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> HajerSettings:
        """Read the `HAJER_*` variables. `env` is a seam for tests; production passes nothing."""
        source: Mapping[str, str] = os.environ if env is None else env
        return cls(
            ci_case_timeout_seconds=_positive_int(source, "HAJER_CI_CASE_TIMEOUT_SECONDS", 60),
            ci_fixed_repeat_limit=_positive_int(source, "HAJER_CI_FIXED_REPEAT_LIMIT", 20),
            ci_budget_microusd=(
                0
                if _string(source, "HAJER_CI_BUDGET_MICROUSD") == "0"
                else _positive_int(source, "HAJER_CI_BUDGET_MICROUSD", 0)
            ),
            ci_budget_file=_string(source, "HAJER_CI_BUDGET_FILE"),
            ci_input_deadline_ms=_positive_int(source, "HAJER_CI_INPUT_DEADLINE_MS", 30_000),
            ci_input_max_bytes=_positive_int(source, "HAJER_CI_INPUT_MAX_BYTES", 1_048_576),
            adapter_check_answers=_positive_int(source, "HAJER_ADAPTER_CHECK_ANSWERS", 16),
            ci_commit_sha=_string(source, "GITHUB_SHA") or _string(source, "HAJER_CI_COMMIT_SHA"),
            ci_branch=_string(source, "GITHUB_HEAD_REF")
            or _string(source, "GITHUB_REF_NAME")
            or _string(source, "HAJER_CI_BRANCH"),
            api_key=_string(source, "HAJER_API_KEY"),
            team_id=_string(source, "HAJER_TEAM_ID"),
            base_url=_string(source, "HAJER_BASE_URL") or DEFAULT_BASE_URL,
            environment=_string(source, "HAJER_ENVIRONMENT"),
            deadline_ms_default=_positive_int(source, "HAJER_DEADLINE_MS_DEFAULT", 1_500),
            observe_queue_max=_positive_int(source, "HAJER_OBSERVE_QUEUE_MAX", 1_000),
            observe_flush_interval_ms=_positive_int(source, "HAJER_OBSERVE_FLUSH_INTERVAL_MS", 2_000),
            observe_batch_max=_positive_int(source, "HAJER_OBSERVE_BATCH_MAX", 32),
            observe_flush_deadline_ms=_positive_int(source, "HAJER_OBSERVE_FLUSH_DEADLINE_MS", 5_000),
            observe_backoff_initial_ms=_positive_int(source, "HAJER_OBSERVE_BACKOFF_INITIAL_MS", 200),
            observe_backoff_max_ms=_positive_int(source, "HAJER_OBSERVE_BACKOFF_MAX_MS", 30_000),
            observe_sink=_string(source, "HAJER_OBSERVE_SINK"),
            body_max_bytes=_positive_int(source, "HAJER_BODY_MAX_BYTES", 65_536),
            capture_content=_boolean(source, "HAJER_CAPTURE_CONTENT", default=True),
            capture_raw=_boolean(source, "HAJER_CAPTURE_RAW", default=False),
            capture_call_site=_boolean(source, "HAJER_CAPTURE_CALL_SITE", default=True),
            capture_http=_boolean(source, "HAJER_CAPTURE_HTTP", default=False),
            project_root=_string(source, "HAJER_PROJECT_ROOT"),
            wrapped_call_max_bytes=_positive_int(source, "HAJER_WRAPPED_CALL_MAX_BYTES", 32_768),
            wrapped_calls_max=_positive_int(source, "HAJER_WRAPPED_CALLS_MAX", 32),
            boundary_body_max_bytes=_positive_int(source, "HAJER_BOUNDARY_BODY_MAX_BYTES", 16_384),
            boundary_responses_max=_positive_int(source, "HAJER_BOUNDARY_RESPONSES_MAX", 32),
            record_reply_reads=_boolean(source, "HAJER_RECORD_REPLY_READS", default=False),
            redact_client=_boolean(source, "HAJER_REDACT_CLIENT", default=True),
            proxy_timeout_s=_positive_int(source, "HAJER_PROXY_TIMEOUT_S", 60),
            attach=_boolean(source, "HAJER_ATTACH", default=False),
            tail_interval_ms=_positive_int(source, "HAJER_TAIL_INTERVAL_MS", 1_000),
            tail_limit=_positive_int(source, "HAJER_TAIL_LIMIT", 50),
            disabled=_boolean(source, "HAJER_DISABLED", default=False),
        )


#: Every field of `HajerSettings` and the `HAJER_*` variable that fills it, in declaration order. One
#: table, so `from_env` and `python -m hajer doctor` cannot disagree about which variable feeds which
#: field; `tests/test_settings.py::test_every_setting_has_a_variable` holds them to it.
VARIABLES: Final[tuple[tuple[str, str], ...]] = (
    ("ci_case_timeout_seconds", "HAJER_CI_CASE_TIMEOUT_SECONDS"),
    ("ci_fixed_repeat_limit", "HAJER_CI_FIXED_REPEAT_LIMIT"),
    ("ci_budget_microusd", "HAJER_CI_BUDGET_MICROUSD"),
    ("ci_budget_file", "HAJER_CI_BUDGET_FILE"),
    ("ci_input_deadline_ms", "HAJER_CI_INPUT_DEADLINE_MS"),
    ("ci_input_max_bytes", "HAJER_CI_INPUT_MAX_BYTES"),
    ("adapter_check_answers", "HAJER_ADAPTER_CHECK_ANSWERS"),
    ("ci_commit_sha", "HAJER_CI_COMMIT_SHA"),
    ("ci_branch", "HAJER_CI_BRANCH"),
    ("api_key", "HAJER_API_KEY"),
    ("team_id", "HAJER_TEAM_ID"),
    ("base_url", "HAJER_BASE_URL"),
    ("environment", "HAJER_ENVIRONMENT"),
    ("deadline_ms_default", "HAJER_DEADLINE_MS_DEFAULT"),
    ("observe_queue_max", "HAJER_OBSERVE_QUEUE_MAX"),
    ("observe_flush_interval_ms", "HAJER_OBSERVE_FLUSH_INTERVAL_MS"),
    ("observe_batch_max", "HAJER_OBSERVE_BATCH_MAX"),
    ("observe_flush_deadline_ms", "HAJER_OBSERVE_FLUSH_DEADLINE_MS"),
    ("observe_backoff_initial_ms", "HAJER_OBSERVE_BACKOFF_INITIAL_MS"),
    ("observe_backoff_max_ms", "HAJER_OBSERVE_BACKOFF_MAX_MS"),
    ("observe_sink", "HAJER_OBSERVE_SINK"),
    ("body_max_bytes", "HAJER_BODY_MAX_BYTES"),
    ("capture_content", "HAJER_CAPTURE_CONTENT"),
    ("capture_raw", "HAJER_CAPTURE_RAW"),
    ("capture_call_site", "HAJER_CAPTURE_CALL_SITE"),
    ("capture_http", "HAJER_CAPTURE_HTTP"),
    ("project_root", "HAJER_PROJECT_ROOT"),
    ("wrapped_call_max_bytes", "HAJER_WRAPPED_CALL_MAX_BYTES"),
    ("wrapped_calls_max", "HAJER_WRAPPED_CALLS_MAX"),
    ("boundary_body_max_bytes", "HAJER_BOUNDARY_BODY_MAX_BYTES"),
    ("boundary_responses_max", "HAJER_BOUNDARY_RESPONSES_MAX"),
    ("record_reply_reads", "HAJER_RECORD_REPLY_READS"),
    ("redact_client", "HAJER_REDACT_CLIENT"),
    ("proxy_timeout_s", "HAJER_PROXY_TIMEOUT_S"),
    ("attach", "HAJER_ATTACH"),
    ("tail_interval_ms", "HAJER_TAIL_INTERVAL_MS"),
    ("tail_limit", "HAJER_TAIL_LIMIT"),
    ("disabled", "HAJER_DISABLED"),
)
#: Every boolean setting. `doctor` prints them, and one test asserts that each reads every spelling.
BOOLEAN_FIELDS: Final[tuple[str, ...]] = (
    "capture_content",
    "capture_raw",
    "record_reply_reads",
    "redact_client",
    "attach",
    "disabled",
)
#: The one value that is never printed. A key in a terminal is a key in a scrollback buffer.
SECRET_FIELDS: Final[frozenset[str]] = frozenset({"api_key"})
SOURCE_ENV: Final[str] = "env"
SOURCE_DEFAULT: Final[str] = "default"
#: What `doctor` prints for a setting that has no value at all.
ABSENT: Final[str] = "absent"


@dataclass(frozen=True, slots=True)
class SettingSource:
    """One setting as an operator needs to see it: the value in force, and where it came from.

    "Where it came from" is the whole point of the row. Half of what looks like a broken SDK is a variable
    somebody expected to be set and is not, and a value printed on its own cannot tell that from a default
    that happens to match.
    """

    name: str
    variable: str
    value: str
    source: str


def ci_child_environment(settings: HajerSettings | None = None) -> dict[str, str]:
    """The environment for a CI suite's application process: this one's, with `HAJER_ENVIRONMENT` set to `ci`.

    Traffic tagged CI_ENVIRONMENT is never selected as a test input, so the application a suite runs cannot feed the
    next suite, whatever `HAJER_ENVIRONMENT` the customer's CI exports.
    """
    # The parent needs the Hajer credential for advisory grading and uploads. The customer's app does not.
    environment = {key: value for key, value in os.environ.items() if key != "HAJER_API_KEY"}
    environment["HAJER_ENVIRONMENT"] = CI_ENVIRONMENT
    if settings is not None:
        environment["HAJER_CI_BUDGET_MICROUSD"] = (
            str(settings.ci_budget_microusd) if settings.ci_budget_microusd else ""
        )
        environment["HAJER_CI_BUDGET_FILE"] = settings.ci_budget_file or ""
    return environment


#: What one free-text field of a CI upload may hold: the backend's `CI_MAX_REASON` (an adapter check's `detail`, a
#: check's `reason`, an id). A longer string would reject the whole run's upload, so every one is clipped to it.
CI_TEXT_MAX: Final[int] = 256


def clipped(text: str) -> str:
    """`text` cut to what a CI upload's free-text field holds (`CI_TEXT_MAX`)."""
    return text if len(text) <= CI_TEXT_MAX else text[: CI_TEXT_MAX - 1] + "…"


#: The provider keys a client needs to be constructed. `verify-adapters` sets a placeholder where the environment has
#: none: its model is fake and nothing leaves the process, but a client that refuses to start without a key would
#: stop the call before it reaches any site.
_PLACEHOLDER_KEYS: Final[tuple[str, ...]] = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY")


def adapter_check_environment(declared: Mapping[str, str]) -> dict[str, str]:
    """The environment `verify-adapters` runs an adapter in: the CI child's, the `.hajer/replay.toml` `[environment]`
    literals, and a placeholder for a provider key the environment does not hold."""
    return ci_execution_environment(declared, live=False)


def ci_execution_environment(
    declared: Mapping[str, str], *, live: bool, settings: HajerSettings | None = None
) -> dict[str, str]:
    """Use the same declared app configuration in reachability checks and execution.

    Replay may construct clients using inert placeholders; live execution never invents
    provider credentials. Repository literals cannot override Hajer's budget or transport.
    """
    environment = ci_child_environment(settings)
    if not live:
        for name in _PLACEHOLDER_KEYS:
            if not environment.get(name):
                environment[name] = "hajer-verify-adapters-no-key"
    environment.update({key: value for key, value in declared.items() if not key.startswith("HAJER_")})
    return environment


def settings_sources(settings: HajerSettings, env: Mapping[str, str] | None = None) -> tuple[SettingSource, ...]:
    """Every `HAJER_*` setting, its value in force, and whether it came from the environment or a default."""
    source: Mapping[str, str] = os.environ if env is None else env
    dumped = settings.model_dump()
    return tuple(
        SettingSource(
            name=name,
            variable=variable,
            value=_rendered(name, dumped.get(name), set_in_env=_string(source, variable) is not None),
            source=SOURCE_ENV if _string(source, variable) is not None else SOURCE_DEFAULT,
        )
        for name, variable in VARIABLES
    )


def _rendered(name: str, value: object, *, set_in_env: bool) -> str:
    """One value as one line of output: never a credential, a boolean spelled one way, and a dropped tag named."""
    if name in SECRET_FIELDS:
        return "set" if value else ABSENT
    if name == "environment" and value is None and set_in_env:
        return ENVIRONMENT_IGNORED
    if isinstance(value, bool):  # before the int check: a bool is an int
        return "true" if value else "false"
    if value is None:
        return ABSENT
    return str(value)


def _string(env: Mapping[str, str], name: str) -> str | None:
    raw = env.get(name)
    if raw is None:
        return None
    stripped = raw.strip()
    return stripped or None


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = _string(env, name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        # int() includes the supplied value in its error. Raise outside the handler
        # so neither a cause nor a retained exception context can disclose it.
        pass
    else:
        if value <= 0:
            raise HajerConfigError(name, raw, "a positive integer")
        return value
    raise HajerConfigError(name, raw, "an integer")


def _boolean(env: Mapping[str, str], name: str, *, default: bool) -> bool:
    raw = _string(env, name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise HajerConfigError(name, raw, "a boolean (1/0, true/false, yes/no, on/off)")
