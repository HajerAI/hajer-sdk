# CLAUDE.md — python/

Guidance for contributors and coding agents working in `python/`. `README.md` and `docs/` are the
user-facing documentation; this file has the rules. Most of them are **enforced** — ruff and
basedpyright (`pyproject.toml`), vulture, deptry, the lockfile check and the contract check, all in
`just check` and in `.github/workflows/python.yml`. When a gate fails, fix the code to match the
convention; do not loosen the gate.

## Commands (run from `python/`)

```bash
just sync                 # uv sync --locked --all-extras
just check                # fmt + lint + typecheck + deadcode + deps + lock-check + contract-check — before every commit
just test                 # pytest; `just test -k wrap` for a subset
just contract-refresh     # ../contract/openapi.json → hajer/_wire.py (generated; commit it with the change)
just contract-check       # non-mutating drift check of that chain
just test --engine -m engine   # the golden eval test: Node >= 22.22 on PATH, installs the pinned engine from npm once
just evals-pin 0.124.0    # move the engine pin (hajer/evals/engine/package.json + lockfile), then run the golden test
```

Python ≥ 3.11 · uv · httpx · pydantic v2. Runtime dependencies are **httpx and pydantic, and nothing
else**; `openai` and `anthropic` are optional extras the SDK never imports (ruff bans importing
them). A third runtime dependency is a decision, not an edit. `httpx2`, the httpx fork openai 3.x and
anthropic 1.x send through, is not one: `_http_libraries.py` imports it when the interpreter has it,
and every seam that patches or recognises httpx (HTTP capture, `record_boundaries`)
does the same to it.

## Why this package is small on purpose

It runs inside a customer's request path. Every line here is code somebody else's product executes on
its way to sending an email, so the defaults are the conservative ones and the surface is the
smallest one that does the job. Two consequences that shape everything below:

- **It does not raise into application code.** `verify` returns `unavailable`; `observe` returns a
  receipt. The four places it may raise are in `_errors.py`, each with the reason silence would be
  worse. A new `raise` needs the same justification written next to it.
- **It decides nothing.** The SDK never continues, holds, retries or sends on the application's
  behalf, and never reports an unavailable assessment as satisfied. What to do with an assessment is
  the customer's policy, in the customer's `if`.

## Layout: one concern per module, all private but `__init__`

The core modules (not an exhaustive list — capture has its own):

```
hajer/
├── __init__.py      the public API and nothing else — __all__ is the contract
├── autoattach.py    public, no underscore: `import hajer.autoattach` is the one-line attach hook
├── __main__.py      `python -m hajer` — doctor, tail, proxy, attach-path
├── _bootstrap/      sitecustomize.py — the same hook, where the interpreter finds it on PYTHONPATH
├── _settings.py     HajerSettings; the ONLY reader of the process environment
├── _errors.py       every exception, and why each one is allowed to exist
├── _models.py       Assessment, ObservationRow, Finding, MissingEvidence, CheckOutcome + vocabularies
├── _json.py         JsonValue / JsonObject — the one name for "survives a JSON round trip"
├── _paths.py        the routes the SDK calls (SDK_OPERATIONS), and the package version
├── _transport.py    Deadline, encode_body, SyncTransport, AsyncTransport
├── _wrap.py         wrap(), the WrappedCall record, the task context, scope() and the settled hook
├── _attach.py       attach()/detach(): the class patches and the import hook, provider-library-free
├── _queue.py        the observe queue, its bounds and backoff, and the two receipts
├── _payload.py      the request bodies, the idempotency derivation, the assessment parsing
├── _client.py       Hajer and AsyncHajer
├── _redact.py       the client-side redaction walk, its budgets and the policy
├── _telemetry.py    workflow()/component()/tool(): the span emitter, OTel-free; eval_binding(), flush()
├── _telemetry_otel.py   the OpenTelemetry half of it, loaded only when `hajer[otel]` is installed
├── evals/           `hajer eval` (`hajer[evals]`): the pinned promptfoo engine (engine/package.json + lockfile),
│                    the metadata conventions and `beforeAll` hook, the payload, the upload, the CLI
├── _rules.py        GENERATED from the platform's redaction catalog — never hand-edited
├── _checksums.py    GENERATED alongside _rules.py — never hand-edited
└── _wire.py         GENERATED from ../contract/openapi.json — never hand-edited
```

