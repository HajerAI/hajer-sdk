# Evals: `hajer eval`

`hajer eval` runs ordinary [promptfoo](https://www.promptfoo.dev/docs/configuration/guide/) configurations
on a pinned copy of promptfoo and connects every result to the same stable identity the production SDK emits:
which workflow a test exercised, which obligations it covers, which components and tools actually ran, and
which commit it ran against. Nothing about promptfoo's format changes. The additions are a manifest naming the
repository's suites and obligations, a reserved metadata namespace, three decorators, one provider helper, and
a payload.

```bash
pip install "hajer[evals]"     # Node >= 22.22 on PATH; the engine installs itself on first run
hajer eval                     # runs every suite hajer.yaml declares — or ./promptfooconfig.yaml without one
```

A worked example lives in [`examples/support/`](../examples/support/).

## What a repository declares: `hajer.yaml`

One file at the repository root names the suites and the obligations. `hajer eval` reads it to know what to
run and which obligation ids a test may name; the Hajer platform reads the same file through the repository's
GitHub connection, so the suites, their tests and the obligations are on the platform before the first run is
uploaded.

```yaml
version: 1
suites:
  - id: support                               # ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$, unique
    path: evals/support/promptfooconfig.yaml   # relative to this file, inside the repository
    description: Customer support regression   # optional
obligations:
  - id: obl_refund_status_disclosed            # the same id pattern, unique
    title: Refund status is disclosed accurately
    workflow: wf_support                       # the workflow the obligation is about
    description: ...                           # optional
```

The schema is strict — an unknown key, a duplicate id, a path outside the repository, a suite file that does
not exist — and a refusal names the file and the key (exit code 2). The file is found in the working directory
or any directory above it, so `hajer eval` runs the same from a service's own directory as from the root.

## What a test declares

Use promptfoo's own `metadata`. Two keys matter:

```yaml
defaultTest:
  metadata:
    hajer:
      workflowId: wf_support          # inherited by every test; a test may override it

tests:
  - description: Pending refund is reported as pending
    metadata:
      testCaseId: eval_refund_pending  # promptfoo's own stable id; the trace is correlated through it
      hajer:
        obligationIds: [obl_refund_status_disclosed]
        componentIds: [cmp_refund_agent]       # optional: what the test believes it exercises
    vars: { message: Has my refund gone through?, customer_id: cust_123 }
    assert:
      - { type: trajectory:tool-used, value: get_refund_status }
      - { type: llm-rubric, value: The answer must not claim a pending refund completed. }
```

| `metadata.hajer` key | Required | Meaning |
|---|---|---|
| `workflowId` | yes | The logical workflow the test targets (`wf_support`). The same id the application emits with `hajer.workflow`. |
| `obligationIds` | no | The behavioural requirements the test covers. |
| `componentIds` | no | Components the test expects to exercise; the trace says which actually ran. |
| `sourceTraceIds` | no | Production traces this case was derived from. |
| `generatedBy`, `provenance` | no | Who or what wrote the case. |

Three rules, all enforced by the hook that runs before the first provider call:

- A test **without** `metadata.hajer` is an ordinary promptfoo test. It runs unchanged and appears in the
  payload with `correlation: none` — unless `defaultTest.metadata.hajer` names a workflow, in which case every
  test inherits it and is correlated.
- A malformed `metadata.hajer` — a number for `workflowId`, an unknown key such as `workflowID` — stops the
  run with exit code 2 and the offending test named. Typos in a reserved namespace are errors, not silence.
- A valid `metadata.hajer` **without** `metadata.testCaseId` runs, with a warning: correlation then falls back
  to the test's position in the file, which moves when tests are reordered.

`defaultTest.metadata.hajer` is inherited key by key (promptfoo's own merge is shallow; the hook deep-merges
the `hajer` object so a test adding `obligationIds` keeps the inherited `workflowId`).

## What the application emits

```python
import hajer

@hajer.workflow("wf_support")
def handle(message: str, customer_id: str) -> str: ...

@hajer.component("cmp_refund_agent")
def refund_agent(customer_id: str) -> str: ...

@hajer.tool("tool_refund_status", name="get_refund_status")
def get_refund_status(customer_id: str) -> str: ...
```

Each is a decorator (sync or async) and a context manager. They open OpenTelemetry spans carrying
`hajer.workflow.id`, `hajer.component.id` and `hajer.tool.id`; a tool span also carries `gen_ai.tool.name`,
which is what promptfoo's `trajectory:*` assertions match on. The spans are identical in production and under
`hajer eval`; the difference is `deployment.environment = eval` and the `hajer.eval.*` attributes (run id, test
case id, workflow id, obligation ids) that let the platform join a trace to the result it belongs to.

The emitter needs `hajer[otel]` (`hajer[evals]` includes it). Without it the three names still run the code
they wrap, emit nothing, and warn once. With an OpenTelemetry SDK provider already configured in the process,
that provider and its exporters are used; otherwise spans go to `HAJER_OTLP_ENDPOINT` (or the standard
`OTEL_EXPORTER_OTLP_ENDPOINT`) when set; `hajer eval` sets the former to the engine's loopback receiver.

`hajer.workflow(id)` also opens `hajer.scope(workflow=id)`, so model calls recorded by `wrap` group under the
same identity.

## The provider

promptfoo reaches the application through a provider; `hajer.evals.provider` is the one line that binds the
test to the trace:

```python
import hajer.evals
from app import handle

@hajer.evals.provider
def call_api(prompt, options, context):
    return {"output": handle(context["vars"]["message"], context["vars"]["customer_id"])}
```

It reads the `traceparent` the engine minted for the test, the test case id and the `metadata.hajer` ids from
`context`, makes the application's spans children of that trace, and flushes them before returning so the
trajectory assertions see the trace. `hajer.evals.bind(context)` is the same thing as a context manager.

## The command

```
hajer eval [-c PATH ...] [--suite ID] [--workflow ID] [--obligation ID ...] [--upload]
           [--payload-out PATH] [--install-only] [--engine-help] [any promptfoo eval flag]
```

- With no `-c` and a `hajer.yaml`, every declared suite is one engine run, one payload and one upload, from the
  manifest's directory; `--suite ID` runs one of them. Without a manifest, promptfoo's own `promptfooconfig.*`
  in the working directory is used.
- With `-c`, only the named files run; the manifest, when there is one, still says which obligation ids a test
  may name. `--suite` and `-c` cannot be combined.
- `--workflow` / `--obligation` keep only the tests whose metadata matches (both filters AND; a plain test never
  matches a filter). The filter is applied in the hook, because promptfoo's `--filter-metadata` reads top-level
  keys only. An `--obligation` the manifest does not declare is refused before the engine starts.
- Every other flag goes to `promptfoo eval` unchanged: `--no-cache`, `-j 4`, `--filter-pattern`, `--repeat`, …
- The payload is always written to the run directory (`~/.cache/hajer/runs/<run id>/payload.json`), and to
  `--payload-out` as well when given: a file for one suite, a directory of `<suite id>.json` for several.
- `--upload` sends each payload to `POST /api/teams/{team}/eval-runs` with the team key; the platform places
  the run by the repository the payload's git context names. Without credentials the upload is reported
  `skipped`; a refused or unreachable upload is reported `failed` on stderr. **Neither changes the exit code.**

Exit codes: the engine's, unchanged — `0`, or `100` when a test fails (the worst of the suites, for a manifest
run) — plus the runner's own: `2` usage (no configuration, an invalid manifest, invalid `metadata.hajer`, an
obligation the manifest does not declare, no test matched a filter), `3` Node or npm missing or older than
22.22, `4` the engine could not be installed or smoke-tested, `5` the engine finished but wrote no readable
results.

