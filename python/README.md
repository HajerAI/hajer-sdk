# hajer — the Python SDK

At the point where a model output crosses into a side effect — an HTTP response, an email, a database
write — hand Hajer the request, the output and the evidence you chose. Hajer applies a named, versioned
**verifier** and returns an **assessment**. Your application decides what to do with it.

This package is that call. It does not run your application, does not sit in your provider path, and
does not decide anything on your behalf. It can also record the model calls your application makes, so
an assessment is read beside the calls that produced the output.

## Install

```bash
pip install hajer                  # httpx + pydantic, nothing else
pip install "hajer[openai]"        # with the OpenAI SDK alongside
pip install "hajer[anthropic]"     # with the Anthropic SDK alongside
pip install "hajer[otel]"          # optional coexistence with an existing OpenTelemetry setup
```

The `openai` and `anthropic` extras are a convenience: the SDK never imports either library.
`wrap(client)` instruments the object you hand it, by attribute.

Python ≥ 3.11, `httpx>=0.28.1`, `pydantic>=2.12`. The pydantic floor is deliberately low: this package
installs into **your** environment, and a floor above your pin would make it uninstallable rather than
make you upgrade.

## Configure

Three environment variables, read once when the client is constructed:

| Variable | Required | Meaning |
| --- | --- | --- |
| `HAJER_API_KEY` | yes | Your team API key. |
| `HAJER_TEAM_ID` | yes | The team every request is scoped to. |
| `HAJER_BASE_URL` | no | The service. Defaults to `https://api.hajer.ai`; set it only for a local or self-hosted platform. |

```bash
export HAJER_API_KEY=...
export HAJER_TEAM_ID=...
```

**Getting a key.** In the Hajer app, Settings → API keys → Create key. The key is shown once, together
with the team id and the base URL as a `.env` block you can copy as is; the team id stays beside the
page's title afterwards. If you do not have access to a team yet, contact the Hajer team.

**Inert without a key.** Without `HAJER_API_KEY` and `HAJER_TEAM_ID` — or with `HAJER_DISABLED=1` — the
client is inert: `verify` returns `Assessment(status="unavailable", reason="DISABLED")` immediately with
no socket, `observe` returns a receipt in state `disabled`, and nothing raises. The integration can land
in a repository whose test suite has no Hajer credentials and pass unchanged. A missing key is not a
configuration error.