Everything but `__init__.py`, `autoattach.py` and `__main__.py` is private (leading underscore). A name
a customer may use is exported from `__init__.py` and listed in `__all__`; a name that is not there is
not API and may change. `autoattach` has no underscore because it is meant to be written in somebody's
code, and `_bootstrap/sitecustomize.py` is meant to be *found* rather than imported by name.

Import direction is one way and there are no cycles: `_json` / `_paths` ← `_errors` ← `_settings` ←
`_transport` ← `_wrap` ← `_payload` ← `_queue` ← `_client` ← `_attach` ← `__init__`. `_wire` is imported
only by `_payload`. `_telemetry` sits above `_wrap` (it opens `scope()`) and below `_client`; `hajer.evals.*`
imports `_settings`, `_json`, `_transport`, `_paths`, `_errors`, `_redact` and `_telemetry`, and nothing in the
core imports `hajer.evals`. `_wrap` imports nothing of the SDK above `_settings` and never will — attach mode
reaches it through one installed hook (`_wrap.set_settled_hook`) rather than an import, which is what
keeps the seam that runs inside every provider call free of the client that sends the observation.

## The rules that are enforced

- **basedpyright strict, written `Any` banned** (`reportExplicitAny = "error"`). An opaque value —
  anything read off a provider object or a JSON body — is `object`, narrowed with `isinstance` or
  `callable` before use, or `cast` to a concrete type at one named helper. Every `pyright: ignore`
  names its rule and must be needed.
- **The environment is read in one place.** `os.environ` and `os.getenv` are banned by ruff
  everywhere but `_settings.py`.
- **No provider import, ever.** `openai` and `anthropic` are banned imports. `wrap()` finds surfaces
  by attribute on the object it is handed.
- **Every bound is a setting.** No numeric limit is spelled as a literal outside `_settings.py`.
  A new bound is a `HajerSettings` field, a `HAJER_*` variable, a row in the settings table in
  `docs/reference.md`, and a default the docs explain.
- **No network in tests.** The transport seam is `httpx.MockTransport` and the providers are fakes in
  `tests/fakes.py`. A test that would open a socket is a test that does not belong here — with one
  exception, on loopback only, and marked: a test that can only make its claim with a socket carries
  `@pytest.mark.loopback` (registered in `pyproject.toml`; `just test -m "not loopback"` leaves them out)
  and binds only `127.0.0.1`. The second, equally explicit exception is `@pytest.mark.engine`: the golden
  eval test runs the pinned engine on Node and, on a cold cache, installs it from the npm registry. It is
  selected only with `pytest --engine` (never by detecting Node, which every CI runner has), and `just ci`
  leaves it out; the `evals` job in `.github/workflows/python.yml` is where it runs.

## Conventions

- **Milliseconds on the boundary, seconds only where httpx demands them.** `Deadline` is the one
  place that converts.
- **The deadline covers the whole operation** — serialisation, connection, transfer, server work —
  and the remaining budget goes to the server in the body. A per-request httpx timeout is derived
  from what is left; there is no client-level default timeout, because it would silently apply to a
  request whose budget had already run out.
- **No automatic inline retry on `verify`.** `observe` retries with bounded backoff because it is off
  the response path.
- **Wire keys are the platform's camelCase; attributes are its snake_case.** The four assessment
  statuses and the per-check reasons are lower-case, spelled exactly as the platform's verification
  contract spells them, because a customer's own `if` compares against them. Every other enum is
  upper-case.
- **Response models are `extra="ignore"`.** A newer server adding a field must not raise inside a
  customer's request.
