# hajer — Python SDK reference

The detailed reference for the `hajer` package. The [README](../README.md) has the install, the
configuration and the quickstart; this page has everything behind them.

- [The public API](#the-public-api)
  - [`verify` never raises](#verify-never-raises)
  - [The delivery contract of `observe`](#the-delivery-contract-of-observe)
  - [Idempotency](#idempotency)
  - [Case identity: `case_key`](#case-identity-case_key)
- [What `wrap` captures](#what-wrap-captures)
  - [Installing before construction: `instrument()`](#installing-before-construction-instrument)
  - [OpenTelemetry](#opentelemetry)
  - [The httpx fallback: `CaptureTransport`](#the-httpx-fallback-capturetransport)
  - [Streaming, and where token usage comes from](#streaming-and-where-token-usage-comes-from)
  - [Framework chat models](#framework-chat-models)
  - [What is recorded](#what-is-recorded)
  - [Child tasks and `scope()`](#child-tasks-and-scope)
  - [What it can never capture](#what-it-can-never-capture)
  - [Recording non-model responses for replay: `record_boundaries`](#recording-non-model-responses-for-replay-record_boundaries)
- [Client-side redaction](#client-side-redaction)
- [Attach mode](#attach-mode)
- [The command line](#the-command-line)
- [Settings](#settings)
- [What a verification costs, and who bounds it](#what-a-verification-costs-and-who-bounds-it)
- [Errors](#errors)
- [Call-site capture: what leaves the process](#call-site-capture-what-leaves-the-process)

## The public API

```python
hajer.Hajer(*, settings=None, transport=None, raise_on_unavailable=False, policy=None)
hajer.AsyncHajer(*, settings=None, transport=None, raise_on_unavailable=False, policy=None)

    .verify(verifier, request, output, evidence=None, *, deadline_ms=None, idempotency_key=None,
            case_key=None, policy=None) -> Assessment
    .observe(verifier, request, output, evidence=None, *, idempotency_key=None,
             case_key=None, policy=None) -> ObserveReceipt
    .observe_call(wrapped_call) -> ObserveReceipt          # `Hajer` only: one provider call, no verifier
    .observations(*, since=None, limit=None) -> tuple[ObservationRow, ...]    # what `tail` reads
    .flush()            # send everything queued now
    .queue_depth()      # observations waiting in memory
    .close()            # flush, stop the background worker, close the pool
    .settings / .inert
```

On `AsyncHajer` everything above is awaited except `observe` and `queue_depth`: `observe` enqueues and
returns its receipt with no await, because putting a tuple on a bounded in-memory queue is not I/O, and
`await receipt.poll()` is where the asking happens.

```python
hajer.wrap(client, *, settings=None) -> the same client, instrumented in place
hajer.wrapped_calls() / hajer.wrapped_calls_dropped() / hajer.clear_wrapped_calls()
hajer.scope(workflow=None) -> a context manager yielding Operation(.calls, .dropped)
hajer.instrument(...) / hajer.uninstrument()
hajer.attach(*, settings=None, transport=None) -> Attachment(.classes, .modules, .inert, .describe())
hajer.detach() / hajer.attachment()
hajer.record_boundaries(hosts=[...])
hajer.CaptureTransport / hajer.AsyncCaptureTransport
hajer.wire_source()   # where the wire shapes in force were generated from

hajer.build_policy(*, classes_off=(), extra_rules=(), paths_exempt=()) -> ClientRedactionPolicy
hajer.redact_document(document, *, policy) -> tuple[JsonValue, tuple[RedactionEntry, ...]]
```

A name exported from `hajer` and listed in `hajer.__all__` is API. Anything else, including every
module with a leading underscore, may change.

`Assessment` carries `status`, `reason`, `findings`, `missing_evidence`, `limitations`, `shadow`,
`per_check`, `cost_microusd`, `latency_ms`, `verifier`, `observation_id`, and `decided_locally`.

`status` is one of `satisfied`, `violated`, `insufficient_evidence`, `unavailable` — the server
contract's own spelling, because your `if` compares against it.

A verifier is always **pinned**: `refund-policy@1`, never `refund-policy`. There is no "latest", and the
server refuses an unpinned name before ingest sees it. `verify` passes whatever string it is given, so a
caller who forgets learns it from the response.

### `verify` never raises

Transport failure, timeout, 5xx, a body over the bound, a missing key: each is an `unavailable`
assessment whose `reason` says which. `reason` distinguishes what the SDK decided locally from what
the service answered:

| `reason` | Decided by | Means |
| --- | --- | --- |
| `SHADOW` | server | The verifier has not qualified. Recorded in full; nothing you could gate on. |
| `DEADLINE` | server | The judge route's measured p95 does not fit the budget you sent. |
| `VERIFIER_UNKNOWN` | server | That verifier is not indexed. Never a guess. |
| `JUDGE_UNAVAILABLE` / `BUDGET_EXHAUSTED` / `INTERNAL` | server | The service could not answer. |
| `DISABLED` | SDK | No key, no team, or `HAJER_DISABLED`. |
| `LOCAL_DEADLINE` | SDK | Your clock ran out here. Not the same fact as `DEADLINE`. |
| `TRANSPORT` | SDK | The connection failed. |
| `BODY_OVER_BOUND` | SDK | Refused before the socket; see `HAJER_BODY_MAX_BYTES`. |
| `HTTP_STATUS` | SDK | The service answered, with an error status. |
| `MALFORMED_RESPONSE` | SDK | The body was not an assessment. |

`assessment.decided_locally` is True for the bottom six.

**There is no `on_unavailable="satisfied"`.** Continuing your application and satisfying an obligation
are different facts, and the second is never manufactured from the first. What to do when an
assessment is unavailable — continue, hold, review, reject — is your policy, chosen per workflow.
`Hajer(raise_on_unavailable=True)` is there if you would rather have the traceback.

`deadline_ms` covers the **whole** operation: serialisation, connection, transfer and the server's
own work. The server is told what is left of it, in the body, so it can refuse before reserving
rather than answer late. There is **no automatic inline retry**: a late answer cannot retroactively
justify an effect you already performed.

### The delivery contract of `observe`

`observe` is fire-and-forget onto a bounded in-memory queue. "Delivered" is not one fact; it is
three, and the receipt distinguishes them:

| `receipt.state` | What is true | What is not |
| --- | --- | --- |
| `queued` | The observation is in this process's memory. | Nothing has been sent. A crash here loses it. |
| `accepted` | A flush returned 2xx and the server named the observation (`receipt.observation_id`). | The verifier has not necessarily run. |
| `complete` | The assessment is here: `receipt.assessment`. | — |

`receipt.poll()` asks once; `receipt.wait(timeout_ms=…)` asks until it is there or the timeout
expires and returns `None` if it is not. On `AsyncHajer` both are awaited. Two further states exist
because pretending they do not would be the dishonest part: `disabled` (the client is inert) and
`dropped` with `receipt.detail` — `QUEUE_FULL`, `BODY_OVER_BOUND`, `CLIENT_CLOSED` or `PROCESS_EXIT`.

Flushing happens:

- on interval (`HAJER_OBSERVE_FLUSH_INTERVAL_MS`) from one background thread (or asyncio task);
- when the queue reaches `HAJER_OBSERVE_BATCH_MAX`;
- on `flush()` and on `close()`;
- at interpreter exit, via `atexit`, **for `Hajer` only**.

An `AsyncHajer` cannot be flushed from `atexit` — there is no loop left to await on — so it warns and
marks what remains `dropped`. Close it: `await client.close()`, or `async with hajer.AsyncHajer() as
client:`.

A failed flush keeps its observations queued and retries with doubling backoff from
`HAJER_OBSERVE_BACKOFF_INITIAL_MS` up to `HAJER_OBSERVE_BACKOFF_MAX_MS`. A batch is bounded twice: by
count and by the bytes of one request.

### Idempotency

Every request carries an `Idempotency-Key`. Pass your own when you have one — a request id survives a
payload your application regenerated, and a digest does not:

```python
client.verify("refund-policy@1", request, output, evidence, idempotency_key=f"ticket-{ticket.id}")
```

Otherwise it is derived: `sha256` over the scheme, the team, the verifier, and a canonical-JSON
digest of `{request, output, evidence}`. Timing, usage and the wrapped calls are deliberately **not**
in the digest — the same logical submission retried from another process must produce the same key.
The scheme is named in the key (`hj1_…`) so a change to what the digest covers becomes `hj2` rather
than a silent disagreement between two SDK versions.

### Case identity: `case_key`

A **case** is the thing several attempts are attempts *at*. Two calls with the same input and a
different session id are one case, and whether their answers agree is the only question repeatability
can ask. Name it and the service can ask it:

```python
client.verify("refund-policy@1", request, output, evidence, case_key=f"ticket-{ticket.id}")
```

The keyword is `case_key=`, the wire field is `caseKey` and the stored column is `case_key`. One name per
layer, and the keyword is the one you type.

Pass nothing and the SDK derives one: `ck1_<sha256>` over the canonical JSON of the request, taken
**before** client-side redaction rewrites anything. The normalisation is implemented in `hajer/_case_key.py` and pinned by
the shared vectors in `contract/case-key-vectors.json`, which the server's implementation also reads. The row records where the key came from, and the three sources are not
equally strong:

| `caseKeySource` | Who decided | What it is worth |
| --- | --- | --- |
| `CALLER` | your `case_key=` | Strongest. An application that knows its own ticket or scenario id knows more than any digest of a payload. |
| `DERIVED_CLIENT` | this SDK | Stable, because it is taken before redaction. |
| `DERIVED_SERVER` | ingest | Weaker: the server can only read the request as it arrived, so a redaction rule that fires moves the key and two attempts at one case land in two groups. |

That last row is the reason the derivation happens in the client rather than only on the server.

A case key is **not** an idempotency key. The idempotency key answers *is this the same submission*, and
a second attempt at one case is deliberately a different submission with its own key. The case key
answers *is this the same input*. They are derived from overlapping bytes and mean opposite things,
which is why they are two fields.

## What `wrap` captures

The preferred installation is `hajer.wrap(client)`. It returns the object you handed it, instrumented in
place: the same client, the same methods, the same return values, the same exceptions re-raised
unchanged. It recognises `openai.OpenAI` / `AsyncOpenAI` (`chat.completions.create`, `responses.create`,
chat (including beta) and Responses `parse`, raw response helpers, and the `stream` helpers beside them)
and `anthropic.Anthropic` / `AsyncAnthropic` (`messages.create`, `beta.messages.create`,
`messages.stream`), sync and async, streamed and not, by the attributes they carry — and
`google.genai.Client` and the `litellm` module the same way. The
[support matrix](support-matrix.md) is the per-library table. It is **generated** from those target
declarations by `scripts/generate_support_matrix.py` — never hand-written beside them, so it cannot
promise capture the code does not perform.

A raw response is observed when your code calls `parse()`; Hajer never reads a lazy response on your
behalf. Closing a partially consumed stream records `streamComplete: false`. Tool-call ids and the tool
results sent back on the next request are retained across chat, Responses `function_call_output`, and
Anthropic `tool_result` formats. This is evidence of the reported tool result, not independent proof of
a business-system outcome.

Hajer always records a call through its own wrap or HTTP capture, whatever other tool traces it — an
OpenTelemetry instrumentor (Traceloop's LangChain instrumentor, an OpenAI instrumentor), a vendor's
drop-in client (Langfuse's `langfuse.openai`) or an APM. Its instance-level patch composes with a
class-level vendor patch, and a model request is captured at the transport inside any runnable, tool,
task or graph span.

### Installing before construction: `instrument()`

If construction sites are hidden, install once before constructing clients:

```python
import hajer

installation = hajer.instrument()  # future OpenAI/Anthropic clients, including later imports
# Construct and use provider clients normally.
hajer.uninstrument()               # restores the constructors and instance methods
```

Repeated `instrument()` calls return the same installation. `uninstrument()` preserves patches another
library installed afterwards. Clients constructed before this call need `wrap(client)`. Unlike
`attach()`/`detach()`, this installation is reversible. Environment launchers and the proxy remain
separate, opt-in choices.

### OpenTelemetry

`hajer.instrument(tracer_provider=…)` adds an OpenTelemetry receiver beside that capture, for the model
calls Hajer cannot wrap (a provider it has no surface for, a client built before the installation with
HTTP capture off). It needs `hajer[otel]`. It takes **model operations only** (`gen_ai.operation.name`
`chat`, `text_completion`, `embeddings`, `generate_content`); a framework's `invoke_agent`,
`execute_task` and `execute_tool` spans are not calls.

When a span describes a call Hajer also recorded, the record wins and the span is dropped, on evidence
only: the same `gen_ai.response.id`; a span begun inside the call Hajer was recording; or a span begun
just before it whose request model and response id, usage or answer text agree with the record. A span
that disagrees is kept as a call of its own, and one that cannot be compared is kept with a
`SPAN_MAY_DUPLICATE` limitation — a distinct call is never dropped silently. A call recorded from a span
carries no caller frames, so it carries `LINKAGE_DEGRADED`, and the installation receipt says
`linkage="DEGRADED: …"` from the first such call on (`FRAMES` until then).

The receiver registers on an isolated Hajer provider **and on the SDK provider named**; it never
replaces the global provider or its exporters. An unavailable source or a missing `hajer[otel]` remains
configured but unverified. No vendor connector is used. After reversal the receiver is inert;
OpenTelemetry has no public processor-removal API.

The supported vocabulary is semconv 1.37+ `gen_ai.provider.name` (with `gen_ai.system` fallback),
operation, request/response model, usage, response id, and JSON `gen_ai.input.messages` /
`gen_ai.output.messages` parts. Tool-call and tool-call-response ids are linked. Legacy
prompt/completion events, vendor-specific attributes, and `code.*` frame attributes are not interpreted.
Content capture is on by default (`HAJER_CAPTURE_CONTENT=0` turns it off), and the existing exporter
performs redaction.

### The httpx fallback: `CaptureTransport`

The final fallback is an explicitly wrapped **httpx** transport:

```python
import httpx
import hajer

http = httpx.Client(transport=hajer.CaptureTransport(httpx.HTTPTransport()))
# Async: httpx.AsyncClient(transport=hajer.AsyncCaptureTransport(httpx.AsyncHTTPTransport()))
```

It recognises JSON model requests to chat completions, Responses, and messages endpoints and sends
observations through the same capture hooks. It avoids capture inside a wrapped provider call or when
another instrumentation owner is detected. It preserves stream bytes and order, records only consumed
bytes, and bounds retained response bytes by `HAJER_WRAPPED_CALL_MAX_BYTES`. Incomplete or truncated
captures are labelled. SSE semantic decoding and token counts are not verified at this fallback; the
stream bytes can be captured with content consent. Request and response headers are never recorded. It
wraps an httpx transport or an httpx2 one (the fork openai 3.x and anthropic 1.x send through) alike:
`httpx2.Client(transport=hajer.CaptureTransport(httpx2.HTTPTransport()))`.

### Streaming, and where token usage comes from

**A streamed call is recorded when the stream ends**, not when the interpreter collects it. What you get
back is a proxy that forwards the provider's own chunks and its own surface (`text_stream`, `response`,
`until_done()`, `get_final_message()`, `close()`); the record is written when the iterator is exhausted,
when you close it, or when the `with` block around it returns — so a stream you abandoned half way is
recorded as `streamComplete: false` while the operation it belonged to is still open. The chunks are the
provider's own objects; the container is not. It forwards attribute access, iteration, the
context-manager protocol and `close`/`aclose`.

Two limits: a consumer that reads only `text_stream` never sends chunks through the proxy, so
`streamChunks` is 0 (the usage still arrives, from `get_final_message()`); and OpenAI puts token usage
on the final chunk only when the request carried `stream_options={"include_usage": True}` — without it
the record says so rather than reporting zero tokens as a fact.

```python
# OpenAI: without this the usage is not on the wire, and the record says so.
stream = openai_client.chat.completions.create(
    model="gpt-5", messages=messages, stream=True, stream_options={"include_usage": True}
)
for chunk in stream:
    ...

# Anthropic: the usage is in the final message, which the proxy reads when the block returns.
with anthropic_client.messages.stream(model="claude-sonnet-5", max_tokens=256, messages=messages) as stream:
    for text in stream.text_stream:                # reads no chunks through the proxy: streamChunks = 0
        ...
    final = stream.get_final_message()             # and this is where the tokens come from
```

The [support matrix](support-matrix.md) has the `usage available` column per library, and the caveat
beside it.

### Framework chat models

**A framework chat model is instrumented at the provider client it holds.**
`hajer.wrap(ChatAnthropic(model="claude-sonnet-5"))` returns the same `ChatAnthropic` — so you can keep
handing it to whatever already takes a `BaseChatModel`, `bind_tools` included — with its inner
`anthropic.Anthropic` (`_client`) and `anthropic.AsyncAnthropic` (`_async_client`) instrumented, because
those are what `_generate` and `_agenerate` actually call. `hajer.wrap(ChatOpenAI(model=...))` does the
same at `root_client` / `root_async_client`, including the `with_raw_response.create(...)` and
structured-output `with_raw_response.parse(...)` calls LangChain makes, so the record carries the parsed
answer. The record is of the request that left the process, not of the framework's description of it.

Inner clients are looked for only when the object itself carries no recognised surface, so wrapping a
real provider client is unchanged. A chat model that keeps its client somewhere else is
`UnsupportedClientError`, not silence.

The calls go into a per-task `contextvars` context, and the next `verify` or `observe` **in the same
task** attaches them as `wrappedCalls` and clears the context — or into the enclosing `hajer.scope()`,
which is how a call your framework made in a child task reaches the submission that follows it.

### What is recorded

**Always recorded**: provider and api, model, the request settings you passed, the declared tool
names, the tool calls the model asked for and the tool results you sent back (ids, names, error
flags), wall-clock duration, token usage, response id, finish reason, the error class and message
when the call raised, retries performed at this seam, whether the call streamed, whether that stream
ran to exhaustion, and how many chunks crossed it.

**Content capture is on by default**: message text, tool-call arguments, tool-result bodies, and the
accumulated text of a stream. An embedding call is recorded as metadata only — the model, how many
inputs, and how many vectors of how many dimensions — never its input text or its vectors. Client-side
redaction runs before hosted export. The local `HAJER_OBSERVE_SINK` also applies redaction before
writing, including raw captures. It redacts a copy, never the application's response or the in-memory
wrapped call. Set `HAJER_CAPTURE_CONTENT=0` to opt out; shapes, names, counts, timing and usage remain
available.

**Only with `HAJER_CAPTURE_RAW=1` as well**: the provider's own request and response documents, sent
instead of the summary for that call. The difference is what the service can then say about it. A
summary is a description, and is recorded as a call Hajer was told about and shown no bytes of; a raw
capture is read on the server into a **model-call receipt** — provider, model, token counts and finish
reason taken from the provider's own document rather than from this SDK's reading of it. Raw capture is
strictly more disclosing than content capture, so it requires it: `HAJER_CAPTURE_RAW=1` with
`HAJER_CAPTURE_CONTENT=0` is a `HajerConfigError` at construction, not a quiet widening.

One capture is bounded by `HAJER_WRAPPED_CALL_MAX_BYTES` (request document plus response document).
Past the bound the longest texts are clipped in the middle: each keeps its head and tail with
`[hajer: N of M characters omitted here; sha256:<digest> of the whole]` between them. The request keeps
at least half the bound; a clipped *response* also sets `truncated`, which the service reads as "do not
build a receipt from this body". A prompt is never dropped whole; only a document whose *shape* alone
does not fit (thousands of messages under a tiny bound) ships as a summary, whose content is clipped
the same way. A **streamed** call never carries a capture at any setting: the server-sent-event framing
is gone below this seam, so a capture built here would be a reassembly presented as the original bytes.
The summary carries `streamed`, `streamComplete`, `streamChunks` and the usage the final chunk named.

`HAJER_WRAPPED_CALLS_MAX` caps how many calls one task accumulates. Beyond it the newest are dropped
and counted — the first calls of a workflow are usually the ones the obligation is about — and the
count travels with the submission as `wrappedCallsDropped` rather than quietly truncating.

**The frame that awaited the framework is the one recorded.** LangChain runs each `ainvoke` in a child
task (`asyncio.gather`), and a chain runs each step in its own task, so the provider call's own stack
starts at the event loop. Once a client is wrapped, the event loop's `create_task` carries the creating
task's application frames into the child, so the innermost recorded frame is your
`await llm.ainvoke(...)` line and never the framework's internals or whatever started the loop. This
covers loops derived from `asyncio.BaseEventLoop` (the standard ones); uvloop's tasks keep only their
own stacks.

### Child tasks and `scope()`

**A provider call your framework makes in a child asyncio task needs a scope.** Without one, the calls
go into a per-task `contextvars` context and a child task gets a *copy* of it, so a call recorded inside
the child never reaches the `verify` in the parent. This is not hypothetical: `langchain_core` 1.5.1's
`BaseChatModel.agenerate` fans its calls out through `asyncio.gather`, so
`await some_chat_model.ainvoke(...)` records nothing that a following `verify` can attach — measured on a
real application: four provider calls made, zero attached. `hajer.scope()` is the answer, and it is one
line:

```python
with hajer.scope(workflow="support-answer"):   # one operation: one request, one job, one turn
    reply = await agent.ainvoke(question)      # the framework may fan this out as it likes
    assessment = hajer_client.verify(...)      # and the calls are all here
```

Inside the block the sink is a mutable object, so a child task's copied context still points at the
same list. Outside every block nothing changed, and that is deliberate rather than conservative: one
sink installed at `wrap()` time would be shared by every context descended from client construction,
so in a server one request's `verify` would attach another request's model calls. Under-capture is
safe; over-attachment is not. Check your own stack with one assertion — make a call the way your code
does, then `assert hajer.wrapped_calls()` inside the scope, before you rely on the capture.

The optional `workflow` is a stable, non-secret name (1–512 characters), not an execution id. It
travels as `workflowHint` on each call through online and offline capture. Different hints are kept
apart during observed prompt-template inference even when calls share a wrapper or lack frames.
Repeated names still create distinct operations. An unnamed inner scope does not inherit a name. This
is caller-supplied guidance, not proof of static reach, side effects or accepted obligations; without
captured content there is still no prompt evidence to author from. Invalid or redacted hints are
reported and refused by extraction rather than silently pooled into another workflow.

### What it can never capture

At any setting:

- what happened to the output *after* the call — a later transformation, a template, a redaction;
- database truth, authorization state, or whether the side effect actually completed;
- the provider SDK's own internal retries: those happen below this seam, so `retries` counts retries
  *Hajer* performed, which is always zero;
- a stream you never consume, never close and never wrap in a `with`: it stays
  `stream_complete=False`, and the record says that rather than guessing.

### Recording non-model responses for replay: `record_boundaries`

A replay of a recorded trace must not reach a real dependency, so an outbound call the application made
that was not a model call is answered from what was recorded — and a call nobody recorded makes the
replay *unable to verify*, never a guess. Recording is opt-in and names its hosts:

```python
with hajer.record_boundaries(hosts=["crm.internal"]):
    answer = handle(request)        # httpx and httpx2 calls to crm.internal are recorded (sync and async)
    client.verify("refunds@3", request, answer)
```

Each response inside the block (method, URL parts, status, content type, and the body up to
`HAJER_BOUNDARY_BODY_MAX_BYTES` with the whole body's digest) goes into that submission's evidence under
`hajerBoundaryResponses`, where the service's redaction walks it like any other evidence. A streamed
response is recorded without its body. Model hosts are refused: `wrap` records those. Only httpx and
httpx2 are recorded; `requests` and `urllib` calls are not.

## Client-side redaction

**It is on by default.** Before a submission leaves this process, the SDK runs the same detector catalog
the service runs — card numbers, IBANs, VINs, national ids, credentials, email addresses, phone numbers,
long digit runs — and replaces what it finds. The service removes these shapes too, but it removes them
*after* they have crossed a network, sat in a buffer and been read into a request body. One hop earlier
is the difference between a card number that was exposed and one that never left.

The catalog is `redaction-rules@6`, generated from the platform's committed rule set into
`hajer/_rules.py`, so a stored observation's `clientRedaction` report names a rule set that really was in
force. `contract/redaction-vectors.json` holds the shared positive and negative cases the client is
tested against. `python -m hajer doctor` prints the catalog id this build
carries.

**The placeholder is fixed.** A match becomes `[redacted:CARD]`, `[redacted:IBAN]`, `[redacted:EMAIL]` —
`[redacted:<CATEGORY>]`, and `[redacted:UNSCANNED]` for a value the pass could not read at all, which is
the one placeholder that means *we did not look* rather than *we found one*. It is not configurable,
deliberately: receipts and tests on both sides compare on that exact text, and placeholders match no
rule, so `redact(redact(x)) == redact(x)` and a second pass cannot inflate a count.

**It never raises into your code.** An unknown validator, a rule that will not compile, a document past
one of the pass's four budgets: the pass does what it can, records the reason, and sets
`clientRedaction.degraded` on the wire — which arrives back in `assessment.limitations`, so a caller who
believed their payload was redacted and whose pass degraded is told. A redaction pass that threw would
make Hajer the reason somebody's email did not send.

The budgets are constants, not settings: `REDACT_MAX_BYTES` (1 MiB), `REDACT_MAX_NODES` (50,000),
`REDACT_MAX_DEPTH` (64) and `REDACT_MAX_STRING_CHARS` (65,536). They bound a pass that runs inside your
request path, and an environment variable that could raise them is an environment variable that could
turn a 1 MB document into a 100 MB one.

### The policy

```python
import hajer

policy = hajer.build_policy(
    classes_off=("ACCOUNT_LIKE",),                              # a class this team has decided about
    extra_rules=(("CUSTOMER_REF", r"\bCUS-[0-9]{6}\b"),),       # a shape of your own
    paths_exempt=("request.accountNumber",),                    # a value a check has to be able to read
)

client = hajer.Hajer(policy=policy)                             # every submission from this client
client.verify("refund-policy@1", request, output, evidence, policy=hajer.build_policy())  # or just this one
```

- `classes_off` turns off a catalog class, exactly as the server's own policy would. A team whose order
  ids are written in groups of four turns `ACCOUNT_LIKE` off.
- `extra_rules` are `(category, pattern)` pairs, compiled once when the policy is built. A pattern that
  does not compile is refused **there**, where you wrote it, rather than degrading every later call.
- `paths_exempt` is how a verifier's *decisive* fields stay readable: a check that compares an account
  number cannot compare a placeholder, so name the paths your checks read and those values travel. The
  service's own content contract is what protects them from there.

`build_policy` is a constructor rather than a bare dataclass because the two things that can be wrong
with a policy — a pattern that will not compile, a path the grammar cannot read — are yours, and belong
at the point you make them. Inside `verify` there is no good answer to either.

To see what a pass would remove before you send anything:

```python
document, entries = hajer.redact_document({"note": "card 4111111111111111"}, policy=hajer.build_policy())
# {'note': 'card [redacted:CARD]'}   (RedactionEntry(category='CARD', path='note', count=1),)
```

`redact_document` always returns the same pair, `tuple[JsonValue, tuple[RedactionEntry, ...]]`, whatever
happened. `RedactionEntry.category` is the class name; the wire key is `class`, which Python cannot use
as an attribute name.

**Turning it off**: `HAJER_REDACT_CLIENT=0`. Then the wire carries no `clientRedaction` report at all —
an absent report says *this client did not redact*, which a report of zeroes could not — and the
service's own pass is the only one that runs. An entry is a count and a path; it is never a value.

## Attach mode

`wrap` and `verify` are lines somebody writes. Attach mode is for the process nobody has instrumented
yet: **every provider call made outside a `hajer.scope()` becomes one `observe` observation of its own,
with no verifier.** The tuple is recorded; nothing is verified, because nothing was declared to verify it
against. It is how you find out what the workflows are before you have written an obligation about any of
them — and `python -m hajer tail --follow` prints them as they land.

```bash
# no code change at all: the shim is a sitecustomize.py the interpreter imports at start-up
PYTHONPATH="$(python -m hajer attach-path)" HAJER_ATTACH=1 python -m your_app

# and in another terminal, one line per observation as they land
python -m hajer tail --follow
```

```python
import hajer.autoattach     # or one line in the entry point; it reads HAJER_ATTACH, so it is safe to keep
hajer.attach()              # or explicitly, which does not consult the variable
```

It instruments the **classes** `openai`, `anthropic`, `langchain_anthropic` and `langchain_openai` export
— including the Azure, Bedrock and Vertex constructors, and the LangChain chat models at their inner
provider client — so every client the process builds after that point is instrumented at construction. A
library that is not imported yet is instrumented the moment it is, through a `sys.meta_path` finder that
wraps the loader the ordinary machinery chose. No provider library is imported to do any of it.

**What leaves the process by default**: one observation per call carrying provider, api, model, the
request settings you passed, declared tool names, message count and roles, response id, finish reason,
token usage, duration, the tool *names* the model asked for, and the error class and message when the
call raised. **Message text, tool arguments and tool results are captured by default**, then client-side
redaction runs before hosted export. `HAJER_CAPTURE_CONTENT=0` omits that content. Provider bytes remain
opt-in: `HAJER_CAPTURE_RAW=1` adds the provider's own request and response documents, and only then does
the service mint a model-call receipt from them. The tuple itself never carries content at any setting.

With no `HAJER_API_KEY` / `HAJER_TEAM_ID`, or with `HAJER_DISABLED=1`, an attached process is inert: the
records exist for `hajer.wrapped_calls()`, every observation is a `disabled` receipt and no socket opens.

**It is opt-in.** An observation is your data leaving your process, so attach mode is never on because a
key happens to be present.

### litellm and google-genai

`google.genai.Client` is instrumented like any other class: `models.generate_content`,
`models.generate_content_stream` and their `aio` twins, sync and async, with the usage arriving in the
final chunk's `usage_metadata`. A client you constructed *before* `attach()` is reached with
`hajer.wrap(client)` instead.

litellm is the one that is not a class, and it is the one that needs a rule. Its entry point is a module
function rather than a client, so what attach mode replaces is `litellm.completion` (and `acompletion`)
on the module. A module of yours that did `from litellm import completion` bound the *function object*
into its own namespace at import time, and that binding never consults `litellm` again — so attach also
rebinds every such alias that still points at the original, in every module already loaded. **Import
order therefore does not decide whether your calls are captured**, whichever of the two spellings you
use:

```python
import litellm                        # litellm.completion(...)  — captured
from litellm import completion        # completion(...)          — captured, before or after attach()
```

Two cases it cannot reach, both narrow: a reference you took into a **local variable** or a container
before attaching (`fn = completion` inside a function that is already running), and an alias created
**after** `hajer.detach()`. `hajer.wrap(litellm)` is the explicit form if you would rather name the
library than attach the process.

### What `hajer.detach()` does, and what it does not

`detach()` stops the **observing**. It is not an undo:

- A patched constructor stays patched, and inert. Another thread may be inside it; unpatching under that
  thread is a crash, and a crash in somebody else's process is worse than a no-op.
- **A client you had already built stays instrumented.** That is `wrap`'s own behaviour, and detaching
  the process does not reach into an object `wrap` returned.
- The module functions and the rebound aliases stay rebound, for the same reason.
- An alias created **after** `detach()` points at the original function and is not captured.

What stops is the sending. After `detach()` the records go where `hajer.wrapped_calls()` reads them and
nowhere else, and `hajer.attachment()` returns the attachment (or `None`) so a process can ask what it is
in rather than guess. If you need a reversible installation, use `hajer.instrument()` /
`hajer.uninstrument()`.

## The command line

`python -m hajer` has no configuration file. Its subcommands are `attach-path` (above), `doctor`, `tail`
and `proxy`.

### `python -m hajer doctor`

The first command to run when nothing is arriving.

```bash
python -m hajer doctor           # prose
python -m hajer doctor --json    # the same facts, as one document
python -m hajer doctor --emit    # one authenticated synthetic observation; requires a team key
```

```
hajer 0.1.0
wire            generated from contract/openapi.json at sha256:9958b76da62c…
redaction       redaction-rules@6
base url        https://api.hajer.ai/api/health -> no answer (keyless GET)
inert           yes
                HAJER_API_KEY and HAJER_TEAM_ID absent: verify returns unavailable{DISABLED} and observe is a no-op

setting                    variable                           source   value
api_key                    HAJER_API_KEY                      default  absent
team_id                    HAJER_TEAM_ID                      default  absent
base_url                   HAJER_BASE_URL                     default  https://api.hajer.ai
deadline_ms_default        HAJER_DEADLINE_MS_DEFAULT          default  1500
…
redact_client              HAJER_REDACT_CLIENT                default  true
proxy_timeout_s            HAJER_PROXY_TIMEOUT_S              default  60
attach                     HAJER_ATTACH                       default  false
tail_interval_ms           HAJER_TAIL_INTERVAL_MS             default  1000
tail_limit                 HAJER_TAIL_LIMIT                   default  50
disabled                   HAJER_DISABLED                     default  false
```

That is the output on a shell with nothing exported. Export the two variables and the top lines change:
`inert` reads `no`, and `base url` reads `-> 200 (keyless GET)` against a service that is up.

It prints the SDK version, where the wire types came from, the redaction catalog this build carries,
whether `HAJER_BASE_URL` answers a **keyless** GET of the liveness probe (a 401 is a fine answer —
something is listening, and the key is a separate question), whether this client would be inert and why,
and then **every `HAJER_*` setting with the value in force and where it came from**: `env` or `default`.
That last column is the point. A value on its own cannot tell a variable you forgot to export from a
default that happens to match it, and that is most of what "the SDK isn't working" turns out to be.

Doctor also runs one local synthetic provider call through the actual instrumentation and redaction
path, and prints `selfCheck.recorded`, `wouldEmit`, and the redacted trace preview. It does not submit
that trace. This proves local wiring only; a hosted ingest acknowledgement is what proves delivery.

`--emit` makes one POST with one synthetic trace through the normal bounded, redacted observe path; it
does not retry or run the health probe. Missing credentials send nothing and print
`configured but unverified` with exit code **6**. The server's receipt currently returns observation ids
but no permalink: the SDK reports the accepted id, never invents a URL, and still exits **6**. HTTP
errors and malformed receipts also remain unverified.

It sends nothing else, and it never prints your key: `api_key` reads `set` or `absent`.

### `python -m hajer tail`

```
2026-09-19T07:21:55.174187Z       ing-36f84d853579edf7b88904a7991d84cc      OBSERVE  -                         anthropic/claude-sonnet-5           redactions=0    OUTPUT_OBSERVED       assessed=no
```

One line per observation, oldest first: the instant, the observation id, the mode, the verifier it named
(`-` for none), the provider and model its model-call receipt names (`-` when the calls arrived as
summaries, so there is no receipt), how much redaction removed, ingest's reading of the tuple, and whether
an assessment exists. Never the request, the output or the evidence.

`--since` is an ISO instant and is **inclusive**: one `observe` flush writes its rows in a single
transaction and they share a `createdAt`, so a strictly-after read would never show you the rest of a
flush you had seen one row of. The tail therefore drops the ids it has already printed at the boundary
instant — which is also what your own loop should do if you call `observations()` directly. `--follow`
polls every `HAJER_TAIL_INTERVAL_MS` and Ctrl-C ends it; `--limit` sets the page size, and the server
clips it to its own bound. With no `HAJER_API_KEY` / `HAJER_TEAM_ID` it says so on stderr and exits 2:
this is a read of a team's own observations and it cannot be done anonymously.

### `python -m hajer proxy`

Everything above is a **self-report**: your process says what it asked a provider and what came back,
and Hajer believes it. That is the right default — it needs no credential of Hajer's, no traffic through
Hajer and no trust from you — and it is exactly as strong as the process reporting it.

The proxy is the other option, and it is opt-in per process. Every exchange through it is recorded with
`origin: BROKER`: read at the wire rather than reported by the application.

```bash
python -m hajer proxy --upstream https://api.anthropic.com --listen 127.0.0.1:8091
```

```
hajer proxy: https://api.anthropic.com on http://127.0.0.1:8091 (origin BROKER)
```

Then point your own workflow at it and change nothing else:

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:8091 python your_workflow.py
```

A broker receipt witnesses the exchange at the wire, not the application's use of it. A proxy cannot
tell you what your code did with the answer.

**Loopback only.** `--listen` accepts `127.0.0.1`, `::1` or `localhost` and refuses anything else by
name, with the reason: this process forwards *your* provider credential, so binding it where another
machine can reach it makes it a credential-stealing endpoint. Tunnel if something else has to reach it.

**Your credential stays yours.** The `x-api-key` / `authorization` / `api-key` header is forwarded to
the upstream unchanged and is never in a recorded header, a captured body or a log line. Hajer holds no
provider credential for this and could not make a call of its own through it.

**Request headers are an allowlist**, because a denylist is a list of the headers somebody thought of.
`content-type`, `accept`, `anthropic-version`, `anthropic-beta` and `user-agent` go up;
`accept-encoding` is stripped so the body this records is the body the provider meant. On the way back
`content-length`, `content-encoding`, `transfer-encoding`, `connection` and `keep-alive` are dropped,
because the bytes a WSGI server re-frames are not the bytes those describe. `--strip-header` adds to the
strip set and `--pass-header` adds to the allowlist; both are repeatable.

**An absolute or protocol-relative path is refused.** A proxy that forwarded `http://…` or `//host/…`
would be an open relay, and an open relay inside somebody's network is a worse problem than an
unobserved provider call.

**Streams pass through**, chunk by chunk and unbuffered, so your own streaming UI still streams; the
recorded body is the stream as it went past.

**Content is recorded by default** — the same `HAJER_CAPTURE_CONTENT` switch `wrap()` obeys. With
`HAJER_CAPTURE_CONTENT=0` the observation carries the exchange's shape — method, path, status, timing
and the **usage**, which is always recorded — and no message text. With capture on, the provider's own
bytes are the capture, and the service reads *them* into a model-call receipt rather than taking a
summary's word for it.

That is the trade: only a capture can be priced, because the price comes from the tokens the *service*
read (`costSource: LOCAL_PRICED`). With content capture off a broker exchange arrives as a summary,
carrying a limitation that says a receipt cannot be minted from it. Broker-grade receipts require
content disclosure; opting out prevents that receipt.

**An upstream failure answers 502** with a structured body — `{"error": {"type":
"hajer_proxy_upstream", "status": …, "message": …}}` — so it is not mistaken for the provider's own
502, and the exchange is recorded with status 502 rather than dropped. Every response this proxy
returns carries `x-hajer-proxy: 1`.

`--timeout 120` overrides `HAJER_PROXY_TIMEOUT_S` for this process. With no
`HAJER_API_KEY` / `HAJER_TEAM_ID` the proxy still runs and still forwards: it says on stderr that
exchanges are kept in memory only, because a proxy you started in order to observe yourself should not
be the thing that silently observes nothing.

```
usage: hajer proxy [-h] --upstream UPSTREAM [--listen LISTEN]
                   [--timeout TIMEOUT] [--strip-header STRIP_HEADER]
                   [--pass-header PASS_HEADER]

Record provider exchanges at the wire.

options:
  -h, --help            show this help message and exit
  --upstream UPSTREAM   the provider base URL, e.g. https://api.anthropic.com
  --listen LISTEN       host:port; loopback only
  --timeout TIMEOUT     upstream read timeout in seconds
  --strip-header STRIP_HEADER
                        a header not to forward (repeatable)
  --pass-header PASS_HEADER
                        a header to forward (repeatable)
```

## Settings

Every bound is a `HajerSettings` field with an environment variable. Nothing in this package spells a
limit as a literal anywhere else. `HajerSettings.from_env()` is the only place the process environment is
read; you can also construct `HajerSettings(...)` yourself and pass it to the client.

| Variable | Default | What it is, and when to change it |
| --- | --- | --- |
| `HAJER_API_KEY` | — | Your team API key. Absent → inert. |
| `HAJER_TEAM_ID` | — | The team every route is scoped to. Absent → inert. |
| `HAJER_BASE_URL` | `https://api.hajer.ai` | The service. Point it at a local or self-hosted platform ([local-platform.md](local-platform.md)). |
| `HAJER_ENVIRONMENT` | absent | The environment this process runs in — `production`, `staging`, `dev` — sent as `environment` on every observation. Your team chooses which environments' recorded calls may become test inputs (`production` until it says otherwise), so set it in every environment you want Hajer to learn from; a process that names none is never one, and CI and replay traffic (`ci`) never is. Folded to lower case; anything that is still not lower-case letters, digits and dashes (at most 64, a letter or digit first) is dropped, never raised, and `python -m hajer doctor` prints it as `ignored: invalid`. |
| `HAJER_DEADLINE_MS_DEFAULT` | `1500` | The budget a `verify` uses when it names none. 1500 ms suits a deterministic check; it is **far** below what a semantic judge can answer inline (a complete judge answer takes on the order of fifteen seconds), so a synchronous `verify` runs deterministic checks and the semantic ones arrive through `observe`. Raise it only against a route whose measured p95 fits. |
| `HAJER_OBSERVE_QUEUE_MAX` | `1000` | Observations held in memory before the newest is refused. Raise it if your traffic is bursty and your memory budget allows; lower it if a queued observation going stale matters more than losing it. |
| `HAJER_OBSERVE_FLUSH_INTERVAL_MS` | `2000` | How long a partial batch waits. Lower for fresher data, higher for fewer requests. |
| `HAJER_OBSERVE_BATCH_MAX` | `32` | Observations in one flush. Bounded by `HAJER_BODY_MAX_BYTES` as well, whichever binds first. |
| `HAJER_OBSERVE_FLUSH_DEADLINE_MS` | `5000` | The deadline of one flush request, and of one `poll`. |
| `HAJER_OBSERVE_BACKOFF_INITIAL_MS` | `200` | The first wait after a failed flush. |
| `HAJER_OBSERVE_BACKOFF_MAX_MS` | `30000` | The ceiling doubling backoff never exceeds. |
| `HAJER_OBSERVE_SINK` | absent | A directory every settled wrapped call is written to **instead of sent**: one file per process, rewritten after each call, in the shape the platform's observation export reads. It opens no socket and reads no credential, so a test suite that patches its transports session-wide can be exported: the suite exercises the workflows for free, and the calls it makes are observations like any other. Run entries are one per `hajer.scope()`, plus `process` for everything settled outside every scope. |
| `HAJER_BODY_MAX_BYTES` | `65536` | A body larger than this is refused locally, before the socket, as `BODY_OVER_BOUND`. It mirrors the server's own ingest limit: the whole tuple has to reach a judge inside one model message. |
| `HAJER_CAPTURE_CONTENT` | `1` | Message content, tool arguments and tool results are captured by default; client-side redaction runs before hosted export. Set `0` to opt out. |
| `HAJER_CAPTURE_RAW` | `0` | Send the provider's own request and response documents for each wrapped call, so the service reads them into a model-call receipt instead of taking the summary's word for it. Requires `HAJER_CAPTURE_CONTENT=1`: raw bytes are strictly more disclosing, so the narrower switch has to be on first. |
| `HAJER_CAPTURE_HTTP` | `0` | Also record every outbound HTTP request the application makes through httpx's or httpx2's default transports (a search API, a data vendor) as a call: `provider="http"`, the method, the scheme and host, the path as a template (a segment that is a number becomes `{id}`, one the redaction catalog recognises `{redacted}`, a long random-looking one `{token}` — webhooks and bots carry their credential in the path), the status and where it was made. Never a header, never the query string, never a body. A wrapped model call's own request is not recorded twice, and the SDK's own requests never. Takes effect at the first `wrap`, `attach` or `instrument`. |
| `HAJER_CAPTURE_CALL_SITE` | `1` | Record where each wrapped call was made; see [call-site capture](#call-site-capture-what-leaves-the-process). |
| `HAJER_WRAPPED_CALL_MAX_BYTES` | `32768` | What one raw capture may carry: request document plus response document (and one summary's content). Past it the longest texts are clipped in the middle, keeping head and tail around their full length and SHA-256, and the capture is marked clipped; a prompt is never dropped whole. Well under `HAJER_BODY_MAX_BYTES` because several captures and the tuple all have to fit inside one body — lower it when a workflow makes many large calls, raise it when it makes one. |
| `HAJER_WRAPPED_CALLS_MAX` | `32` | Wrapped calls one task — or one `scope()` — accumulates before further ones are counted and dropped. |
| `HAJER_BOUNDARY_BODY_MAX_BYTES` | `16384` | Bytes of one recorded non-model response body kept inside `hajer.record_boundaries()`; the digest and size cover the whole body and `truncated` says when it was cut. |
| `HAJER_BOUNDARY_RESPONSES_MAX` | `32` | Recorded non-model responses one submission carries; later ones are counted as `dropped`. |
| `HAJER_REDACT_CLIENT` | `1` | **On.** Run the detector catalog over the submission in this process, before anything is sent. `0` turns it off and the wire then carries no `clientRedaction` report at all, so a reader can tell *this client did not redact* from *this client redacted nothing*. Turn it off when the service's own pass is the only one you want, not to make a value travel — `paths_exempt` on a policy is that. |
| `HAJER_PROXY_TIMEOUT_S` | `60` | How long `python -m hajer proxy` waits for the upstream before it answers 502. `--timeout` overrides it for one process. It affects nothing but the proxy. |
| `HAJER_ATTACH` | `0` | The switch the import-time hook reads: with it on, `import hajer.autoattach` (the one line, or the `sitecustomize` shim) attaches this process. `hajer.attach()` in code does not consult it. |
| `HAJER_TAIL_INTERVAL_MS` | `1000` | How often `python -m hajer tail --follow` asks for newer rows. |
| `HAJER_TAIL_LIMIT` | `50` | Rows per `tail` page. The server clips it to its own page bound. |
| `HAJER_DISABLED` | `0` | Inert regardless of the key. The kill switch. |

Booleans accept `1/0`, `true/false`, `t/f`, `yes/no`, `y/n`, `on/off`, any case — the same spellings for
every boolean setting. A value that is neither a number nor a boolean where one is required raises
`HajerConfigError` when the settings are read — at construction, never during a call.
`python -m hajer doctor` prints each of these with the value in force and whether it came from the
environment or from the default above.

## What a verification costs, and who bounds it

A deterministic check costs nothing but the round trip. A **semantic** check reaches a judge, which is a
provider call, which is money — and `assessment.cost_microusd` is what that one answer cost, in
microUSD, settled from the provider's own token counts rather than estimated.

That spend is bounded on the server by a **campaign ledger**. These are platform settings, set by
whoever operates the Hajer platform, not SDK settings:

| Platform variable | Default | Bounds |
| --- | --- | --- |
| `LOCAL_HARNESS_CEILING_MICROUSD` | 5,000,000 µUSD (USD 5) | one whole campaign |
| `VERIFY_LANE_MICROUSD_MAX` | 1,000,000 µUSD (USD 1) | what *customer verification traffic* may commit inside that campaign |
| `VERIFICATION_MICROUSD_MAX` | 338,780 µUSD | what any **one** verification may reserve |

The middle row is a lane, held apart from the lane a scan spends on: a scan that exhausts its budget
stops work an operator started, and a verify lane that exhausts its budget stops answering production
traffic. One total would let either starve the other silently, and the receipt afterwards could not say
which one did.

Every call **reserves before it is sent** and settles at what it actually cost, so an exhausted ceiling
is discovered before the money is spent rather than after. A reservation is a conservative upper bound
over the bytes about to go out, which is why the per-verification number is larger than what a
verification settles at.

The platform's own ceiling on that setting is `MAX_CEILING_MICROUSD` — 50,000,000 µUSD, USD 50 — a
constant and not a setting. No configuration can put a campaign above it.

When a ceiling binds you get `unavailable{BUDGET_EXHAUSTED}`, which is a refusal and not a verdict. The
SDK will not report an assessment it could not obtain as `satisfied`; what to do about it is your
policy, the same as every other `unavailable`.

## Errors

The SDK raises in four places, all under `hajer.HajerError`:

- `HajerConfigError` — a `HAJER_*` value is not the type its setting declares. Code
  `HAJER_INVALID_CONFIGURATION` names the variable and expected format. The supplied value is withheld
  from the message and exception attributes; `.value` is always `[withheld]`.
- `UnsupportedClientError` — `wrap()` found no surface it recognises. Returning the client
  uninstrumented would mean silently capturing nothing forever.
- `AssessmentUnavailableError` — only with `raise_on_unavailable=True`.
- `BodyOverBoundError` — internal; `verify` and `observe` turn it into an assessment or a receipt and
  it never reaches your code through them.

## Call-site capture: what leaves the process

With `HAJER_CAPTURE_CALL_SITE` on (the default), each wrapped call carries where it was made: up to 8
application frames (module, qualified name, file relative to the project root, line), the provider
endpoint's `host:port`, and **for each frame whose file is under the project root, the SHA-256 digest of
that file's bytes**.

The digest is sent only while the file is a regular file under the project root, last modified or
replaced (`max(mtime, ctime)`) at least 2 s before the process started, and unchanged since first read.
"The process started" is the operating system's process start, not the SDK import; for a forked worker
(gunicorn `preload_app`, celery prefork, a fork of a fork) it is the earliest start of the ancestors it
was forked from without an `exec` (`PF_FORKNOEXEC` on Linux, `P_EXEC` on macOS), and unknown for an
orphaned fork child. Where the start cannot be read (no `/proc`, `sysctl`, `GetProcessTimes`, or an
interpreter without `ctypes`), no module loaded before `hajer` gets a digest. The service uses the digest
to read the line in the indexed revision that really ran.

File digests therefore leave the process: they reveal nothing of the source text, but anyone holding
the same file can recognise it. No digest is ever sent for a file outside the project root. A path is
compared with the project root as spelled and then with its symlinks resolved, so a checkout reached as
`/tmp/app` and as `/private/tmp/app` (macOS) is one root. `HAJER_CAPTURE_CALL_SITE=0` sends none of
this. A `scope` that declares a workflow key declares it for every call inside it: one scope means one
call site.
