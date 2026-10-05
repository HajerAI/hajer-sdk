# hajer — the Python SDK

Your application's model calls, as traces the Hajer platform stores and shows: every request a trace, every
model call a generation under it, every conversation a session you can read top to bottom. And `hajer eval`:
the repository's promptfoo suites, run on a pinned engine in CI and reported to the platform against the
workflows and obligations they cover.

This package does not run your application, does not sit in your provider path and decides nothing on your
behalf. It records what the application already does and exports it over OpenTelemetry.

**Documentation: [docs.hajer.ai](https://docs.hajer.ai)**

## Install

```bash
pip install "hajer[otel]"          # the SDK with the OpenTelemetry exporter: what a production app installs
pip install hajer                  # httpx + pydantic only: records locally, exports nothing (no OTel SDK)
pip install "hajer[evals]"         # `hajer eval`: run promptfoo suites on the pinned engine (Node >= 22.22 on PATH)
```

Python ≥ 3.11.

## Configure

| Variable | Required | Meaning |
| --- | --- | --- |
| `HAJER_API_KEY` | yes | Your team API key. |
| `HAJER_TEAM_ID` | yes | The team the traces belong to. |
| `HAJER_BASE_URL` | no | The service. Defaults to `https://api.hajer.ai`. |

In the Hajer app, Settings → API keys → Create key shows all three as a `.env` block. Without a key and team
id the SDK is inert: every call is still recorded locally, nothing opens a socket, nothing raises. Every
other setting is in [Configuration](https://docs.hajer.ai/telemetry/configuration).

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

Each `handle_ticket` call is one trace: a `workflow answer-support-question` span with a `chat gpt-5`
generation under it, and `session.id` / `user.id` on both. `openai`, `anthropic`, `langchain-openai`,
`langchain-anthropic`, `litellm` and `google-genai` are supported, sync and async, streamed and not. Run
`hajer doctor` when nothing arrives.

## Evals

```bash
hajer eval            # runs every suite hajer.yaml declares, on the pinned promptfoo
hajer eval --upload   # and reports each run to the platform
```

## Documentation

- [Telemetry](https://docs.hajer.ai/telemetry/quickstart): instrumenting clients, workflows, sessions,
  export, redaction, configuration, the command line and the API reference.
- [Evals](https://docs.hajer.ai/evals/quickstart): `hajer eval`, `hajer.yaml`, test metadata, providers,
  uploading, CI and the engine.
- [Changelog](https://github.com/HajerAI/hajer-sdk/blob/main/python/CHANGELOG.md) ·
  [Contributing](https://github.com/HajerAI/hajer-sdk/blob/main/python/CONTRIBUTING.md)

## License

MIT.
