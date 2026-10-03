# Running Hajer suites in CI

`hajer[ci]` adds a pytest plugin that runs your team's committed Hajer suites against your application,
writes a compact receipt per run, and can upload it to your team. In GitHub Actions the
[Hajer suites action](https://github.com/HajerAI/hajer-sdk/blob/main/action/README.md) installs the
package and runs the plugin for you; this page describes what it does underneath, and how to run it
yourself.

## Running the plugin

```bash
pip install "hajer[ci]"
pytest -p hajer.pytest_plugin --hajer-suites .hajer/suites
```

To run one workflow, pass its exact JSON file to `--hajer-suites`; only that workflow appears in the run
receipt and summary.

The `pytest11` entry point loads the plugin into every pytest where `hajer` is installed, but without
`--hajer-suites` it does nothing: your ordinary `pytest` collects no Hajer case, keeps its own outcomes
and exit status, and writes or uploads nothing.

| Option | Meaning |
| --- | --- |
| `--hajer-suites PATH` | The suite directory, or one suite's JSON file. Required to do anything. |
| `--hajer-live` | Use the application's own provider credentials for fresh model answers (metered; see below). Without it, cases replay recorded answers and spend nothing. |
| `--hajer-results PATH` | The compact local receipt. Default `.hajer/results.json`. |
| `--hajer-summary PATH` | Append a Markdown summary to this file (the Action points it at the job summary). |
| `--hajer-upload --hajer-project-id UUID` | Upload the compact receipt with the normal `HAJER_API_KEY` / `HAJER_TEAM_ID`. |
| `--hajer-fixed-repeats N` | Optional fixed-repeat measurement after the ordinary run; see [below](#optional-fixed-repeat-measurement). |

`python -m hajer upload-results PATH --project-id UUID` uploads a receipt `--hajer-results` already wrote;
it is the only CI step that needs `HAJER_API_KEY`. `python -m hajer verify-adapters` calls each
`.hajer/replay.toml` adapter once with a fake model and reports whether it reaches its declared site.

The suites (`.hajer/suites/*.json`) and the execution bindings (`.hajer/executions/`) are published into
your repository by Hajer; the bindings name the exact suite version and file digest. You do not write
these tests or label outputs. The Action's README describes the committed format.

## Settings

Used only by `hajer.pytest_plugin` and `python -m hajer verify-adapters`:

| Environment | Default | Meaning |
|---|---|---|
| `HAJER_CI_FIXED_REPEAT_LIMIT` | 20 | Maximum explicitly requested extra fixed measurement attempts per case. This cap never enables collection; use `--hajer-fixed-repeats N`. The default supports small repeat studies without changing ordinary qualification's five-attempt cap. |
| `HAJER_CI_CASE_TIMEOUT_SECONDS` | 60 | Per-attempt process deadline, matching the SDK proxy deadline. |
| `HAJER_CI_BUDGET_MICROUSD` | 0 | Provider-spend ceiling shared by all cases and repeats. Zero admits no paid requests. One dollar is 1,000,000 microUSD. |
| `HAJER_CI_BUDGET_FILE` | absent | Local SQLite ledger for that ceiling. Required for live dispatch; reuse it to retain reservations across restarts. Never commit it. |
| `HAJER_CI_INPUT_DEADLINE_MS` | 30000 | Deadline for retrieving approved private traffic inputs in CI. No automatic retry. |
| `HAJER_CI_INPUT_MAX_BYTES` | 1048576 | Maximum private input response bytes held by the CI parent. Over-limit responses refuse execution. |
| `HAJER_ADAPTER_CHECK_ANSWERS` | 16 | `python -m hajer verify-adapters`: fake model answers each model endpoint serves one adapter's call; a call past them is refused, and the check says so. |
| `GITHUB_SHA` | absent | Commit identity retained on CI receipts; absent stays unknown. |
| `HAJER_CI_COMMIT_SHA` | absent | Fallback commit identity when `GITHUB_SHA` is absent. |
| `HAJER_CI_BRANCH` | absent | Fallback branch name when `GITHUB_HEAD_REF` and `GITHUB_REF_NAME` are absent. |

## Verdicts, labels and repeats

A live case repeats by Hajer's qualification rule, the same one the platform applies
([`contract/qualification-vectors.json`](../../contract/qualification-vectors.json)): PASS and FAIL
count, UNABLE_TO_VERIFY spends an attempt without counting, three agreeing counted attempts are
`qualified` (a qualified FAIL too), both PASS and FAIL are `flaky`, and at most five attempts run. The
rule is fixed, not a setting.

A live check's verdict is the one its label settled on: PASS or FAIL when `qualified`, UNABLE_TO_VERIFY
otherwise. A replay's single read on fixed recorded answers is its verdict, with the label left
`not_yet` and the receipt marked `mode: replay`. Every attempt is kept in the receipt, and the run is
green only when every verdict is PASS.

The application process of every case runs with `HAJER_ENVIRONMENT=ci`, so CI traffic is never selected
as a test input.

## The egress guard

Each case runs in a guarded child process. A replay sends nothing. A live run (`--hajer-live`) sends only
to the model provider: to an address its name resolves to, at its exact port. Anything else is refused
and the attempt fails closed — another host, a proxy that is not the provider, a Unix socket, a database
driver, a process. Success is counted per send: a provider answer counts only when it came over a
connection the guard admitted to that provider.

**Known limits of the guard.** It is a CPython audit hook and patched transports inside the
application's own process. Two things are outside what it can hold:

- **Networking below Python's `socket` module.** uvloop (libuv connects without an audit event) and
  extensions with their own sockets (grpcio, pycurl, librdkafka) are not seen when they connect. In a
  live run, bytes sent that way can leave before the guard knows. A provider request that went that way
  is refused afterwards as `unconnected`, so the attempt still fails closed, but other traffic sent that
  way is neither refused nor logged. Run live suites without uvloop and without native network clients
  in the path. Hajer's hosted replay adds an operating-system sandbox, which does hold.
- **Deliberate tampering with the guard's own objects.** Code that replaces a `Sandbox` method, or a
  function of `hajer.replay`, can defeat it. The guard holds against an application's ordinary
  configuration and libraries — environment proxies, `proxy=`, `uds=`, custom transports, DNS answers
  that change — not against code written to disable it.

These are processes you control, not a hosted security sandbox.

## Spend

Live runs spend your provider credentials, so they are bounded by `HAJER_CI_BUDGET_MICROUSD` and the
ledger in `HAJER_CI_BUDGET_FILE`. Before every supported provider send, the SDK reserves the whole model
context at its most expensive cache tier plus the output-token limit, then settles at the dated list
price once usage is known. Errors, timeouts and missing usage keep the reserve. The Action's README has
the full rules, the priced models and what is refused before dispatch.

## Optional fixed-repeat measurement

```bash
pytest -p hajer.pytest_plugin --hajer-suites .hajer/suites --hajer-live \
  --hajer-fixed-repeats 5 --hajer-project-id PROJECT
```

adds **five extra fresh application attempts per runnable case after the ordinary tests finish**. This
is off by default. It requires an explicit nonzero `HAJER_CI_BUDGET_MICROUSD`, `HAJER_CI_BUDGET_FILE`,
`HAJER_TEAM_ID`, and commit identity (`GITHUB_SHA` or `HAJER_CI_COMMIT_SHA`). The ordinary and extra calls
share the same spend ledger; no automatic budget increase occurs.

The ordinary adaptive qualification result and CI policy stay unchanged. Measurement failures are
recorded separately. Extra calls run after the ordinary receipt is saved, so they cannot spend a later
ordinary test's budget. An interrupted pytest session starts no extra calls.

The terminal names a private `results-repeats-RUN_ID/` directory beside the results file. Each selected
case has a prospective manifest; every check has an immutable `.plan.json` and a current
`.measurement.json` before extra execution starts. Snapshots update atomically after each attempt. Keep
these files as CI artifacts, not repository test inputs. They contain identities, counts, verdicts, times
and hashes, never application inputs or outputs. Missing attempts remain in the fixed plan; timeouts,
budget refusals and changed request configurations are UNKNOWN. If a write fails, collection stops rather
than making another unrecorded paid call.

Each `.measurement.json` is read by the Hajer platform's offline repeat-statistics analysis. The SDK uses
the ordinary run's observed request fingerprint as the frozen reference. A different request sequence
cannot receive a settled measurement verdict. This comparison excludes credentials; it does not
establish provider independence, model-version stability, or exchangeability. Therefore SDK measurements
declare `NOT_ESTABLISHED`: descriptive pass counts and disagreement are available, but pass@k inference
remains unavailable.

In a tool-using workflow, a different model answer may legitimately change a later request. Such a
trial is recorded as `UNKNOWN / REQUEST_CONFIGURATION_CHANGED`, not removed from the planned
denominator. Settled-only pass counts therefore describe the comparable subset, not the reliability of
the whole workflow; they do not establish independent or identically distributed trials.

Only existing deterministic checks are evaluated on extra attempts. No additional model graders or
metamorphic calls are authorised by this switch. Checks needing those calls remain UNKNOWN; a case
containing only such checks incurs no extra calls. No reference request, missing adapter, or unexecuted
case leaves its measurement empty with planned attempts still visible. A killed process may leave
partial snapshots; never interpret absent attempts as passing. The files are caller-produced evidence,
not independent attestation that a provider call happened.
