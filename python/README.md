# hajer — the Python SDK

Your application's model calls, as traces the Hajer platform stores and shows: every request a trace, every
model call a generation under it, every conversation a session you can read top to bottom. And `hajer eval`:
the repository's promptfoo suites, run on a pinned engine in CI and reported to the platform against the
workflows and obligations they cover.

This package does not run your application, does not sit in your provider path and decides nothing on your
behalf. It records what the application already does and exports it over OpenTelemetry.

## Install

```bash
pip install "hajer[otel]"          # the SDK with the OpenTelemetry exporter: what a production app installs
pip install hajer                  # httpx + pydantic only: records locally, exports nothing (no OTel SDK)
pip install "hajer[evals]"         # `hajer eval`: run promptfoo suites on the pinned engine (Node >= 22.22 on PATH)
pip install "hajer[openai]"        # with the OpenAI SDK alongside; "hajer[anthropic]" likewise
```

The `openai` and `anthropic` extras are a convenience: the SDK never imports either library. `wrap(client)`
instruments the object you hand it, by attribute. The `otel` extra is optional on purpose — this package
installs into **your** environment, and an application pinned to an older OpenTelemetry keeps its pin — but
without it nothing leaves the process, and `hajer doctor` says so.

Python ≥ 3.11, `httpx>=0.28.1`, `pydantic>=2.12`; with `[otel]`, `opentelemetry-sdk>=1.37` and the OTLP/HTTP
exporter.

## Configure

Two environment variables, read once:

| Variable | Required | Meaning |
| --- | --- | --- |
| `HAJER_API_KEY` | yes | Your team API key. |
| `HAJER_TEAM_ID` | yes | The team the traces belong to. |
| `HAJER_BASE_URL` | no | The service. Defaults to `https://api.hajer.ai`; set it only for a local or self-hosted platform. |

```bash
export HAJER_API_KEY=...
export HAJER_TEAM_ID=...
```

**Getting a key.** In the Hajer app, Settings → API keys → Create key. The key is shown once, together with
the team id and the base URL as a `.env` block you can copy as is.

**Inert without a key.** Without `HAJER_API_KEY` and `HAJER_TEAM_ID` — or with `HAJER_DISABLED=1` — the SDK
is inert: every call is still recorded locally, nothing opens a socket, nothing raises. The integration can
land in a repository whose test suite has no Hajer credentials and pass unchanged. A missing key is not a
configuration error.