## The payload

One JSON document per run, `schemaVersion: 1`, written as `payload.json` and uploaded as the request body:

```
schemaVersion, runId, createdAt, status (passed | failed | errored | aborted), engineExitCode
engine { name: promptfoo, version, lockfileDigest, nodeVersion, evalId }
sdk { version }
git { commitSha, branch, dirty, remoteUrl, ci { provider, runId, prNumber, baseRef, headRef, repository } }
suiteId                          # the suite's id in hajer.yaml; null for a bare -c
config { path, description, providerIds[] }   # path relative to hajer.yaml when it drove the run
filters { workflowId, obligationIds[] }
stats { total, passed, failed, errored, durationMs, tokenUsage, cost }
warnings[]
results[] {
  testCaseId, testIdx, promptIdx, correlation (platform | none),
  workflowId, obligationIds[], componentIds[], sourceTraceIds[], generatedBy, provenance,
  description, provider { id, label }, outcome (passed | failed | errored), score, error,
  assertions[] { type, metric, passed, score, reason, weight },
  latencyMs, tokenUsage, cost, traceId, evaluationId,
  output,                        # redacted with the client-side catalog, clipped to HAJER_EVAL_OUTPUT_MAX_CHARS
  spans[] { spanId, parentSpanId, name, startTime, endTime, status, attributes },   # startTime/endTime: epoch milliseconds
                                   # attributes: hajer.*, gen_ai.*, deployment.*, tool.*, session.*, user.*, server.*, error.*, code.*
  spanSummary { count, errorCount, toolNames[], componentIds[], workflowIds[] }
}
```

