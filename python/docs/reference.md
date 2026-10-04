# hajer — Python SDK reference

The whole public surface, the rules it keeps, and every setting. [`README.md`](../README.md) is the
quickstart; [`evals.md`](evals.md) is `hajer eval`.

Two rules shape everything here, because this package runs inside your request path:

- **It does not raise into application code.** A span that cannot be started, exported or flushed costs the
  span, never the call it was about. The exceptions that exist are listed under [Errors](#errors), each with
  the reason silence would be worse.
- **It changes nothing about the call.** `wrap(client)` returns the object you handed it; the methods, the
  return values and the exceptions are the provider's own, re-raised unchanged.

## The public API

```python
# instrumentation: every model call the client makes is recorded, and emitted as a gen_ai span
hajer.wrap(client, *, settings=None) -> the same client, instrumented in place
hajer.instrument(*, settings=None) -> Instrumentation       # patch the provider classes: clients built later too
hajer.uninstrument()                                        # restore the patches
hajer.attach(*, settings=None) -> Attachment(.classes, .modules, .inert, .describe())   # no code change
hajer.detach() / hajer.attachment()
hajer.CaptureTransport / hajer.AsyncCaptureTransport        # an httpx transport that records model requests

# the shape around the calls: declared spans
hajer.workflow(workflow_id) / hajer.component(component_id) / hajer.tool(tool_id, *, name=None, arguments=None)
    # each a decorator (sync or async), a `with` block and an `async with` block

# the conversation: declared once, carried by every span inside
hajer.session(session_id) / hajer.user(user_id)
hajer.context(*, session=None, user=None, tags=(), metadata=None)
    # each a decorator, a `with` block and an `async with` block; nesting merges

# process-wide configuration
hajer.configure(settings=None, *, tracer_provider=None, policy=None)
hajer.flush(timeout_ms=None) -> bool                        # wait for exported spans to leave

# what was recorded, locally
hajer.scope(workflow=None) -> a context manager yielding Operation(.calls, .dropped)
hajer.wrapped_calls() / hajer.wrapped_calls_dropped() / hajer.clear_wrapped_calls()

# client-side redaction
hajer.build_policy(*, classes_off=(), extra_rules=(), paths_exempt=()) -> ClientRedactionPolicy
hajer.redact_document(document, *, policy) -> tuple[JsonValue, tuple[RedactionEntry, ...]]

hajer.HajerSettings            # every setting, as a frozen object; `HajerSettings.from_env()`
hajer.__version__
```

A name exported from `hajer` and listed in `hajer.__all__` is API. Anything else, including every module
with a leading underscore, may change. `hajer.evals.provider` and `hajer.evals.bind` are in
[evals.md](evals.md).

## What a span carries

### The declared spans

| Span | Name | Attributes |
| --- | --- | --- |
| `hajer.workflow("wf_support")` | `workflow wf_support` | `hajer.workflow.id` |
| `hajer.component("cmp_refund_agent")` | `component cmp_refund_agent` | `hajer.component.id`, plus the enclosing `hajer.workflow.id` |
| `hajer.tool("tool_refund_status", name="get_refund_status", arguments={...})` | `execute_tool get_refund_status` | `hajer.tool.id`, `gen_ai.tool.name`, `gen_ai.operation.name=execute_tool`, `gen_ai.tool.call.arguments` (with content capture; redacted and bounded), plus the enclosing ids |

The ids form a stack in a `contextvars.ContextVar`: a component inside a workflow carries the workflow's id,
a tool inside both carries both, across `await` and into threads that copy the context (`asyncio.to_thread`).
`hajer.workflow` also enters `hajer.scope(workflow=…)`, so the model calls recorded inside it carry the same
id as their `workflow_hint`. Repeated ids are repeated runs of one workflow, each with its own span. An
exception the wrapped code raises propagates unchanged; the span records its *type* (`error.type`) and an
error status, never the message or the stack trace.

### The model spans

Every call `wrap`, `instrument` or `attach` records becomes one span when it settles, backdated to when it
began, under whatever span was current when it began — the workflow or component — and never made current
itself. Named `{operation} {model}` (`chat gpt-5`, `embeddings text-embedding-3-small`,
`generate_content gemini-2.5-pro`), kind CLIENT.

| Attribute | From |
| --- | --- |
| `gen_ai.operation.name` | `chat` (OpenAI chat and Responses, Anthropic, litellm), `embeddings`, `generate_content` (google-genai) |
| `gen_ai.provider.name` | `openai`, `anthropic`, `gcp.gen_ai`, litellm's upstream prefix (`anthropic/…` → `anthropic`), else `litellm` |
| `hajer.api` | the surface, as recorded: `chat.completions`, `responses`, `messages`, `embeddings`, `litellm.completion`, `genai.generate_content[_stream]` |
| `gen_ai.request.model`, `gen_ai.response.model` | the model asked for, and the one that answered |
| `gen_ai.request.temperature`, `top_p`, `top_k`, `max_tokens`, `seed`, `frequency_penalty`, `presence_penalty`, `choice.count`, `stop_sequences` | the request settings, when passed |
| `hajer.request.tool_names` | the declared tools' names |
| `gen_ai.response.id`, `gen_ai.response.finish_reasons` | the answer's id and finish reason |
| `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.usage.total_tokens`, `hajer.usage.cache_read_input_tokens`, `hajer.usage.cache_write_input_tokens` | the usage the provider named |
| `gen_ai.input.messages`, `gen_ai.output.messages`, `gen_ai.system_instructions` | the content, with `HAJER_CAPTURE_CONTENT` (below) |
| `gen_ai.embeddings.dimension.count`, `hajer.embedding.vectors` | an embedding answer's shape; never its vectors or its input |
| `server.address`, `server.port` | the client's endpoint, with `HAJER_CAPTURE_CALL_SITE` |
| `code.function.name`, `code.file.path`, `code.line.number` | the innermost application frame, with `HAJER_CAPTURE_CALL_SITE` |
| `hajer.stream`, `hajer.stream.complete`, `hajer.stream.chunks` | a streamed call: whether it ran to exhaustion, how many chunks crossed |
| `error.type`, `http.response.status_code` | when the call raised: the exception class and the status it carried, never the message |
| `hajer.limitations` | what the record could not observe, one sentence each (a stream that named no usage, content that was clipped) |

And on every span, declared or model, the stamp of its surroundings: `session.id`, `user.id`, `hajer.tags`,
`hajer.metadata.<key>` from the conversation context; `hajer.workflow.id` / `hajer.component.id` from the
enclosing declared spans; `deployment.environment.name` (and the older `deployment.environment`) from
`HAJER_ENVIRONMENT`; and under `hajer eval`, `hajer.eval.run.id`, `hajer.eval.test_case.id`,
`hajer.eval.workflow.id`, `hajer.eval.obligation.ids`.

### The content

`gen_ai.input.messages` and `gen_ai.output.messages` are JSON strings in the conventions' message shape,
whichever library made the call:

```json
[{"role": "user", "parts": [{"type": "text", "content": "where is my order"}]},
 {"role": "assistant", "parts": [{"type": "tool_call", "id": "call-1", "name": "lookup", "arguments": {"id": "o-1"}}]},
 {"role": "tool", "parts": [{"type": "tool_call_response", "id": "call-1", "result": "shipped"}]}]
```

OpenAI chat messages, Responses items (`function_call`, `function_call_output`), Anthropic blocks (`tool_use`,
`tool_result`) and google-genai parts (`function_call`, `function_response`) all become this; a block that is
not text (an image, a document) becomes a part naming its type and nothing else; roles are folded onto
`user`, `assistant`, `system`, `tool`. The output is one assistant message per choice, with the finish reason
on it; a streamed call's output is the text as it arrived. `gen_ai.system_instructions` is the out-of-band
`system` / `instructions` / `config.system_instruction` argument; OpenAI chat keeps its instructions in the
messages, where they already are.

Content is carried only with `HAJER_CAPTURE_CONTENT` (on by default), passes [client-side
redaction](#client-side-redaction) first, and the three attributes together fit inside
`HAJER_WRAPPED_CALL_MAX_BYTES`: the input first, then the output, then the instructions; what does not fit is
clipped in the middle (head and tail kept, the full length and SHA-256 marked between them), and a document
whose shape alone does not fit is left out and said in `hajer.limitations`. With content capture off the span
carries shapes, names, counts, timing and usage, and no text.

## Where the spans go

Decided once per process, OpenTelemetry-free, in this order:

1. **`HAJER_OTLP_ENDPOINT`** (or the standard `OTEL_EXPORTER_OTLP_ENDPOINT`): a collector of your own. Spans
   are exported to its `/v1/traces`, with no credential unless `HAJER_OTLP_HEADERS` names one. `hajer eval`
   sets it to the engine's loopback receiver.
2. Otherwise, with `HAJER_API_KEY` and `HAJER_TEAM_ID` set, `HAJER_DISABLED` off and `HAJER_TRACES_ENABLED`
   on: **the platform**, at `{HAJER_BASE_URL}/api/teams/{team}/otel/v1/traces`, the key as the bearer.
3. Otherwise nowhere.

The exporter for that target is a `BatchSpanProcessor` added to the OpenTelemetry provider in use: the one
`hajer.configure(tracer_provider=…)` named; else the application's own global SDK `TracerProvider`, which keeps
its own exporters, sampler and resource beside it (what a tracing vendor's SDK does too); else an isolated
provider the SDK builds and **never installs as the global provider**. One exporter per provider, however
often `configure()` is called; the SDK's own provider carries `service.name` (`HAJER_SERVICE_NAME`, then
`OTEL_SERVICE_NAME`), `deployment.environment.name` and `hajer.sdk.version` as its resource.

Nothing blocks a call: the exporter batches on its own thread, bounded by `HAJER_TRACE_QUEUE_MAX`,
`HAJER_TRACE_BATCH_MAX`, `HAJER_TRACE_BATCH_DELAY_MS` and `HAJER_TRACE_EXPORT_TIMEOUT_MS`; `hajer.flush()`
waits `HAJER_TRACE_FLUSH_TIMEOUT_MS` for what is queued; at exit the queue gets one chance to leave and the
SDK's own exporter stops. An unreachable receiver costs at most one export timeout at exit, and
OpenTelemetry logs each failed export on its own logger. `hajer doctor` prints the target in force, or why
there is none.

Without `hajer[otel]` every decorator and every wrap still runs the code it wraps, records locally, emits
nothing, and the `hajer` logger says so once.

### Another instrumentation already there

The SDK's model span is **not emitted** for a call another OpenTelemetry instrumentation already traces: a
span carrying `gen_ai.operation.name` current when the call opened, or one started inside the call (a
client-level instrumentation's patch runs inside Hajer's). One call, one span — theirs. This is best
effort: an instrumentation that sets the operation name after its span ended, or in another context, slips
through, and `HAJER_MODEL_SPANS=0` is the switch that makes it certain. The conversation, workflow and
environment are stamped onto that instrumentation's spans either way, each key only when the span does not
already carry it.

## The conversation: `session`, `user`, `context`

```python
with hajer.session(conversation.id), hajer.user(customer.id):
    answer = agent.run(question)

@hajer.context(session="…", user="…", tags=["beta"], metadata={"plan": "pro", "seats": 3})
async def handle(request): ...
```

A trace is one request; the platform groups traces into a conversation by `session.id`, names the person
by `user.id` and filters by tags and metadata. Declared as a block or a decorator, carried by every span
opened inside — the declared spans, the model spans and any other instrumentation's — across `await` and
into threads that copy the context. Nesting merges: an inner declaration adds to or overrides the enclosing
one key by key, tags accumulate in order. Bounds, clipped rather than refused: 16 tags, 32 metadata keys,
1024 characters per value. A metadata value is a string, a number or a boolean; a document is a `TypeError`
where it is written, because a nested value is not a label. A `user.id` is an id of your own — never a name
or an email.

## What `wrap` captures

`hajer.wrap(client)` returns the object you handed it, instrumented in place. It recognises `openai.OpenAI`
/ `AsyncOpenAI` (`chat.completions.create`, `responses.create`, chat and Responses `parse`, the raw-response
helpers, and the `stream` helpers beside them), `anthropic.Anthropic` / `AsyncAnthropic` (`messages.create`,
`beta.messages.create`, `messages.stream`), `google.genai.Client` (`models.generate_content[_stream]`, sync
and `aio`) and the `litellm` module (`completion`, `acompletion`), by the attributes they carry — no
provider library is imported. The [support matrix](support-matrix.md) is the per-library table, generated
from the SDK's own target declarations.

**A framework chat model is instrumented at the provider client it holds.** `hajer.wrap(ChatAnthropic(…))`
returns the same `ChatAnthropic` — keep handing it to whatever takes a `BaseChatModel`, `bind_tools` included
— with its inner `anthropic` clients (`_client`, `_async_client`) instrumented, because those are what
`_generate` and `_agenerate` call; `hajer.wrap(ChatOpenAI(…))` does the same at `root_client` /
`root_async_client`, including the `with_raw_response` calls LangChain makes. The record is of the request
that left the process. A chat model that keeps its client somewhere else is `UnsupportedClientError`, not
silence.

**`instrument()`** patches the provider classes (and installs an import hook for a library imported later),
so clients constructed afterwards are instrumented at construction; `uninstrument()` restores the patches.
**`attach()`** does the same for every library in the support matrix, for a process nobody instrumented
(below). A `unittest.mock` object handed to `wrap` is returned unchanged and records nothing.

### Streaming

A streamed call is recorded — and its span emitted — when the stream ends: when the iterator is exhausted,
when you close it, or when the `with` block around it returns. Never at garbage collection. What you get back
is a proxy that forwards the provider's own chunks and its own surface (`text_stream`, `response`,
`until_done()`, `get_final_message()`, `close()`); the chunks are the provider's objects, the container is
not. A stream abandoned half way is `hajer.stream.complete=false`. The span keeps the session, user and
workflow the call was *opened* under, wherever the stream is consumed.

Two limits: a consumer that reads only Anthropic's `text_stream` sends no chunks through the proxy
(`hajer.stream.chunks` is 0; the usage still arrives from `get_final_message()`); and OpenAI puts token
usage on the final chunk only when the request carried `stream_options={"include_usage": True}` — without
it the record says so in `hajer.limitations` rather than reporting zero tokens as a fact.

### Raw responses

A `with_raw_response` / `with_streaming_response` call hands back the provider's own object, untouched, and
is recorded when your code reads the body — `parse()`, `read()`, `json()`, `iter_lines()`, the
`http_response` — exactly once. A body never read is a record that says so (`RAW_RESPONSE_NOT_READ`); a 2xx
body that is not a JSON answer (a proxy's page) is `RAW_RESPONSE_UNREADABLE`. `HAJER_BODY_MAX_BYTES` bounds
how much of such a body is buffered to read the answer out of it.

### The transport tee: `CaptureTransport` and `HAJER_CAPTURE_HTTP`

```python
http = httpx.Client(transport=hajer.CaptureTransport(httpx.HTTPTransport()))
# Async: httpx.AsyncClient(transport=hajer.AsyncCaptureTransport(httpx.AsyncHTTPTransport()))
```

A model request your code sends through its own httpx (or httpx2) client — a JSON POST to a chat
completions, Responses or messages path — is recorded as that provider call, from the bytes, when the
application consumes them. Bytes are observed, never pre-read; retained response bytes are bounded by
`HAJER_WRAPPED_CALL_MAX_BYTES`; headers are never recorded; a request a wrapped provider call is making is
not recorded twice, and the SDK's own never.

`HAJER_CAPTURE_HTTP=1` patches httpx's default transports at the first `wrap`, `attach` or `instrument`, so
every other outbound request the application makes — a search API, a data vendor — is recorded as an HTTP
client span: the method, the host, the path as a **template** (a numeric segment becomes `{id}`, one the
redaction catalog recognises `{redacted}`, a long random-looking one `{token}`: webhooks and bots carry
their credential in the path), the status and where it was made. Never a header, the query string or a body.

### Child tasks and `scope()`

The calls `wrap` records go into a per-task `contextvars` context, and a child task gets a *copy* of it; a
scope collects every call made inside it, whatever task made it:

```python
with hajer.scope(workflow="support-answer") as operation:
    reply = await agent.ainvoke(question)      # the framework may fan this out as it likes
    calls = operation.calls                    # and the calls are all here
```

`hajer.workflow(id)` enters a scope of its own, so inside a declared workflow this is already true. The
optional `workflow` is a stable, non-secret name carried as each call's `workflow_hint`. Outside every scope
`hajer.wrapped_calls()` is this task's calls, capped at `HAJER_WRAPPED_CALLS_MAX` (the first ones kept, the
rest counted in `wrapped_calls_dropped()`).

### What it can never capture

At any setting: what happened to the output *after* the call — a later transformation, a template; database
truth, authorization state, or whether a side effect completed; the provider SDK's own internal retries
(`retries` is always zero); a stream you never consume, never close and never wrap in a `with`.

## Client-side redaction

On by default (`HAJER_REDACT_CLIENT=1`). Card numbers, IBANs, VINs, national ids, account-like runs,
credentials, email addresses, phone numbers, person names, postal addresses and dates of birth are replaced
with `[redacted:<CATEGORY>]` in every message, answer, tool argument and tool result — and in a declared
tool span's arguments — before a span carries it, under the catalog in `_rules.py`. A field whose *name* is a
secret (`password`, `api_key`, `authorization`, …) is withheld whatever it holds. The pass never raises: past
its budgets (1 MiB, 50 000 values, 64 levels, 65 536 characters per value) it stops scanning and withholds
what it did not read.

```python
policy = hajer.build_policy(
    classes_off=["ACCOUNT_LIKE"],                 # a class this team has decided about
    extra_rules=[("CUSTOMER_REF", r"\bCUS-\d{6}\b")],   # a team's own shape
    paths_exempt=["request.account"],             # a path that must travel as it is
)
hajer.configure(policy=policy)                    # for every span from here on
redacted, entries = hajer.redact_document(document, policy=policy)   # the same pass, by hand
```

A pattern that does not compile or a path the grammar cannot read is refused where it is written
(`build_policy`), never during a call.

## Attach mode

For an application nobody has instrumented yet: every provider client the process builds records — and,
with a key, exports — its model calls. Three ways in, one hook:

```bash
PYTHONPATH="$(python -m hajer attach-path)" HAJER_ATTACH=1 python -m your_app   # the sitecustomize shim
```

```python
import hajer.autoattach    # one line in the entry point; reads HAJER_ATTACH, so it is safe to keep
hajer.attach()             # in code; does not consult the variable — the line is the yes
```

Attach patches the **classes** the supported libraries export (so every client built afterwards is
instrumented at construction) and installs an import hook for a library imported later; nothing is
imported to find them. A module function target (litellm) has its entry points replaced and every alias
rebound. `hajer.detach()` stops instrumenting new clients and removes the hook; it does **not** un-patch
what it patched (another thread may be inside it) — a client already built keeps recording and emitting,
and `HAJER_DISABLED=1` or `HAJER_TRACES_ENABLED=0` is what stops a process from exporting. With no key the
attachment is `inert`: the classes are instrumented, `hajer.wrapped_calls()` works, nothing leaves.

## Call-site capture

With `HAJER_CAPTURE_CALL_SITE` on (the default), each recorded call carries where it was made — up to 8
application frames (module, qualified name, file relative to the project root, line) — and the provider
endpoint's `host:port`; the span carries the innermost frame as `code.function.name`, `code.file.path`,
`code.line.number` and the endpoint as `server.address` / `server.port`. A file is only ever relative to the
project root (`HAJER_PROJECT_ROOT`, else the working directory); a file outside it is `<outside>/<basename>`,
and a home directory is never part of what is recorded, whatever the root. These are locations, never values,
which is why they need no content consent. `HAJER_CAPTURE_CALL_SITE=0` records none of them.

## The command line

```bash
hajer doctor [--json]   # every setting in force and where it came from; the OTel SDK; where spans would go; whether HAJER_BASE_URL answers
hajer attach-path       # the directory to put on PYTHONPATH for attach mode
hajer eval …            # evals.md
```

`hajer` is a console script; `python -m hajer` is the same program. `doctor` never prints the key (`set` or
`absent` is the whole answer), probes the service with one keyless GET of `/api/health` (a 401 is an
answer: something is listening), and prints `otel` (the OpenTelemetry SDK's version, or what to install) and
`traces` (the receiver URL and whether the key rides along, or why nothing would be exported).

## Settings

Every bound is a `HajerSettings` field with an environment variable. `HajerSettings.from_env()` is the only
place the process environment is read; you can also construct `HajerSettings(...)` yourself and pass it to
`wrap`, `instrument`, `attach` or `configure`.

| Variable | Default | What it is, and when to change it |
| --- | --- | --- |
| `HAJER_API_KEY` | — | Your team API key. Absent → inert. |
| `HAJER_TEAM_ID` | — | The team the traces belong to. Absent → inert. |
| `HAJER_BASE_URL` | `https://api.hajer.ai` | The service. Point it at a local or self-hosted platform (`http://localhost:8000` for a local backend). |
| `HAJER_ENVIRONMENT` | absent | The environment this process runs in — `production`, `staging`, `dev` — on every span as `deployment.environment.name`. Folded to lower case; anything that is still not lower-case letters, digits and dashes (at most 64, a letter or digit first) is dropped, never raised, and `hajer doctor` prints it as `ignored: invalid`. |
| `HAJER_TRACES_ENABLED` | `1` | Export to the platform when there is a key. `0` keeps the spans on the application's own OpenTelemetry provider, if it has one, and sends nothing to Hajer. |
| `HAJER_MODEL_SPANS` | `1` | Emit a `gen_ai` span per recorded model call. `0` for a process whose model calls another instrumentation already traces. The declared spans are unaffected. |
| `HAJER_OTLP_ENDPOINT` | `OTEL_EXPORTER_OTLP_ENDPOINT`, else absent | A collector of your own instead of the platform; spans go to its `/v1/traces`, with no credential unless `HAJER_OTLP_HEADERS` names one. Wins over the platform target. `hajer eval` sets it. |
| `HAJER_OTLP_HEADERS` | absent | Headers for the exporter, in OpenTelemetry's own `key=value,key2=value2` spelling (values percent-decoded). Added to the platform's `Authorization` header, or the only headers a collector gets. |
| `HAJER_SERVICE_NAME` | `OTEL_SERVICE_NAME`, else OpenTelemetry's default | `service.name` on the resource of the provider the SDK builds when the application has none. |
| `HAJER_TRACE_EXPORT_TIMEOUT_MS` | `5000` | One export request's deadline, and so the most an exit flush waits on an unreachable receiver. |
| `HAJER_TRACE_BATCH_DELAY_MS` | `5000` | How long a partial batch waits before it is exported anyway. |
| `HAJER_TRACE_QUEUE_MAX` | `2048` | Spans held for export before the newest are dropped. |
| `HAJER_TRACE_BATCH_MAX` | `128` | Spans in one export request. Below OpenTelemetry's 512 because a model span may carry `HAJER_WRAPPED_CALL_MAX_BYTES` of content. |
| `HAJER_TRACE_FLUSH_TIMEOUT_MS` | `2000` | How long `hajer.flush()` waits for exported spans to leave; an eval provider flushes before it answers. |
| `HAJER_CAPTURE_CONTENT` | `1` | Message content, tool arguments and tool results are captured; client-side redaction runs before export. `0` keeps shapes, names, counts, timing and usage, and no text. |
| `HAJER_CAPTURE_CALL_SITE` | `1` | Record where each call was made and the endpoint it went to; see [call-site capture](#call-site-capture). |
| `HAJER_CAPTURE_HTTP` | `0` | Also record every outbound HTTP request the application makes through httpx's default transports, as an HTTP client span: the method, the host, the path as a template, the status. Never a header, the query string or a body. |
| `HAJER_PROJECT_ROOT` | the working directory | The directory call-site frames are made relative to. A process started from `/` records every frame as `<outside>` until this names the checkout. |
| `HAJER_WRAPPED_CALL_MAX_BYTES` | `32768` | What one call's content may carry — its messages, its answer, its tool arguments and results, and the three content attributes of its span together. Past it the longest texts are clipped in the middle, keeping head and tail around their full length and SHA-256, and the span says so. |
| `HAJER_WRAPPED_CALLS_MAX` | `32` | Recorded calls one task — or one `scope()` — accumulates before further ones are counted and dropped. |
| `HAJER_BODY_MAX_BYTES` | `65536` | Bytes of a provider's raw response body (`with_raw_response`) buffered to read the answer out of it; past it the body is handed through unread and the record says so. |
| `HAJER_REDACT_CLIENT` | `1` | Run the detector catalog over every message, answer and tool argument before a span carries it. `0` turns it off — a decision for a team that needs a value the rules would remove; `paths_exempt` on a policy is the narrower one. |
| `HAJER_ATTACH` | `0` | The switch the import-time hook reads: with it on, `import hajer.autoattach` (the one line, or the `sitecustomize` shim) attaches this process. `hajer.attach()` in code does not consult it. |
| `HAJER_CACHE_DIR` | `$XDG_CACHE_HOME/hajer`, else `~/.cache/hajer` | Where `hajer eval` installs the pinned engine and keeps run directories. The `HAJER_EVAL_*` settings are in [evals.md](evals.md#settings). |
| `HAJER_DISABLED` | `0` | Inert regardless of the key. The kill switch. |

Booleans accept `1/0`, `true/false`, `t/f`, `yes/no`, `y/n`, `on/off`, any case — the same spellings for
every boolean setting. A value that is neither a number nor a boolean where one is required raises
`HajerConfigError` when the settings are read — never during a model call; the span emitter, which reads the
environment lazily, logs it and runs on defaults instead. `hajer doctor` prints each setting with the value
in force and whether it came from the environment or from the default above.

## Errors

- `HajerError` — the base class; one `except hajer.HajerError` catches everything this package raises.
- `HajerConfigError` — a setting is not a number or not a boolean. Raised where the settings are read, never
  during a call; the supplied value is withheld from the message. A *missing* key is not a configuration
  error: it makes the SDK inert.
- `UnsupportedClientError` — `wrap()` was handed an object with no surface it recognises. Returning it
  uninstrumented would mean silently capturing nothing.
- `BodyOverBoundError` — internal to the eval upload, which turns it into a `BODY_OVER_BOUND` receipt.
- `EvalMetadataError`, `ManifestError` — `hajer eval` only, in its own processes: a malformed
  `metadata.hajer`, an obligation the manifest does not declare, a `hajer.yaml` the reader refuses.

Nothing else raises into application code. A span that cannot be started, a document that cannot be encoded,
an exporter that cannot flush — each degrades to "no span" or "no attribute", and `hajer.flush()` returns
`False` rather than raising.
