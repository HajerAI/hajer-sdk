# The eval engine

`hajer eval` runs [promptfoo](https://github.com/promptfoo/promptfoo), unmodified, at one exact version. This
file is the record of that pin: what is pinned, where it runs, what `hajer eval` switches off, and how to move it.

## What is pinned

| | |
|---|---|
| Package | `promptfoo` on the npm registry (MIT) |
| Version | the `dependencies.promptfoo` entry of [`hajer/evals/engine/package.json`](../hajer/evals/engine/package.json) — exact, no range |
| Lockfile | [`hajer/evals/engine/package-lock.json`](../hajer/evals/engine/package-lock.json), lockfile version 3, every transitive package with its integrity hash |
| Node | `>= 22.22.0` (`engines.node` in the same file; what promptfoo itself requires) |

Both files ship inside the `hajer` wheel as package data and are the single source of truth: the Python side
reads the version from them at runtime and never spells it. `tests/test_evals_engine.py` fails when the two
files disagree, and the golden test fails when the installed engine reports a different version.

Hajer ships no JavaScript. Everything it adds — the reserved `metadata.hajer` namespace, the span emitter, the
`beforeAll` hook, the payload and the upload — is Python, reached through promptfoo's public extension points:
Python extension hooks, a second `-c` configuration, the OTLP trace receiver, `context.traceparent` handed to
providers, and the `--output` results document. A fork would add nothing but merge work.

## Where it runs

On the first `hajer eval` (or `hajer eval --install-only`) the engine is installed with `npm ci` into

```
$HAJER_CACHE_DIR/engine/<first 16 hex of sha256(package-lock.json)>/
```

(`~/.cache/hajer` by default, `$XDG_CACHE_HOME/hajer` when that is set). A directory is keyed by the lockfile,
so a bumped pin installs beside the old one and a rollback finds its install still there. A `.ready` file
holding the version marks a finished install; without it, or with another version in it, `npm ci` runs again
under a file lock so two CI shards cannot install over each other. The install needs the npm registry once;
`actions/cache` on `~/.cache/hajer/engine` keyed by the lockfile's hash makes later CI runs offline.

Each run gets its own directory, `$HAJER_CACHE_DIR/runs/<run id>/`, holding the overlay config, promptfoo's own
config directory (its SQLite results and trace store, never `~/.promptfoo`), the hook report, `results.json`
and `payload.json`. The newest `HAJER_EVAL_RUNS_KEEP` runs are kept.

## What `hajer eval` sets in the engine's environment

Promptfoo contacts its vendor by default; `hajer eval` turns every such path off and the golden test proves,
through a recording loopback proxy (`NODE_USE_ENV_PROXY=1`), that a run reaches no host but `127.0.0.1`.

| Variable | Value | Why |
|---|---|---|
| `PROMPTFOO_DISABLE_TELEMETRY` | `1` | product analytics (`src/telemetry.ts`) |
| `PROMPTFOO_DISABLE_UPDATE` | `1` | the version check (`src/updates.ts`) |
| `PROMPTFOO_DISABLE_SHARING` | `1` | result sharing to promptfoo's cloud |
| `PROMPTFOO_DISABLE_REMOTE_GENERATION`, `PROMPTFOO_DISABLE_REDTEAM_REMOTE_GENERATION` | `1` | remote test generation |
| `PROMPTFOO_DISABLE_SHARE_EMAIL_REQUEST` | `1` | the share-by-email prompt |
| `NO_UPDATE_NOTIFIER` | `1` | npm's own notifier during the install |
| `PROMPTFOO_CONFIG_DIR` | `<run dir>/promptfoo` | results and traces stay with the run |
| `PROMPTFOO_CACHE_PATH` | `$HAJER_CACHE_DIR/promptfoo-cache` | promptfoo's provider cache, shared across runs |
| `PROMPTFOO_PYTHON` | the interpreter running `hajer eval` | the hook and Python providers import the same `hajer` |
| `HAJER_OTLP_ENDPOINT` | `http://127.0.0.1:<port>` | the emitter exports to the engine's receiver (`/v1/traces`). The generic `OTEL_EXPORTER_OTLP_ENDPOINT` is deliberately not set: promptfoo's own OpenTelemetry SDK reads it as a complete URL and would post to the receiver's root |
| `HAJER_ENVIRONMENT` | `eval` | every span and observation says it came from an eval |
| `HAJER_EVAL_RUN_ID`, `HAJER_EVAL_WORKFLOW`, `HAJER_EVAL_OBLIGATIONS`, `HAJER_EVAL_HOOK_REPORT` | the run's | read back by the hook and the emitter |
| `HAJER_API_KEY` | **removed** | the application under test is inert towards Hajer; the parent uploads |

Three things to know about the engine itself:

- Its exit code is `100` when any test fails (`PROMPTFOO_FAILED_TEST_EXIT_CODE`), and its pass rate counts
  errors in the denominator. `hajer eval` returns the engine's code unchanged and reports `failed` and
  `errored` rows separately in the payload.
- **It still reaches three hosts on its own**, and the golden test pins exactly these (`KNOWN_ENGINE_EGRESS` in
  `tests/test_evals_golden.py`); any other host fails the test:
  - `r.promptfoo.app` — with telemetry disabled, promptfoo sends one `telemetry disabled` event
    (`src/telemetry.ts`, `recordTelemetryDisabled`); that endpoint is not behind the disable flag;
  - `169.254.169.254` and `metadata.google.internal` — cloud-identity probes two bundled provider SDKs make at
    start-up (`openai`'s subject-token providers and `@azure/msal-common`'s IMDS client), whatever providers
    the suite uses.
  All three carry no data about the suite, and refusing them (the test does, through the proxy) changes
  nothing about the run. They cannot be switched off without forking the engine; a run on an air-gapped
  machine logs them as unreachable and proceeds.
- A config whose `prompts` is a map (`prompts: {label: ...}`) cannot be combined with the overlay's empty list
  (promptfoo refuses to mix the two shapes); use the list form, which every promptfoo example uses.

## Moving the pin

```bash
cd python
just evals-pin 0.124.0            # rewrites package.json, regenerates package-lock.json (needs node + npm)
just test --engine -m engine      # the golden test against the new engine (installs it into the cache)
just check
```

Then note the bump under `[Unreleased]` in `CHANGELOG.md` and release: the pin changes only with a `hajer`
release, so one package version names one engine version. `--package-lock-only` records every platform's
optional packages, which is what lets a lock made on macOS install under `npm ci` on Linux CI.
