# @hajer/sdk — the TypeScript SDK

At the point where a model output crosses into a side effect — an HTTP response, an email, a database
write — hand Hajer the request, the output and the evidence you chose. Hajer applies a named, versioned
**verifier** and returns an **assessment**. Your application decides what to do with it.

This package is that call, in TypeScript, over the same wire and the same redaction catalog as the
[Python SDK](../python/README.md). `src/wire.ts` is generated from the platform's OpenAPI snapshot
(`contract/openapi.json`) — the same snapshot `hajer/_wire.py` is generated from — and `src/rules.ts`
from the same detector catalog. Neither is hand-written, on either side, which is what keeps two SDKs
from disagreeing about what a submission is.

The Python SDK is the published one (`pip install hajer`). This package is **not yet on npm**.

## Install

Until it is published, install from a checkout of this repository:

```bash
npm install ./typescript
```

Or as a dependency of your own project, which is the form that survives a lockfile:

```json
{ "dependencies": { "@hajer/sdk": "file:../hajer-sdk/typescript" } }
```

When the package is published, `npm install @hajer/sdk` replaces those lines and nothing else on this
page changes.

Node ≥ 22. `openai` and `@anthropic-ai/sdk` are optional peer dependencies: the SDK never imports
either one — `wrap(client)` instruments the object you hand it, by property.

## Configure

The same three environment variables as the Python SDK: `HAJER_API_KEY` and `HAJER_TEAM_ID` (from your
Hajer team settings; contact the Hajer team if you do not have access to a team yet), and optionally
`HAJER_BASE_URL`, which defaults to `https://api.hajer.ai` and is set only for a local or self-hosted
platform.

## The five lines

```ts
import { Hajer, wrap } from "@hajer/sdk";
import OpenAI from "openai";

const hajer = new Hajer();                 // HAJER_API_KEY, HAJER_TEAM_ID from the environment
const openai = wrap(new OpenAI());         // the model calls are recorded beside the answer

export async function handleTicket(ticket: Ticket, order: Order) {
  const completion = await openai.chat.completions.create({
    model: "gpt-5",
    messages: [{ role: "user", content: ticket.question }],
  });
  const reply = completion.choices[0].message.content;

  const assessment = await hajer.verify(
    "refund-policy@1",                                         // the verifier, pinned: there is no "latest"
    { ticketId: ticket.id, question: ticket.question },         // the request
    reply,                                                     // the output, as it will be sent
    { orderState: order.state, approvedPolicy: order.policy },  // the evidence you chose
  );
  if (assessment.status === "violated") {
    return holdForReview(reply, assessment.findings);
  }
  return send(reply);
}
```

The same three things the Python README says about the same lines, because they are the whole design:
the evidence fields are **named**, never a `toJSON()` of the order; `verify` is called on the payload
that is **about to be sent**, after every transformation; and the **application decides** — `verify`
answers, your `if` acts, and there is no option here that makes it hold, retry or send on your behalf.

Two differences from the Python spelling, both forced by the language:

- Everything is `async`. There is no synchronous `verify`, because there is no synchronous HTTP client
  worth having in Node.
- The request, output and evidence are already camelCase, which is the wire's own spelling. The Python
  SDK converts; here there is nothing to convert.

## Inert without a key

Without `HAJER_API_KEY` and `HAJER_TEAM_ID` — or with `HAJER_DISABLED=1` — the client is **inert**:
`verify` resolves to `{ status: "unavailable", reason: "DISABLED" }` immediately with no socket,
`observe` returns a receipt in state `disabled` and queues nothing, and nothing throws. That is what
lets the integration land in a repository whose test suite has no Hajer credentials and pass unchanged.
A missing key is not a configuration error.

`verify` never throws, for the same reason it never raises in Python: transport failure, a timeout, a
5xx, a body over the bound and a missing key are each an `unavailable` assessment whose `reason` says
which, and `decidedLocally` says whether this process or the service decided it. The reason table is in
the [Python reference](../python/docs/reference.md#verify-never-raises) and it is the same table — one
vocabulary, two clients.

## Call-site capture: what leaves the process

With `HAJER_CAPTURE_CALL_SITE` on (the default), each wrapped call carries where it was made: up to 8
application frames (module, qualified name, file relative to the project root, line), the provider
endpoint's `host:port`, and **for each frame whose file is under the project root, the SHA-256 digest of
that file's bytes**. The digest is sent only while the file is a regular file under the project root,
last modified or replaced (`max(mtime, ctime)`) at least 2 s before the process started, and unchanged
since first read; the service uses it to read the line in the indexed revision that really ran. File
digests therefore leave the process: they reveal nothing of the source text, but anyone holding the same
file can recognise it. No digest is ever sent for a file outside the project root.
`HAJER_CAPTURE_CALL_SITE=0` sends none of this. A `scope` that declares a workflow key declares it for
every call inside it: one scope means one call site.

## Development

From `typescript/`:

```bash
npm ci            # or `just sync`: the exact-pinned dev tools (typescript, vitest, openapi-typescript)
just check        # the generated wire is current, then tsc strict
just test         # vitest, fakes only: nothing opens a socket or spends money
just generate-wire   # after ../contract/openapi.json moves: regenerate src/wire.ts and commit it
```

```
typescript/
├── src/wire.ts       GENERATED from ../contract/openapi.json — never hand-edited
├── src/rules.ts      GENERATED from the platform's detector catalog — never hand-edited
├── src/checksums.ts  GENERATED alongside rules.ts: Luhn, IBAN mod-97, VIN — a shape says could be, a checksum says is
├── src/redact.ts     the walk, the budgets and the policy, over src/rules.ts
├── src/client.ts     Hajer: verify, observe, scope, inert without a key, a bounded observe queue
├── src/attach.ts     the openai and @anthropic-ai/sdk clients, instrumented by property
├── src/callSite.ts   call-site capture
├── src/caseKey.ts    ck1, derived before anything rewrites the request
├── src/payload.ts    the body, typed by src/wire.ts, and the idempotency key
├── src/queue.ts      the bounded observe queue and its receipt
├── src/jsonpath.ts   one path grammar, the subset the platform declares
├── fixture-app.ts    the five lines as a runnable application, for end-to-end checks against a live platform
├── test/*.test.ts    vitest, fakes only: inertness, the five lines, redaction, attach, call site, wire
└── justfile          sync · check · test · generate-wire
```

`src/rules.ts` and `src/checksums.ts` are generated by tooling in the platform repository, not here
(`contract/README.md`). What is generated is the catalog as data; `src/redact.ts` is the walk over it —
the same split the Python SDK makes between `_rules.py` and `_redact.py`. A generated file with
hand-written logic inside it is a file somebody eventually edits and a generator eventually overwrites.

**How the two clients are held together.** `contract/redaction-vectors.json` holds positives and
negatives with the class each reports and the exact text it becomes; `test/redact.test.ts` and the
Python SDK's `tests/test_redact_vectors.py` both read it. A rule the two clients disagree about is a red
test on both sides rather than a `clientRedaction` report naming a catalog whose rules one of them did
not run.
