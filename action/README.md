# Hajer suites in GitHub Actions

The Action installs the `hajer[ci]` package from the same commit of this repository, runs the
committed Hajer suites against your application in CI, and appends a per-check table to the job
summary. Install your application's own dependencies before invoking it. What the underlying pytest
plugin does is described in [`python/docs/ci.md`](../python/docs/ci.md).

A complete workflow:

```yaml
name: Hajer suites
on: [pull_request]

jobs:
  hajer:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install the application
        run: pip install -r requirements.txt
      - uses: HajerAI/hajer-sdk/action@YOUR_PINNED_COMMIT
        with:
          upload: 'true'
        env:
          HAJER_API_KEY: ${{ secrets.HAJER_API_KEY }}
          HAJER_TEAM_ID: ${{ vars.HAJER_TEAM_ID }}
          HAJER_PROJECT_ID: ${{ vars.HAJER_PROJECT_ID }}
```

Pin the Action to a full commit SHA. Inputs:

| Input | Default | Meaning |
| --- | --- | --- |
| `suites-dir` | `.hajer/suites` | The committed suite directory in your checkout. |
| `live` | `'false'` | Opt in to metered provider calls. Requires `HAJER_CI_BUDGET_MICROUSD` and the provider key. |
| `blocking` | `'false'` | Opt in to failing the job for code-check or execution failures. Model judgments remain advisory. |
| `upload` | `'false'` | Upload compact receipts using `HAJER_API_KEY`, `HAJER_TEAM_ID` and `HAJER_PROJECT_ID`. |
| `artifact-name` | generated | A unique name for the suite artifact; the default is unique per invocation. |

Without `upload: 'true'`, replay needs no Hajer key. Results are always written to
`$RUNNER_TEMP/hajer-suite-results.json`. Upload stores compact IDs, verdicts,
labels, timings and `GITHUB_SHA` under the team's existing evaluation project.
No request, application output or recording is uploaded. Upload errors preserve
the local receipt and mark the suite step failed; blocking is opt-in. Compact
results and the content-free provider ledger are also retained as job artifacts,
including on failures when Actions can run cleanup. This action does not publish a PR comment.
The suite artifact name is unique per invocation, including matrix jobs. Set
`artifact-name` only when a fixed downstream name is needed; it must remain unique within the run.

Hajer publishes `.hajer/suites/*.json` and scanned execution bindings under
`.hajer/executions/`. The bindings name the exact suite version and file digest;
stale or unavailable inputs remain unverified. You do not write these tests
or label outputs. Recorded answers can replay an app path without spending, but
new inputs need live execution to obtain a fresh model answer.

Set `live: 'true'` (CLI `--hajer-live`) to use the application's own provider
credentials, and set `HAJER_CI_BUDGET_MICROUSD` to the permitted per-job ceiling.
The action supplies a local ledger path. For a direct CLI run also set
`HAJER_CI_BUDGET_FILE` to a writable private path outside the repo. The install
workflow Hajer generates for your repository uses repository variables `HAJER_LIVE=true` and
`HAJER_CI_BUDGET_MICROUSD`, plus the application's provider secret. Fork pull
requests stay in free replay mode.

Before every supported provider send, the SDK reserves the whole model context
at its most expensive cache tier plus the output-token limit. This is
conservative: a Sonnet 5 request needs a little over $4 remaining to start even
when its eventual bill is cents. The ledger then settles input, output and cache
usage at dated list prices. Errors, timeouts and missing usage keep the reserve;
they are never charged as zero. Every case and repeat shares the same cap. A new
GitHub job has a new cap; this is not an account-wide monthly budget. Reusing the
ledger cannot increase or reset its ceiling.

Current priced models are Claude Haiku 4.5 (`claude-haiku-4-5-20251001`), Sonnet 5,
Opus 5.5 and GPT-5.1. If a GPT-5.1 request omits its optional output limit, the
SDK reserves its published 128,000-token maximum, with the source and verification
date in the local receipt; it does not change the request. Other models still
need an explicit output limit until their maximum has been verified. Invalid
explicit limits, unknown models, paid server tools,
priority/flex tiers, fast inference and regional price modifiers are refused
before dispatch. The normal OpenAI/Anthropic HTTP request and SSE response forms
are metered. List-price estimates and uncertain reserved amounts appear in the
local result file and CI summary; they are not yet ingested by Hajer's
billing. The provider invoice remains the billing authority.

