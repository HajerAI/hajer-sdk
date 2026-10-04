# Changelog

All notable changes to the `hajer` Python package. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the package uses [semantic versioning](https://semver.org/);
while the version is `0.x`, a minor release may change the public API.

## [Unreleased]

### Added

- `hajer eval` (`pip install "hajer[evals]"`): run an ordinary promptfoo configuration on a pinned copy of
  promptfoo (installed from npm on first use; Node >= 22.22 on PATH) and produce a versioned payload that
  connects every result to the workflow it targets, the obligations it covers, its trace and the commit it ran
  against. `metadata.hajer` is the reserved namespace (`workflowId`, `obligationIds`, `componentIds`,
  `sourceTraceIds`); `metadata.testCaseId` is the stable case id. `--workflow` / `--obligation` filter the
  suite, `--upload` sends the payload, and an upload failure never changes the exit code.
- `hajer.workflow`, `hajer.component` and `hajer.tool`: decorators and context managers that emit
  OpenTelemetry spans carrying stable ids (`hajer.workflow.id`, `hajer.component.id`, `hajer.tool.id`,
  `gen_ai.tool.name`), identical in production and under `hajer eval`. Behind `hajer[otel]`, which now also
  installs the OTLP/HTTP exporter; a no-op with one warning without it.
- `hajer.evals.provider` / `hajer.evals.bind`: bind a promptfoo Python provider's spans to the row's trace.
- `hajer` as a console script (`python -m hajer` is unchanged), and an `eval engine` line in `hajer doctor`.
- Settings: `HAJER_OTLP_ENDPOINT`, `HAJER_TRACE_FLUSH_TIMEOUT_MS`, `HAJER_PROJECT_ID`, `HAJER_CACHE_DIR` and
  the `HAJER_EVAL_*` family (docs/evals.md).

### Removed

- The CI suites runner: the `hajer.pytest_plugin` pytest plugin and its `hajer[ci]` extra, the guarded
  replay child (`hajer.replay`), the `upload-results` and `verify-adapters` commands of `python -m hajer`,
  the `HAJER_CI_*` and `HAJER_ADAPTER_CHECK_ANSWERS` settings, and the composite GitHub Action at
  `action/`. The application-side SDK (`verify`, `observe`, `wrap`, `scope`, `attach`, `instrument`) is
  unchanged.
- The unpublished TypeScript client (`typescript/`, `@hajer/sdk`). This repository ships one package.

## [0.1.0] - 2026-10-04

First public release. Python 3.11, 3.12 and 3.13.