- **Docstrings say why, not what.** A module docstring states the rule the module exists to keep.

## The contract chain

`../contract/` holds files vendored from the Hajer platform: its OpenAPI snapshot and the shared test
vectors (case keys, redaction, and others). They are not edited in this repository;
they change when the platform re-vendors them. The SDK generates its wire types from the snapshot:

```
../contract/openapi.json                        vendored from the platform
  │  scripts/generate_wire.py
  ▼
hajer/_wire.py                                  written by: just contract-refresh
                                                guard: just contract-check (just check · CI)
```

Which operations it reads is not a list in the generator: it is `hajer._paths.SDK_OPERATIONS`.
`_wire.py` is generated and `WIRE_READY` is `True`, so the module carries the platform's own model for
every schema those operations reach, and `hajer.wire_source()` reports the snapshot digest it came from.
After the snapshot moves the whole of the work is:

```bash
just contract-refresh && just check && just test
```

Two things the generator does that are easy to get wrong, and both are pinned by
`tests/test_wire_generation.py`: a component that declares **properties** becomes a `BaseModel`, and a
component that declares an **enum** (`type CheckReason = Literal[...]`) or nothing at all (`JsonValue`)
becomes a `TypeAlias` — rendering either as a model with no fields would turn a closed vocabulary into
an object that accepts anything. `JsonValue` is the name the module already imports, so the generator
emits a comment rather than a circular alias.

`_payload.py` still builds the request body as a plain dict, on purpose: it runs inside somebody else's
request path, and validating every submission there would spend the caller's latency proving something
the tests already prove. What binds the two is `tests/test_payload.py::TestTheGeneratedVerifyIn`, which
puts a real body through the generated `VerifyIn` **and** against the snapshot's own JSON schema —
required properties present, no undeclared key, `additionalProperties: false` respected. `IngestMode`
is the generated alias rather than a second declaration of the same two words.

One requirement the server makes that the SDK does not enforce: **a verifier is pinned**
(`refund-policy@1`). `VerifyIn.verifier` carries a `name@version` pattern, there is no "latest", and an
unpinned name is a 422 before ingest sees it. The fixture app and the docs pin it; `verify()` passes
whatever string it is given, so a caller who forgets learns it from the response.

Never hand-edit `_wire.py`. Regenerate. It is excluded from ruff (a formatter that rewrote it would
put it permanently at odds with its generator).

`_rules.py` and `_checksums.py` are generated too, from the platform's redaction catalog, by tooling that
lives in the platform repository; both carry a digest of their sources. Never hand-edit them, and do not
add a generator for them here. They are excluded from ruff for the same reason as `_wire.py`.

## Testing (`tests/`)

- pytest + pytest-asyncio in auto mode. `tests/conftest.py` has the `Recorder` seam (an
  `httpx.MockTransport` that collects what was sent and answers from a script), the `settings`
  fixture, the `times_out` handler that spends the SDK's own derived budget before failing, and the
  two stream protocols.
- `tests/fakes.py` is the provider surface: fake OpenAI and Anthropic clients and response objects,
  sync and async, streamed and not. Excluded from vulture — every field exists to be read by
  `getattr` at runtime, which is exactly what vulture cannot see.
- `tests/fixture_app.py` is the fixture application: the README quickstart in a runnable shape, with a
  real sink. `tests/test_fixture_app.py` runs it end to end.
- A test asserts on the wire (`recorder.bodies()`, `recorder.keys()`), not on internals, wherever the
  wire is what the behaviour is about.
- Timing assertions are bounded and honest: the deadline test asserts the whole call returned inside
  `deadline_ms + 50`, which is the claim the reference makes.
- Tests marked `requires_platform` (`tests/repo.py`) compare against the platform's own source. They
  run only when this repository is checked out as the platform's `sdk/` submodule and skip otherwise;
  a standalone checkout's `just test` is green without them.
- Files under `../contract/` are read by tests as fixtures (the vector files); never edit them to make
  a test pass.