Each attempt runs in a fresh process. Its bytes may reach one place: the
`.hajer/replay.toml` `[provider]` (`host`, `port`, and `scheme` when it is not the
port's own, https on 443 and http otherwise), else the OpenAI and Anthropic APIs, and
only a socket connected to an address that provider's name resolved to, at its exact
port. A CI proxy (`HTTP(S)_PROXY`) or client `proxy=` that is not the provider, a Unix
socket or a transport's own socket is refused at the connect and named, so the attempt
is `UNABLE_TO_VERIFY`; a metered proxy declared as the `[provider]` is the provider.
A provider answer counts toward `providerSuccesses` only when it came over a connection
admitted to that provider, per send. Known limits: the guard runs inside the application's
process on CPython's `socket` layer, so uvloop and native network extensions (grpcio,
pycurl, librdkafka) connect unseen — a provider request that way fails closed afterwards,
other traffic that way is not refused — and code that deliberately tampers with the guard
is not resisted ([`python/docs/ci.md`](../python/docs/ci.md#the-egress-guard)). A case repeats by Hajer's
qualification rule (`contract/qualification-vectors.json`):
PASS and FAIL count, UNABLE_TO_VERIFY spends an attempt without counting, three
agreeing counted attempts are `qualified` (three FAILs are a qualified FAIL), PASS
and FAIL together are `flaky`, anything else is `not_yet`, and the case repeats
while any of its checks is `not_yet`, at most five attempts. Labels do not add a
separate gate or claim a statistical confidence level. A check's verdict is the one
its label settled on: PASS or FAIL when `qualified`,
UNABLE_TO_VERIFY when `not_yet` or `flaky`. Every attempt stays in the receipt, so a
qualified PASS over UNABLE_TO_VERIFY, PASS, PASS, PASS still lists the unknown one
and its reason. Replay runs once on fixed recorded answers, so its single read is
deterministic and is the check's verdict (PASS, FAIL or UNABLE_TO_VERIFY), while its
label stays `not_yet`: labels describe live repeated behaviour. The receipt and the
job summary say `mode=replay`, so a replay PASS is never read as a qualified PASS.
These are processes you control, not a hosted security sandbox.

The action is advisory by default. Set `blocking: 'true'` (or repository variable
`HAJER_BLOCKING=true` for the install workflow Hajer generates) to let code-check and
execution failures fail the job. Model judgments stay advisory in either mode.
The underlying pytest result and retained receipt keep failures and unverified
states even when the job is allowed to continue; an advisory job's completion is
not evidence that the AI passed. `run-suites.sh` runs the suites apart from your own pytest
setup: `PYTEST_ADDOPTS`, `PYTEST_PLUGINS`, your ini (`addopts`,
`xfail_strict`, `required_plugins`), auto-loaded plugins such as pytest-rerunfailures
or pytest-xdist and `conftest.py` files are not used, and only the suite directory
is collected. When the plugin runs inside your own pytest, it still refuses
a second run of a case (no re-run plugin can replace a first result or spend more
than five live attempts), ignores xfail markers on its cases and fails the session
on any check that is not PASS, whatever the item outcomes say. It refuses to run
under pytest-xdist (`-n`), which would split one receipt across workers.

The SDK's pytest11 entry point loads the plugin into every pytest where `hajer` is
installed, but it is inert unless `--hajer-suites` is given: your ordinary
`pytest` collects no Hajer case, keeps its own outcomes and exit status, and writes
or uploads nothing.

Every application process a suite run starts, replay or live, runs with
`HAJER_ENVIRONMENT=ci` (overriding whatever your CI exports), and
`run-suites.sh` exports it too, so CI traffic is tagged `ci` and never selected as
a test input.

CLI example: `pytest -p hajer.pytest_plugin --hajer-suites .hajer/suites
--hajer-results .hajer/results.json`. Add `--hajer-upload --hajer-project-id UUID`
to send the compact receipt using the normal SDK environment variables.