Set `HAJER_ENVIRONMENT` (`production`, `staging`, `dev`): it is on every span as
`deployment.environment.name`. Every other setting has a default; the full table is in the
[reference](https://github.com/HajerAI/hajer-sdk/blob/main/python/docs/reference.md#settings).

## Quickstart

<!-- The `hajer:quickstart` tag on the fence is extracted and compiled by an external check. Keep the
tag, and keep the block valid Python: a copy kept elsewhere would pass forever while these lines broke. -->

```python hajer:quickstart
import hajer
from openai import OpenAI

hajer.instrument()                      # every provider client built from here on records its model calls
client = OpenAI()                       # (or: client = hajer.wrap(OpenAI()) where you build it)

@hajer.workflow("answer-support-question")      # the unit the platform shows a trace as
def handle_ticket(ticket):
    with hajer.session(ticket.conversation_id), hajer.user(ticket.customer_id):
        reply = client.chat.completions.create(
            model="gpt-5", messages=[{"role": "user", "content": ticket.question}]
        ).choices[0].message.content
    return reply
```

That is the integration. With the two variables set, each `handle_ticket` call is one trace on the platform:
a `workflow answer-support-question` span, a `chat gpt-5` generation under it carrying the messages, the
answer, the model, the tokens and the finish reason, and `session.id` / `user.id` on both — so the
conversation reads as one thread across requests. Three things about those lines:

- **`instrument()` or `wrap(client)`.** `wrap` instruments the client object you hand it, in place;
  `instrument()` patches the provider classes so clients built later are instrumented too. Either is enough.
- **`workflow` is the root.** A model call outside any workflow is still a trace, of one generation; inside
  one, it is a generation under the workflow span, and `hajer.component` / `hajer.tool` describe the steps
  between.
- **`session` is the conversation.** It, `user`, `tags` and `metadata` (`hajer.context(...)`) are declared
  once and carried by every span inside — the SDK's own and any other OpenTelemetry instrumentation's.

## Concepts

**What a model span carries.** The GenAI semantic conventions' own attributes: `gen_ai.operation.name`,
`gen_ai.provider.name`, the request and response model, `gen_ai.request.temperature` and friends,
`gen_ai.response.id` and finish reasons, `gen_ai.usage.input_tokens` / `output_tokens`, and — with content
capture, which is on by default — `gen_ai.input.messages`, `gen_ai.output.messages` and
`gen_ai.system_instructions` in the conventions' message shape, whichever library made the call. Also the
innermost application frame (`code.function.name`, `code.file.path`, `code.line.number`), the provider's
host, and on failure `error.type` with an error status — never the message.

**Where the spans go.** With a key, to the platform's receiver for the team, as OTLP/HTTP; the exporter is
added to the OpenTelemetry provider the process already has, beside its own exporters, or to one the SDK
builds and never installs globally. `HAJER_OTLP_ENDPOINT` names a collector of your own instead. Export is
batched on its own thread and never blocks a call; an unreachable receiver costs at most one export timeout
at exit.

**Client-side redaction** is on by default. Card numbers, IBANs, national ids, credentials, email addresses
and similar shapes are replaced with `[redacted:<CATEGORY>]` in every message, answer and tool argument
before a span carries it. `hajer.configure(policy=hajer.build_policy(...))` adjusts it.

**Attach mode.** For an application nobody has instrumented yet: every provider client the process builds
records and exports its calls, with no code change. It is opt-in:

```bash
PYTHONPATH="$(python -m hajer attach-path)" HAJER_ATTACH=1 python -m your_app   # no code change
```

```python
import hajer.autoattach     # one line in the entry point; reads HAJER_ATTACH, so it is safe to keep
```

**Another instrumentation already there.** The SDK's model span is not emitted for a call another
OpenTelemetry instrumentation already traces (its `gen_ai` span current around the call, or started inside
it), and `HAJER_MODEL_SPANS=0` turns the SDK's model spans off outright. The session, user and workflow are
stamped onto that instrumentation's spans either way.

## Evals: `hajer eval`

```yaml
# hajer.yaml — the repository's suites and the obligations its tests cover
version: 1
suites:
  - id: support
    path: evals/support/promptfooconfig.yaml
obligations:
  - id: obl_refund_status_disclosed
    title: Refund status is disclosed accurately
    workflow: wf_support
```

```bash
hajer eval            # runs every declared suite on the pinned promptfoo; one payload per suite
hajer eval --upload   # and reports each run to the platform, against the repository the commit belongs to
```

An ordinary promptfoo configuration, with `metadata.hajer.workflowId` and `obligationIds` on the tests that
cover an obligation. The platform reads the same `hajer.yaml` through the repository's GitHub connection, so
the suites, their tests and the obligations are there before the first run is uploaded.
[docs/evals.md](https://github.com/HajerAI/hajer-sdk/blob/main/python/docs/evals.md) has the whole of it.

## Command line

```bash
hajer doctor           # every HAJER_* setting in force, where it came from, where spans would go, whether the service answers
hajer attach-path      # the directory to put on PYTHONPATH for attach mode
hajer eval             # run the repository's suites on the pinned engine (docs/evals.md)
```

`hajer` is a console script; `python -m hajer` is the same program. `doctor` is the first command to run when
nothing is arriving. It never prints your key.

## Supported libraries

`openai`, `anthropic`, `langchain-openai`, `langchain-anthropic`, `litellm` and `google-genai`, sync and
async, streamed and not. The
[support matrix](https://github.com/HajerAI/hajer-sdk/blob/main/python/docs/support-matrix.md) is
generated from the SDK's own target declarations and lists, per library, whether usage is observable on
a streamed call and each one's caveat. A model request your code sends through its own httpx client
can be captured by wrapping that client's transport in `hajer.CaptureTransport`.

## Documentation

- [Reference](https://github.com/HajerAI/hajer-sdk/blob/main/python/docs/reference.md) — the public
  API, what a span carries, what `wrap` captures, streaming, redaction, attach mode, the command line,
  every setting, and errors.
- [Evals](https://github.com/HajerAI/hajer-sdk/blob/main/python/docs/evals.md) — `hajer eval`, `hajer.yaml`,
  the payload; and the [engine pin](https://github.com/HajerAI/hajer-sdk/blob/main/python/docs/evals-engine.md)
  behind it.
- [Development](https://github.com/HajerAI/hajer-sdk/blob/main/python/docs/development.md) — working
  on this package.

## License

MIT.
