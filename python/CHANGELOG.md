# Changelog

All notable changes to the `hajer` Python package. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the package uses [semantic versioning](https://semver.org/);
while the version is `0.x`, a minor release may change the public API.

## [Unreleased]

## [0.2.2] - 2026-10-06

### Added

- **`hajer eval` warns about an assertion value that names a var the test does not have.** promptfoo renders an
  assertion's `value` as a Nunjucks template with the test's vars, and an undefined one renders as an empty string,
  so `not-contains: "Dear {{firstName}}"` with no `firstName` var checks for `"Dear "` and passes. The `beforeAll`
  hook now reports `W_TEMPLATE_UNDEFINED_VAR` for every `{{ name }}` (or `{{ name.attr }}`, `{{ name | filter }}`)
  whose root is not one of the test's vars (`defaultTest.vars` under its own), in the test's `assert`, in
  `defaultTest.assert` and inside `assert-set`s, ignoring `{% raw %}` blocks. A warning, never an error: it is
  printed with the others and carried in the run's `warnings`. A test whose vars are not an object, or that has a
  `transformVars`, is not checked.

### Fixed

- **`hajer doctor` names the standard OpenTelemetry variables it read.** An `otlp_endpoint` from
  `OTEL_EXPORTER_OTLP_ENDPOINT`, a `service_name` from `OTEL_SERVICE_NAME` and a `cache_dir` from `XDG_CACHE_HOME`
  were printed as `default` under the `HAJER_*` variable. The row now shows the variable the value came from, with
  source `env`.
- **No false "OpenTelemetry SDK is not installed" at exit.** After a `configure()`, the exit hook built a new
  exporter at interpreter exit just to flush it, and any failure there was logged as a missing package. The exit
  hook now flushes and stops only the exporter already in use, and an exporter that fails to build is reported as
  such (`hajer emits no spans: its OpenTelemetry exporter could not be built (<exception class>).`), keeping
  "not installed" for an actual missing `hajer[otel]`.

## [0.2.1] - 2026-10-05

### Changed

- **The documentation moved to [docs.hajer.ai](https://docs.hajer.ai).** `docs/reference.md`, `docs/evals.md`,
  `docs/evals-engine.md` and `docs/support-matrix.md` are gone from this repository; the PyPI README is a short
  landing page that links there, and `scripts/generate_support_matrix.py` writes the support matrix into the docs
  site with `--out`. Contributor notes are in `CONTRIBUTING.md`. The old `docs/` paths keep a one-line pointer
  each, so links from the 0.2.0 PyPI page still land somewhere.

## [0.2.0] - 2026-10-04

**`verify` and `observe` are removed.** The package is now the tracing SDK for the Hajer platform and the
`hajer eval` runner: it records every model call and exports traces over OpenTelemetry, and reports CI eval
runs against the repository's `hajer.yaml`. The platform routes the 0.1.0 client called no longer exist.

### Added

- **Traces leave for the platform by default.** With `HAJER_API_KEY` and `HAJER_TEAM_ID`, every span the SDK
  emits goes to the platform's OTLP/HTTP receiver for the team (`/api/teams/{team_id}/otel/v1/traces`), on the
  OpenTelemetry provider the process already has — beside its own exporters — or on one the SDK builds and
  never installs globally. `HAJER_OTLP_ENDPOINT` names a collector of your own instead.
- **Every recorded model call is a `gen_ai` span**, in the GenAI semantic conventions' words: operation,
  provider, request and response model, request settings, response id and finish reasons, usage, the
  endpoint, the application frame, `error.type` on failure, and — with content capture — the messages, the
  answer and the system instructions as `gen_ai.input.messages` / `gen_ai.output.messages` /
  `gen_ai.system_instructions` in the conventions' parts shape, for OpenAI chat and Responses, Anthropic,
  google-genai and litellm alike, redacted first and bounded. Emitted when the call settles, backdated to
  when it opened, under the span that was current then. Suppressed for a call another instrumentation's
  `gen_ai` span already covers; `HAJER_MODEL_SPANS=0` turns them off.
- **`hajer.session`, `hajer.user`, `hajer.context`**: the conversation, declared once (a block or a
  decorator) and carried as `session.id`, `user.id`, `hajer.tags` and `hajer.metadata.*` by every span
  inside — the SDK's own and any other OpenTelemetry instrumentation's.
- **`hajer.yaml`**: the repository's suites and the obligations its tests cover, declared at the root.
  `hajer eval` with no `-c` runs every declared suite (`--suite ID` for one), refuses an obligation id the
  manifest does not declare, and names the suite in the payload (`suiteId`, still `schemaVersion: 1`).
- `hajer.configure(settings=, tracer_provider=, policy=)` and `hajer.flush()` are public; `hajer doctor`
  prints whether the OpenTelemetry SDK is installed and where spans would go.
- Settings: `HAJER_TRACES_ENABLED`, `HAJER_MODEL_SPANS`, `HAJER_OTLP_HEADERS`, `HAJER_SERVICE_NAME`,
  `HAJER_TRACE_EXPORT_TIMEOUT_MS`, `HAJER_TRACE_BATCH_DELAY_MS`, `HAJER_TRACE_QUEUE_MAX`,
  `HAJER_TRACE_BATCH_MAX`, `HAJER_EVAL_UPLOAD_BACKOFF_INITIAL_MS` / `_MAX_MS`.

### Changed

- `hajer eval --upload` posts to `/api/teams/{team_id}/eval-runs`; the platform places a run by the
  repository its git context names. `--project-id` and `HAJER_PROJECT_ID` are gone.
- `attach()` and `instrument()` no longer take a `transport`; `instrument()` no longer takes a
  `tracer_provider` (pass it to `hajer.configure`). `detach()` stops instrumenting new clients; a client
  already built keeps emitting, and `HAJER_DISABLED` / `HAJER_TRACES_ENABLED=0` is what stops export.
- `HAJER_WRAPPED_CALL_MAX_BYTES` bounds one call's content and the three content attributes of its span;
  `HAJER_BODY_MAX_BYTES` bounds only the raw response body buffered to read an answer.
- The eval payload's spans also carry the `session.*`, `user.*`, `server.*`, `error.*` and `code.*`
  attributes; `pyyaml` joins the `evals` extra.

### Removed

- `hajer.Hajer`, `hajer.AsyncHajer`, `verify`, `observe`, `observations`, the assessment and observation
  models, `AssessmentUnavailableError`, `record_boundaries`, `observe_sink` / `HAJER_OBSERVE_SINK`,
  `wire_source`, `python -m hajer tail`, `python -m hajer proxy`, `hajer doctor --emit`, the vendored
  OpenAPI contract and its generator, the OpenTelemetry *receiver* (`instrument(tracer_provider=…)`) that
  turned other tools' spans into observations, and the raw-capture and reply-read switches
  (`HAJER_CAPTURE_RAW`, `HAJER_RECORD_REPLY_READS`, `HAJER_BOUNDARY_*`, `HAJER_PROXY_TIMEOUT_S`,
  `HAJER_TAIL_*`, `HAJER_DEADLINE_MS_DEFAULT`, `HAJER_OBSERVE_*`, `HAJER_PROJECT_ID`).

## [0.1.0] - 2026-10-04

First public release. Python 3.11, 3.12 and 3.13.