Set `HAJER_ENVIRONMENT` (`production`, `staging`, `dev`) in every environment you want Hajer to learn
from. Every other setting has a default; the full table is in the
[reference](https://github.com/HajerAI/hajer-sdk/blob/main/python/docs/reference.md#settings).

## Quickstart

<!-- The `hajer:quickstart` tag on the fence is extracted and compiled by an external check. Keep the
tag, and keep the block valid Python: a copy kept elsewhere would pass forever while these lines broke. -->

```python hajer:quickstart
import hajer
from openai import OpenAI

hajer_client = hajer.Hajer()           # reads HAJER_API_KEY and HAJER_TEAM_ID from the environment
openai_client = hajer.wrap(OpenAI())   # model calls are recorded and attached to the next verify

def handle_ticket(ticket, order):
    reply = openai_client.chat.completions.create(
        model="gpt-5", messages=[{"role": "user", "content": ticket.question}]
    ).choices[0].message.content

    assessment = hajer_client.verify(
        "refund-policy@1",                                          # the verifier, pinned: there is no "latest"
        {"ticketId": ticket.id, "question": ticket.question},       # the request
        reply,                                                      # the output, as it will be sent
        {"orderState": order.state, "approvedPolicy": order.policy},  # the evidence you chose
    )
    if assessment.status == "violated":
        return hold_for_review(reply, assessment.findings)
    return send(reply)
```

Three things about those lines, because they are the whole design:

- **Explicit evidence fields.** `{"orderState": order.state}`, never `order.to_dict()`. A verifier's
  evidence contract names fields; an assessment can only be about what it was given.
- **The final payload, before the effect.** `verify` is called on the string that is about to be sent,
  after every transformation, not on the raw model output.
- **The application decides.** `verify` answers; your `if` acts. Nothing in this SDK holds, retries or
  sends on your behalf.

`assessment.status` is one of `satisfied`, `violated`, `insufficient_evidence` or `unavailable`.

To try one `verify` end to end against a verifier that already exists, see
[Your first row](https://github.com/HajerAI/hajer-sdk/blob/main/python/docs/local-platform.md#your-first-row).

## Concepts

**`verify` and `observe`.** `verify` is synchronous with your request path: it returns an `Assessment`
within `deadline_ms` (default 1500 ms) and **never raises** — a transport failure, a timeout or a 5xx is
an `unavailable` assessment whose `reason` says which. There is no automatic retry, because a late answer
cannot justify an effect you already performed. `observe` takes the same arguments, puts the submission
on a bounded in-memory queue and returns a receipt at once; a background worker flushes it, and the
receipt moves through `queued`, `accepted` and `complete`. Use `observe` where nothing waits on the
answer, including semantic checks too slow to run inline. `AsyncHajer` is the same client for asyncio.

**`wrap(client)`.** Instruments an OpenAI, Anthropic, google-genai or LangChain chat model client — or
the `litellm` module — in place and returns the same object. Each call it makes is recorded (provider,
model, settings, tool calls and results, usage, timing, and message content, redacted client-side) and
attached as `wrappedCalls` to the next `verify` or `observe` in the same task. `hajer.instrument()` does
the same for clients constructed later, when you cannot reach the construction site.

**`scope()`.** Frameworks often make provider calls in child asyncio tasks, whose context never reaches
the parent's `verify`. A scope collects every call made inside it, whatever task made it:

```python
with hajer.scope(workflow="support-answer"):
    reply = await agent.ainvoke(question)
    assessment = hajer_client.verify(...)
```

**Attach mode.** For an application nobody has instrumented yet: every provider call made outside a
`scope()` becomes one `observe` observation of its own, with no verifier — which is how you find out what
the workflows are before writing an obligation about any of them. It is opt-in:

```bash
PYTHONPATH="$(python -m hajer attach-path)" HAJER_ATTACH=1 python -m your_app   # no code change
```

```python
import hajer.autoattach     # one line in the entry point; reads HAJER_ATTACH, so it is safe to keep
```

**Client-side redaction** is on by default. Card numbers, IBANs, national ids, credentials, email
addresses and similar shapes are replaced with `[redacted:<CATEGORY>]` before anything leaves the
process. `hajer.build_policy(...)` adjusts it per client or per call.

## Command line

```bash
python -m hajer doctor           # every HAJER_* setting in force, where it came from, and whether the service answers
python -m hajer tail --follow    # one line per recorded observation as it lands
python -m hajer proxy --upstream https://api.anthropic.com --listen 127.0.0.1:8091   # record at the wire
python -m hajer attach-path      # the directory to put on PYTHONPATH for attach mode
```

`doctor` is the first command to run when nothing is arriving. It never prints your key.

## Supported libraries

`openai`, `anthropic`, `langchain-openai`, `langchain-anthropic`, `litellm` and `google-genai`, sync and
async, streamed and not. The
[support matrix](https://github.com/HajerAI/hajer-sdk/blob/main/python/docs/support-matrix.md) is
generated from the SDK's own target declarations and lists, per library, whether usage is observable on
a streamed call and each one's caveat. A model request your code sends through its own httpx client
can be captured by wrapping that client's transport in `hajer.CaptureTransport`.

## Documentation

- [Reference](https://github.com/HajerAI/hajer-sdk/blob/main/python/docs/reference.md) — the public
  API, the `verify` reason table, the `observe` delivery contract, idempotency and case keys, what
  `wrap` captures, streaming, redaction, attach mode, the command line, every setting, costs and errors.
- [Local platform](https://github.com/HajerAI/hajer-sdk/blob/main/python/docs/local-platform.md) —
  pointing the SDK at a local or self-hosted Hajer platform, and a first `verify` you can run as written.
- [Development](https://github.com/HajerAI/hajer-sdk/blob/main/python/docs/development.md) — working
  on this package.

## License

MIT.
