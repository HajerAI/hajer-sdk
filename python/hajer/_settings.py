"""`HajerSettings` — every bound the SDK obeys, each one a field with an environment variable.

Nothing in this package spells a limit as a literal anywhere but here: nothing
is hard-coded. Each default is explained in `README.md` under *Settings*, with what would make
you change it. `from_env` is the **only** place in the SDK that reads the process environment; the
ruff ban on `os.environ` / `os.getenv` (`pyproject.toml`) keeps it that way.

**Inert mode is the important behaviour here.** Without `HAJER_API_KEY` and `HAJER_TEAM_ID` — or with
`HAJER_DISABLED=1` — the SDK is inert: every call is still recorded locally, no span leaves the process,
nothing opens a socket and nothing raises. That is what lets the pull request that installs the SDK land in a
repository whose test suite has no Hajer credentials and still pass unchanged, and it is why a missing key is
not a `HajerConfigError`.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import unquote

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
#: 1 MiB: a document larger than this is not one a span attribute could carry anyway.
REDACT_MAX_BYTES: Final[int] = 1_048_576
#: 50,000 values. A tuple with more nodes than this is not the bounded shape the inline lane supports, and
#: an unbounded node count is a scan cost nobody can reason about before the call.
REDACT_MAX_NODES: Final[int] = 50_000
#: 64 levels. Well above the ingest depth bound the service applies to evidence (6) and well below
#: CPython's own recursion limit, which this walk does not use anyway.
REDACT_MAX_DEPTH: Final[int] = 64
#: 65,536 characters of one value: past it the scan costs more than the value is worth and the honest answer
#: is "not scanned".
REDACT_MAX_STRING_CHARS: Final[int] = 65_536

#: What one stable id in a test's reserved `metadata.hajer` may be (`hajer.evals._metadata`): a workflow, obligation,
#: component or source-trace id of at most 128 characters — the platform's own bound on a workflow key, so an id the
#: eval declares is one the platform can store. A schema constant like the four above, not a budget a team tunes.
ID_MAX_CHARS: Final[int] = 128
#: What `hajer.context()` may carry (`hajer._context`): tags, metadata keys, and the length of any one value.
#: Schema constants like `ID_MAX_CHARS`: a label past them is clipped, never refused, because a declaration about a
#: conversation must not cost the request it is made in.
CONTEXT_TAGS_MAX: Final[int] = 16
CONTEXT_METADATA_KEYS_MAX: Final[int] = 32
CONTEXT_VALUE_MAX_CHARS: Final[int] = 1_024
#: What a repository's `hajer.yaml` may hold (`hajer.evals._manifest`): the platform reads the file by the same
#: bounds, so a manifest this reader accepts is one the platform stores.
MANIFEST_FILE_MAX_BYTES: Final[int] = 262_144
MANIFEST_SUITES_MAX: Final[int] = 100
MANIFEST_OBLIGATIONS_MAX: Final[int] = 500
MANIFEST_TITLE_MAX_CHARS: Final[int] = 512
MANIFEST_DESCRIPTION_MAX_CHARS: Final[int] = 4_096

#: What one environment tag may be (`HAJER_ENVIRONMENT`): lower-case letters, digits and dashes, a letter or digit
#: first, at most 64 characters — the platform's own pattern for an environment name, so a tag this process sends is
#: one the service stores. A value outside it is dropped rather than sent.
ENVIRONMENT_PATTERN: Final[str] = r"^[a-z0-9][a-z0-9-]{0,63}$"
#: What `doctor` prints for a `HAJER_ENVIRONMENT` that was set and dropped.
ENVIRONMENT_IGNORED: Final[str] = "ignored: invalid"
#: The environment tag every span and observation carries while the application runs under `hajer eval`.
#: Production and eval traces are the same shape; this tag, and the `hajer.eval.*` attributes, are the difference.
EVAL_ENVIRONMENT: Final[str] = "eval"
#: The engine's phone-home switches, every one of them off while `hajer eval` runs it: product analytics, the
#: update check, result sharing, remote test generation and the share-by-email prompt. `tests/test_evals_golden.py`
#: proves it at the socket; this tuple is what `eval_engine_environment` sets and what the unit test asserts.
PROMPTFOO_DISABLE_FLAGS: Final[tuple[str, ...]] = (
    "PROMPTFOO_DISABLE_TELEMETRY",
    "PROMPTFOO_DISABLE_UPDATE",
    "PROMPTFOO_DISABLE_SHARING",
    "PROMPTFOO_DISABLE_REMOTE_GENERATION",
    "PROMPTFOO_DISABLE_REDTEAM_REMOTE_GENERATION",
    "PROMPTFOO_DISABLE_SHARE_EMAIL_REQUEST",
)
#: The CI variables `hajer eval` reads for an eval run's Git context (`hajer.evals._git`), and no other: a
#: whitelist, so a CI job's secrets are never swept up with them. GitHub Actions, GitLab CI, CircleCI,
#: Buildkite, and the generic `CI` flag.
CI_VARIABLES: Final[tuple[str, ...]] = (
    "CI",
    "GITHUB_ACTIONS",
    "GITHUB_SHA",
    "GITHUB_REF",
    "GITHUB_REF_NAME",
    "GITHUB_HEAD_REF",
    "GITHUB_BASE_REF",
    "GITHUB_RUN_ID",
    "GITHUB_REPOSITORY",
    "GITHUB_SERVER_URL",
    "GITLAB_CI",
    "CI_COMMIT_SHA",
    "CI_COMMIT_REF_NAME",
    "CI_MERGE_REQUEST_IID",
    "CI_MERGE_REQUEST_SOURCE_BRANCH_NAME",
    "CI_MERGE_REQUEST_TARGET_BRANCH_NAME",
    "CI_PIPELINE_ID",
    "CI_PROJECT_URL",
    "CIRCLECI",
    "CIRCLE_SHA1",
    "CIRCLE_BRANCH",
    "CIRCLE_PULL_REQUEST",
    "CIRCLE_BUILD_NUM",
    "CIRCLE_REPOSITORY_URL",
    "BUILDKITE",
    "BUILDKITE_COMMIT",
    "BUILDKITE_BRANCH",
    "BUILDKITE_PULL_REQUEST",
    "BUILDKITE_PULL_REQUEST_BASE_BRANCH",
    "BUILDKITE_BUILD_ID",
    "BUILDKITE_REPO",
)


def _default_cache_dir() -> str:
    """`~/.cache/hajer`: what a constructor gets; `from_env` honours `$XDG_CACHE_HOME` before it."""
    return str(Path.home() / ".cache" / "hajer")


_TRUE = frozenset({"1", "true", "t", "yes", "y", "on"})
_FALSE = frozenset({"0", "false", "f", "no", "n", "off"})


class HajerSettings(BaseModel):
    """Everything the SDK reads from configuration, validated once, then frozen."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # ── identity ────────────────────────────────────────────────────────────────────────────────
    #: `HAJER_API_KEY` — a team API key. Absent → inert.
    api_key: str | None = None
    #: `HAJER_TEAM_ID` — the team every route is scoped to. Absent → inert.
    team_id: str | None = None
    #: `HAJER_BASE_URL`
    base_url: str = DEFAULT_BASE_URL
    #: `HAJER_ENVIRONMENT` — the environment this process runs in (`production`, `staging`, `dev`), carried on every
    #: span as `deployment.environment.name`. Folded to lower case; a value that is still not `ENVIRONMENT_PATTERN` is
    #: dropped — never raised, because a tag is a hint and must not cost the call it rides on — and `doctor` says so.
    environment: str | None = None

    # ── capture ─────────────────────────────────────────────────────────────────────────────────
    #: `HAJER_BODY_MAX_BYTES` — bytes of a provider's raw response body (`with_raw_response`) buffered to read the
    #: answer out of it; past it the body is handed through unread and the record says so.
    body_max_bytes: int = Field(default=65_536, gt=0)
    #: `HAJER_CAPTURE_CONTENT` — on by default. With it off, `wrap` records no message text, no tool
    #: arguments and no tool results: shapes, names, counts, timing and usage only.
    capture_content: bool = True
    #: `HAJER_CAPTURE_CALL_SITE` — **on by default.** A wrapped call's summary carries where it was made
    #: (up to `MAX_CALLER_FRAMES` application frames: module, qualified name, file, line, and for a file
    #: under the project root the SHA-256 digest of its bytes — never its text — while it is unchanged since
    #: the process started) and the `host:port` of the client's endpoint. A frame's file is only ever relative to the project root
    #: (`HAJER_PROJECT_ROOT`, else the working directory); a file outside it is sent as
    #: `<outside>/<basename>`, and a home directory (`/home/<user>`, `/Users/<user>`,
    #: `C:\\Users\\<user>`) is never part of what is sent, whatever the root. These are locations and a
    #: host name, never values, which is why they need no content consent: the platform uses them to link
    #: the call to the code that made it. `HAJER_CAPTURE_CALL_SITE=0` sends neither.
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
    #: `HAJER_WRAPPED_CALL_MAX_BYTES` — what one call's content may carry: its messages, its output, its tool
    #: arguments and results. Past it the longest texts are clipped in the middle — head and tail kept, their
    #: full length and SHA-256 marked between them — and the record says it was clipped.
    wrapped_call_max_bytes: int = Field(default=32_768, gt=0)
    #: `HAJER_WRAPPED_CALLS_MAX` — wrapped calls one task accumulates before further ones are
    #: counted and dropped rather than kept.
    wrapped_calls_max: int = Field(default=32, gt=0)
    # ── client-side redaction ───────────────────────────────────────────────────────────────────
    #: `HAJER_REDACT_CLIENT` — **on by default**. Card numbers, IBANs, VINs, national ids, account-like runs,
    #: credentials, emails and phone numbers are removed from every message, output, tool argument and tool
    #: result *before* a span carries it, under the catalog in `_rules.py`. `HAJER_REDACT_CLIENT=0` turns it
    #: off, which is a decision a team makes when a value the rules would remove is one they need to see —
    #: and the narrower answer to that is `ClientRedactionPolicy(paths_exempt=…)`, which keeps the rest.
    redact_client: bool = True

    # ── attach mode ─────────────────────────────────────────────────────────────────────────────
    #: `HAJER_ATTACH` — the switch the import-time hook reads. With it on, `import hajer.autoattach`
    #: (the one line, or the `sitecustomize` shim in `hajer/_bootstrap/`, which is that same import)
    #: instruments the provider clients this process builds, so every model call they make is recorded and
    #: exported. Off by default: a span is customer data leaving a customer's process, so it is opted into.
    #: `hajer.attach()` called in code does not consult it — a developer who wrote the line has already said yes.
    attach: bool = False

    # ── telemetry (`hajer.workflow` / `component` / `tool`, and every model call) ───────────────
    #: `HAJER_TRACES_ENABLED` — **on by default.** With a key and a team, every span this process emits is
    #: exported to the platform's OTLP receiver for the team (`hajer/_telemetry.py`). `0` keeps the spans on
    #: the application's own OpenTelemetry provider, if it has one, and sends nothing to Hajer.
    traces_enabled: bool = True
    #: `HAJER_MODEL_SPANS` — **on by default.** Every model call `wrap`, `instrument` or `attach` records is emitted
    #: as a `gen_ai` span of its own. `0` for a process whose model calls another instrumentation already traces:
    #: the SDK suppresses its span when it sees that instrumentation's span around the same call, but best-effort,
    #: and this is the switch that makes it certain. The declared spans (`hajer.workflow` and the rest) are unaffected.
    model_spans: bool = True
    #: `HAJER_OTLP_ENDPOINT` — a collector of your own instead of the platform: the emitter exports to its
    #: `/v1/traces`, with no credential unless `HAJER_OTLP_HEADERS` names one. Falls back to
    #: `OTEL_EXPORTER_OTLP_ENDPOINT`, the standard variable. It wins over the platform target: a process that
    #: named a collector meant it. `hajer eval` sets it to the engine's loopback receiver.
    otlp_endpoint: str | None = None
    #: `HAJER_OTLP_HEADERS` — headers for the exporter, in OpenTelemetry's own `key=value,key2=value2`
    #: spelling (values percent-decoded). Added to the platform's `Authorization` header, or the only
    #: headers a `HAJER_OTLP_ENDPOINT` collector gets.
    otlp_headers: str | None = None
    #: `HAJER_SERVICE_NAME` — `service.name` on the resource of the provider the SDK builds when the
    #: application has none; `OTEL_SERVICE_NAME`, the standard variable, is read when it is absent.
    service_name: str | None = None
    #: `HAJER_TRACE_FLUSH_TIMEOUT_MS` — how long `flush()` waits for exported spans to leave. Two seconds:
    #: an eval provider flushes before it answers, so that the trace is there when the row is graded.
    trace_flush_timeout_ms: int = Field(default=2_000, gt=0)
    #: `HAJER_TRACE_EXPORT_TIMEOUT_MS` — one export request's deadline, and so the most an exit flush waits
    #: on an unreachable receiver. Five seconds: a batch is a few hundred kilobytes at most.
    trace_export_timeout_ms: int = Field(default=5_000, gt=0)
    #: `HAJER_TRACE_BATCH_DELAY_MS` — how long a partial batch waits before it is exported anyway.
    trace_batch_delay_ms: int = Field(default=5_000, gt=0)
    #: `HAJER_TRACE_QUEUE_MAX` — spans held for export before the newest are dropped; OpenTelemetry's own default.
    trace_queue_max: int = Field(default=2_048, gt=0)
    #: `HAJER_TRACE_BATCH_MAX` — spans in one export request. Below OpenTelemetry's 512 on purpose: a model
    #: span may carry `HAJER_WRAPPED_CALL_MAX_BYTES` of content, so 128 keeps one request near 4 MB at worst.
    trace_batch_max: int = Field(default=128, gt=0)

    # ── evals (`hajer eval`) ────────────────────────────────────────────────────────────────────
    #: `HAJER_CACHE_DIR` — where `hajer eval` installs the pinned engine (`engine/<lockfile digest>/`) and keeps
    #: run directories (`runs/<run id>/`). `$XDG_CACHE_HOME/hajer`, else `~/.cache/hajer`.
    cache_dir: str = Field(default_factory=_default_cache_dir)
    #: `HAJER_EVAL_INSTALL_TIMEOUT_S` — how long one `npm ci` of the engine may take. Ten minutes covers a
    #: cold cache on a slow CI runner; a hung registry is reported rather than waited on forever.
    eval_install_timeout_s: int = Field(default=600, gt=0)
    #: `HAJER_EVAL_RUNS_KEEP` — run directories kept under the cache; older ones are removed at the start of a run.
    eval_runs_keep: int = Field(default=20, gt=0)
    #: `HAJER_EVAL_OTLP_PORT` — the loopback port the engine's trace receiver listens on. 4318 is OTLP/HTTP's
    #: own; when it is busy `hajer eval` picks a free one and tells the engine and the emitter the same number.
    eval_otlp_port: int = Field(default=4318, gt=0)
    #: `HAJER_EVAL_UPLOAD_ATTEMPTS` — sends of one run's payload before the upload is reported failed.
    eval_upload_attempts: int = Field(default=3, gt=0)
    #: `HAJER_EVAL_UPLOAD_MAX_BYTES` — the payload's ceiling; past it the spans are dropped, then the outputs,
    #: before the upload is refused locally. 8 MiB: a run's spans are the bulk, and they are allow-listed first.
    eval_upload_max_bytes: int = Field(default=8_388_608, gt=0)
    #: `HAJER_EVAL_UPLOAD_DEADLINE_MS` — the deadline of one upload request.
    eval_upload_deadline_ms: int = Field(default=30_000, gt=0)
    #: `HAJER_EVAL_UPLOAD_BACKOFF_INITIAL_MS` — the first wait after a failed upload attempt.
    eval_upload_backoff_initial_ms: int = Field(default=200, gt=0)
    #: `HAJER_EVAL_UPLOAD_BACKOFF_MAX_MS` — the ceiling that doubling backoff never exceeds.
    eval_upload_backoff_max_ms: int = Field(default=30_000, gt=0)
    #: `HAJER_EVAL_OUTPUT_MAX_CHARS` — characters of one result's output kept in the payload, after redaction.
    eval_output_max_chars: int = Field(default=4_096, gt=0)
    #: `HAJER_EVAL_SPANS_MAX` — spans of one result's trace kept in the payload; the summary counts them all.
    eval_spans_max: int = Field(default=256, gt=0)
    #: `HAJER_EVAL_GIT_TIMEOUT_S` — how long one `git` read for the run's commit context may take. Five seconds
    #: is generous for `rev-parse` and `status`; a hung filesystem is reported as no context, not waited on.
    eval_git_timeout_s: int = Field(default=5, gt=0)
    #: `HAJER_EVAL_RUN_ID` — set by `hajer eval` for the engine process it starts, and read back by the hook and
    #: the span emitter inside it. Never set by hand.
    eval_run_id: str | None = None
    #: `HAJER_EVAL_WORKFLOW` — `hajer eval --workflow`, carried to the hook that filters the suite.
    eval_workflow: str | None = None
    #: `HAJER_EVAL_OBLIGATIONS` — `hajer eval --obligation …`, comma-separated, carried the same way.
    eval_obligations: str | None = None
    #: `HAJER_EVAL_HOOK_REPORT` — where the hook writes what it classified, filtered and warned about.
    eval_hook_report: str | None = None
    #: `HAJER_EVAL_MANIFEST` — the `hajer.yaml` the run was started under, carried to the hook; with it set, every
    #: obligation a test names must be one the manifest declares.
    eval_manifest: str | None = None
    #: `HAJER_EVAL_MANIFEST_OBLIGATIONS` — the obligation ids that manifest declares, comma-separated (empty allowed).
    eval_manifest_obligations: str | None = None

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

    @property
    def inert(self) -> bool:
        """True when nothing may leave the process: no socket, no exception, no span."""
        return self.disabled or not self.api_key or not self.team_id

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> HajerSettings:
        """Read the `HAJER_*` variables. `env` is a seam for tests; production passes nothing."""
        source: Mapping[str, str] = os.environ if env is None else env
        return cls(
            api_key=_string(source, "HAJER_API_KEY"),
            team_id=_string(source, "HAJER_TEAM_ID"),
            base_url=_string(source, "HAJER_BASE_URL") or DEFAULT_BASE_URL,
            environment=_string(source, "HAJER_ENVIRONMENT"),
            body_max_bytes=_positive_int(source, "HAJER_BODY_MAX_BYTES", 65_536),
            capture_content=_boolean(source, "HAJER_CAPTURE_CONTENT", default=True),
            capture_call_site=_boolean(source, "HAJER_CAPTURE_CALL_SITE", default=True),
            capture_http=_boolean(source, "HAJER_CAPTURE_HTTP", default=False),
            project_root=_string(source, "HAJER_PROJECT_ROOT"),
            wrapped_call_max_bytes=_positive_int(source, "HAJER_WRAPPED_CALL_MAX_BYTES", 32_768),
            wrapped_calls_max=_positive_int(source, "HAJER_WRAPPED_CALLS_MAX", 32),
            redact_client=_boolean(source, "HAJER_REDACT_CLIENT", default=True),
            attach=_boolean(source, "HAJER_ATTACH", default=False),
            traces_enabled=_boolean(source, "HAJER_TRACES_ENABLED", default=True),
            model_spans=_boolean(source, "HAJER_MODEL_SPANS", default=True),
            otlp_endpoint=_string(source, "HAJER_OTLP_ENDPOINT") or _string(source, "OTEL_EXPORTER_OTLP_ENDPOINT"),
            otlp_headers=_string(source, "HAJER_OTLP_HEADERS"),
            service_name=_string(source, "HAJER_SERVICE_NAME") or _string(source, "OTEL_SERVICE_NAME"),
            trace_flush_timeout_ms=_positive_int(source, "HAJER_TRACE_FLUSH_TIMEOUT_MS", 2_000),
            trace_export_timeout_ms=_positive_int(source, "HAJER_TRACE_EXPORT_TIMEOUT_MS", 5_000),
            trace_batch_delay_ms=_positive_int(source, "HAJER_TRACE_BATCH_DELAY_MS", 5_000),
            trace_queue_max=_positive_int(source, "HAJER_TRACE_QUEUE_MAX", 2_048),
            trace_batch_max=_positive_int(source, "HAJER_TRACE_BATCH_MAX", 128),
            cache_dir=_string(source, "HAJER_CACHE_DIR")
            or (
                str(Path(xdg) / "hajer")
                if (xdg := _string(source, "XDG_CACHE_HOME")) is not None
                else _default_cache_dir()
            ),
            eval_install_timeout_s=_positive_int(source, "HAJER_EVAL_INSTALL_TIMEOUT_S", 600),
            eval_runs_keep=_positive_int(source, "HAJER_EVAL_RUNS_KEEP", 20),
            eval_otlp_port=_positive_int(source, "HAJER_EVAL_OTLP_PORT", 4318),
            eval_upload_attempts=_positive_int(source, "HAJER_EVAL_UPLOAD_ATTEMPTS", 3),
            eval_upload_max_bytes=_positive_int(source, "HAJER_EVAL_UPLOAD_MAX_BYTES", 8_388_608),
            eval_upload_deadline_ms=_positive_int(source, "HAJER_EVAL_UPLOAD_DEADLINE_MS", 30_000),
            eval_upload_backoff_initial_ms=_positive_int(source, "HAJER_EVAL_UPLOAD_BACKOFF_INITIAL_MS", 200),
            eval_upload_backoff_max_ms=_positive_int(source, "HAJER_EVAL_UPLOAD_BACKOFF_MAX_MS", 30_000),
            eval_output_max_chars=_positive_int(source, "HAJER_EVAL_OUTPUT_MAX_CHARS", 4_096),
            eval_spans_max=_positive_int(source, "HAJER_EVAL_SPANS_MAX", 256),
            eval_git_timeout_s=_positive_int(source, "HAJER_EVAL_GIT_TIMEOUT_S", 5),
            eval_run_id=_string(source, "HAJER_EVAL_RUN_ID"),
            eval_workflow=_string(source, "HAJER_EVAL_WORKFLOW"),
            eval_obligations=_string(source, "HAJER_EVAL_OBLIGATIONS"),
            eval_hook_report=_string(source, "HAJER_EVAL_HOOK_REPORT"),
            eval_manifest=_string(source, "HAJER_EVAL_MANIFEST"),
            eval_manifest_obligations=_string(source, "HAJER_EVAL_MANIFEST_OBLIGATIONS"),
            disabled=_boolean(source, "HAJER_DISABLED", default=False),
        )

    @property
    def otlp_header_values(self) -> dict[str, str]:
        """`HAJER_OTLP_HEADERS` as the headers it names: OpenTelemetry's `k=v,k2=v2`, values percent-decoded.

        A pair with no `=` or an empty key is skipped rather than refused: a header is a hint to an exporter,
        and the one place a malformed value could matter is `doctor`, which prints what was read.
        """
        if self.otlp_headers is None:
            return {}
        headers: dict[str, str] = {}
        for pair in self.otlp_headers.split(","):
            key, separator, value = pair.partition("=")
            if separator and key.strip():
                headers[key.strip()] = unquote(value.strip())
        return headers

    @property
    def eval_obligation_ids(self) -> tuple[str, ...]:
        """`HAJER_EVAL_OBLIGATIONS` as the ids it names, blanks dropped."""
        return _ids(self.eval_obligations)

    @property
    def eval_declared_obligation_ids(self) -> tuple[str, ...]:
        """`HAJER_EVAL_MANIFEST_OBLIGATIONS` as the ids it names, blanks dropped."""
        return _ids(self.eval_manifest_obligations)