The JSON schema is `hajer.evals._payload.payload_json_schema()`. The run id is minted before the engine
starts and used as the upload's idempotency key, so a retried CI step stores one run.

## Settings

| Variable | Default | What it is, and when to change it |
|---|---|---|
| `HAJER_CACHE_DIR` | `$XDG_CACHE_HOME/hajer`, else `~/.cache/hajer` | Where the engine is installed and runs are kept. Point CI's cache action at `<dir>/engine`. |
| `HAJER_OTLP_ENDPOINT` | `OTEL_EXPORTER_OTLP_ENDPOINT`, else absent | Where the emitter exports when the process has no OpenTelemetry provider of its own. `hajer eval` sets it. |
| `HAJER_TRACE_FLUSH_TIMEOUT_MS` | `2000` | How long `flush()` waits for spans to leave before a provider answers; it also bounds one export request to the endpoint, so an unreachable collector costs at most this much at exit. |
| `HAJER_EVAL_OTLP_PORT` | `4318` | The loopback port the engine's receiver listens on; a busy port is replaced by a free one. |
| `HAJER_EVAL_INSTALL_TIMEOUT_S` | `600` | How long one engine install may take. |
| `HAJER_EVAL_RUNS_KEEP` | `20` | Run directories kept under the cache. |
| `HAJER_EVAL_UPLOAD_ATTEMPTS` | `3` | Sends of one payload before the upload is reported failed (5xx and transport errors retry; 4xx does not). |
| `HAJER_EVAL_UPLOAD_MAX_BYTES` | `8388608` | The payload ceiling. Past it spans are dropped, then outputs, before the upload is refused locally. |
| `HAJER_EVAL_UPLOAD_DEADLINE_MS` | `30000` | The deadline of one upload request. |
| `HAJER_EVAL_OUTPUT_MAX_CHARS` | `4096` | Characters of a result's output kept in the payload. |
| `HAJER_EVAL_SPANS_MAX` | `256` | Spans of one result's trace kept in the payload; the summary counts them all. |
| `HAJER_EVAL_GIT_TIMEOUT_S` | `5` | How long one `git` read for the run's commit context may take; a slow one means no context, not a hang. |
| `HAJER_EVAL_UPLOAD_BACKOFF_INITIAL_MS` / `_MAX_MS` | `200` / `30000` | The doubling wait between upload attempts, and its ceiling. |
| `HAJER_EVAL_RUN_ID`, `HAJER_EVAL_WORKFLOW`, `HAJER_EVAL_OBLIGATIONS`, `HAJER_EVAL_HOOK_REPORT`, `HAJER_EVAL_MANIFEST`, `HAJER_EVAL_MANIFEST_OBLIGATIONS` | set by `hajer eval` | Carried to the engine process for the hook and the emitter. Never set by hand. |

The engine itself — what is pinned, where it installs, what `hajer eval` switches off, how to bump it — is in
[evals-engine.md](evals-engine.md).

## Security

A promptfoo configuration is code-adjacent: `file://` providers, graders and hooks are Python or JavaScript
the engine executes on the machine that runs `hajer eval`, with that machine's environment minus
`HAJER_API_KEY`. Nothing here is sandboxed. Run evals from a pull request the way you would run its tests, and
keep the Hajer key, like any other, in your CI's secrets rather than in the configuration.