#: Every field of `HajerSettings` and the `HAJER_*` variable that fills it, in declaration order. One
#: table, so `from_env` and `python -m hajer doctor` cannot disagree about which variable feeds which
#: field; `tests/test_settings.py::test_every_setting_has_a_variable` holds them to it.
VARIABLES: Final[tuple[tuple[str, str], ...]] = (
    ("api_key", "HAJER_API_KEY"),
    ("team_id", "HAJER_TEAM_ID"),
    ("base_url", "HAJER_BASE_URL"),
    ("environment", "HAJER_ENVIRONMENT"),
    ("body_max_bytes", "HAJER_BODY_MAX_BYTES"),
    ("capture_content", "HAJER_CAPTURE_CONTENT"),
    ("capture_call_site", "HAJER_CAPTURE_CALL_SITE"),
    ("capture_http", "HAJER_CAPTURE_HTTP"),
    ("project_root", "HAJER_PROJECT_ROOT"),
    ("wrapped_call_max_bytes", "HAJER_WRAPPED_CALL_MAX_BYTES"),
    ("wrapped_calls_max", "HAJER_WRAPPED_CALLS_MAX"),
    ("redact_client", "HAJER_REDACT_CLIENT"),
    ("attach", "HAJER_ATTACH"),
    ("traces_enabled", "HAJER_TRACES_ENABLED"),
    ("model_spans", "HAJER_MODEL_SPANS"),
    ("otlp_endpoint", "HAJER_OTLP_ENDPOINT"),
    ("otlp_headers", "HAJER_OTLP_HEADERS"),
    ("service_name", "HAJER_SERVICE_NAME"),
    ("trace_flush_timeout_ms", "HAJER_TRACE_FLUSH_TIMEOUT_MS"),
    ("trace_export_timeout_ms", "HAJER_TRACE_EXPORT_TIMEOUT_MS"),
    ("trace_batch_delay_ms", "HAJER_TRACE_BATCH_DELAY_MS"),
    ("trace_queue_max", "HAJER_TRACE_QUEUE_MAX"),
    ("trace_batch_max", "HAJER_TRACE_BATCH_MAX"),
    ("cache_dir", "HAJER_CACHE_DIR"),
    ("eval_install_timeout_s", "HAJER_EVAL_INSTALL_TIMEOUT_S"),
    ("eval_runs_keep", "HAJER_EVAL_RUNS_KEEP"),
    ("eval_otlp_port", "HAJER_EVAL_OTLP_PORT"),
    ("eval_upload_attempts", "HAJER_EVAL_UPLOAD_ATTEMPTS"),
    ("eval_upload_max_bytes", "HAJER_EVAL_UPLOAD_MAX_BYTES"),
    ("eval_upload_deadline_ms", "HAJER_EVAL_UPLOAD_DEADLINE_MS"),
    ("eval_upload_backoff_initial_ms", "HAJER_EVAL_UPLOAD_BACKOFF_INITIAL_MS"),
    ("eval_upload_backoff_max_ms", "HAJER_EVAL_UPLOAD_BACKOFF_MAX_MS"),
    ("eval_output_max_chars", "HAJER_EVAL_OUTPUT_MAX_CHARS"),
    ("eval_spans_max", "HAJER_EVAL_SPANS_MAX"),
    ("eval_git_timeout_s", "HAJER_EVAL_GIT_TIMEOUT_S"),
    ("eval_run_id", "HAJER_EVAL_RUN_ID"),
    ("eval_workflow", "HAJER_EVAL_WORKFLOW"),
    ("eval_obligations", "HAJER_EVAL_OBLIGATIONS"),
    ("eval_hook_report", "HAJER_EVAL_HOOK_REPORT"),
    ("eval_manifest", "HAJER_EVAL_MANIFEST"),
    ("eval_manifest_obligations", "HAJER_EVAL_MANIFEST_OBLIGATIONS"),
    ("disabled", "HAJER_DISABLED"),
)
#: Every boolean setting. `doctor` prints them, and one test asserts that each reads every spelling.
BOOLEAN_FIELDS: Final[tuple[str, ...]] = (
    "capture_content",
    "redact_client",
    "attach",
    "traces_enabled",
    "model_spans",
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


def eval_engine_environment(
    settings: HajerSettings,
    *,
    run_id: str,
    run_dir: str,
    otlp_port: int,
    python_executable: str,
    workflow: str | None = None,
    obligations: tuple[str, ...] = (),
    manifest: Path | None = None,
    declared_obligations: tuple[str, ...] = (),
) -> dict[str, str]:
    """The environment `hajer eval` starts the engine in: this process's, minus the Hajer key, plus the switches.

    The key stays with the parent, which uploads: the application under test is then inert towards Hajer, so an
    eval never exports its spans as if they were production traffic. `PROMPTFOO_PYTHON` is this interpreter, so the hook
    and the Python providers import the same `hajer` that started the run; the config and cache directories are
    the run's own, never `~/.promptfoo`; and the emitter is pointed at the engine's loopback receiver.
    """
    environment = {key: value for key, value in os.environ.items() if key != "HAJER_API_KEY"}
    endpoint = f"http://127.0.0.1:{otlp_port}"
    environment.update(dict.fromkeys(PROMPTFOO_DISABLE_FLAGS, "1"))
    environment.update(
        {
            "NO_UPDATE_NOTIFIER": "1",
            "PROMPTFOO_CONFIG_DIR": str(Path(run_dir) / "promptfoo"),
            "PROMPTFOO_CACHE_PATH": str(Path(settings.cache_dir) / "promptfoo-cache"),
            "PROMPTFOO_PYTHON": python_executable,
            # Only Hajer's own variable: the engine's OpenTelemetry SDK reads `OTEL_EXPORTER_OTLP_ENDPOINT` as a
            # complete URL and would post to the receiver's root, which answers 404 on every flush.
            "HAJER_OTLP_ENDPOINT": endpoint,
            "HAJER_ENVIRONMENT": EVAL_ENVIRONMENT,
            "HAJER_EVAL_RUN_ID": run_id,
            "HAJER_EVAL_WORKFLOW": workflow or "",
            "HAJER_EVAL_OBLIGATIONS": ",".join(obligations),
            "HAJER_EVAL_HOOK_REPORT": str(Path(run_dir) / "hook-report.json"),
        }
    )
    if manifest is not None:
        environment["HAJER_EVAL_MANIFEST"] = str(manifest)
        environment["HAJER_EVAL_MANIFEST_OBLIGATIONS"] = ",".join(declared_obligations)
    else:
        environment.pop("HAJER_EVAL_MANIFEST", None)
        environment.pop("HAJER_EVAL_MANIFEST_OBLIGATIONS", None)
    return environment


def ci_environment_snapshot(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """The `CI_VARIABLES` this process has, and nothing else: what `hajer.evals._git` reads for an eval run."""
    source: Mapping[str, str] = os.environ if env is None else env
    return {name: value for name in CI_VARIABLES if (value := _string(source, name)) is not None}


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


def _ids(joined: str | None) -> tuple[str, ...]:
    if joined is None:
        return ()
    return tuple(item.strip() for item in joined.split(",") if item.strip())


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
