# CLAUDE.md — python/

Guidance for contributors and coding agents working in `python/`. `README.md` and `docs/` are the
user-facing documentation; this file has the rules. Most of them are **enforced** — ruff and
basedpyright (`pyproject.toml`), vulture, deptry and the lockfile check, all in `just check` and in
`.github/workflows/python.yml`. When a gate fails, fix the code to match the
convention; do not loosen the gate.

## Commands (run from `python/`)

```bash
just sync                 # uv sync --locked --all-extras
just check                # fmt + lint + typecheck + deadcode + deps + lock-check — before every commit
just test                 # pytest; `just test -k wrap` for a subset
just test --engine -m engine   # the golden eval test: Node >= 22.22 on PATH, installs the pinned engine from npm once
just evals-pin 0.124.0    # move the engine pin (hajer/evals/engine/package.json + lockfile), then run the golden test
```

Python ≥ 3.11 · uv · httpx · pydantic v2. Runtime dependencies are **httpx and pydantic, and nothing
else**; `openai` and `anthropic` are optional extras the SDK never imports (ruff bans importing
them), and the OpenTelemetry SDK is the `otel` extra, imported only through `importlib` on first use. A
third runtime dependency is a decision, not an edit. `httpx2`, the httpx fork openai 3.x and anthropic
1.x send through, is not one: `_http_libraries.py` imports it when the interpreter has it, and every
seam that patches or recognises httpx (HTTP capture) does the same to it.

## Why this package is small on purpose

It runs inside a customer's request path. Every line here is code somebody else's product executes on
its way to sending an email, so the defaults are the conservative ones and the surface is the
smallest one that does the job. Two consequences that shape everything below:

- **It does not raise into application code.** A span that cannot be started, exported or flushed costs
  the span, never the call it was about. The places it may raise are in `_errors.py`, each with the
  reason silence would be worse. A new `raise` needs the same justification written next to it.
- **It changes nothing about the call.** The SDK never continues, holds, retries or sends on the
  application's behalf; the client, the methods, the return values and the exceptions are the
  provider's own, re-raised unchanged.

## Layout: one concern per module, all private but `__init__`

The core modules (not an exhaustive list — capture has its own):

```
hajer/
├── __init__.py      the public API and nothing else — __all__ is the contract
├── autoattach.py    public, no underscore: `import hajer.autoattach` is the one-line attach hook
├── __main__.py      `python -m hajer` — doctor, attach-path, eval
├── _bootstrap/      sitecustomize.py — the same hook, where the interpreter finds it on PYTHONPATH
├── _settings.py     HajerSettings; the ONLY reader of the process environment
├── _errors.py       every exception, and why each one is allowed to exist
├── _json.py         JsonValue / JsonObject — the one name for "survives a JSON round trip"
├── _paths.py        the routes the SDK calls, and the package version
├── _transport.py    Deadline, encode_body, SyncTransport, probe — the SDK's own HTTP requests (never a span)
├── _wrap.py         wrap(), the WrappedCall record, the task context, scope() and the call observer seam
├── _attach.py       attach()/detach(): the class patches and the import hook, provider-library-free
├── _instrumentation.py   instrument()/uninstrument(): the reversible class patches
├── _http_capture.py the transport tee: CaptureTransport, and HAJER_CAPTURE_HTTP
├── _redact.py       the client-side redaction walk, its budgets and the policy
├── _telemetry.py    workflow()/component()/tool(): the span emitter, OTel-free; eval_binding(), flush()
├── _telemetry_otel.py   the OpenTelemetry half of it, loaded only when `hajer[otel]` is installed
├── evals/           `hajer eval` (`hajer[evals]`): the pinned promptfoo engine (engine/package.json + lockfile),
│                    the metadata conventions and `beforeAll` hook, the payload, the upload, the CLI
├── _rules.py        GENERATED from the platform's redaction catalog — never hand-edited
└── _checksums.py    GENERATED alongside _rules.py — never hand-edited
```

Everything but `__init__.py`, `autoattach.py` and `__main__.py` is private (leading underscore). A name
a customer may use is exported from `__init__.py` and listed in `__all__`; a name that is not there is
not API and may change. `autoattach` has no underscore because it is meant to be written in somebody's
code, and `_bootstrap/sitecustomize.py` is meant to be *found* rather than imported by name.

Import direction is one way and there are no cycles: `_json` / `_paths` ← `_errors` ← `_settings` ←
`_transport` ← `_wrap` ← `_attach` / `_instrumentation` / `_http_capture` ← `__init__`. `_telemetry` sits
above `_wrap` (it opens `scope()`); `hajer.evals.*` imports `_settings`, `_json`, `_transport`, `_paths`,
`_errors`, `_redact` and `_telemetry`, and nothing in the core imports `hajer.evals`. `_wrap` imports nothing
of the SDK above `_settings` and never will — the span emitter reaches it through one installed observer
(`_wrap.set_call_observer`) rather than an import, which is what keeps the seam that runs inside every
provider call free of everything that exports.

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
- **The deadline covers the whole operation** — serialisation, connection, transfer, server work.
  A per-request httpx timeout is derived from what is left; there is no client-level default timeout,
  because it would silently apply to a request whose budget had already run out.
- **Nothing retries on a request path.** The eval upload retries with bounded backoff because it runs
  after the run it reports on; the span exporter batches on its own thread and never blocks a call.
- **Wire keys are the platform's camelCase; attributes are its snake_case.** The eval payload's enums
  are lower-case, spelled exactly as the platform reads them; span attribute keys are the OpenTelemetry
  semantic conventions' own, spelled once in the emitter and nowhere else.
- **Response models are `extra="ignore"`.** A newer server adding a field must not raise inside a
  customer's request.
- **Docstrings say why, not what.** A module docstring states the rule the module exists to keep.

## What is vendored from the platform

`../contract/` holds the redaction vectors both sides run; they are not edited in this repository.
`_rules.py` and `_checksums.py` are generated from the platform's redaction catalog by tooling that lived
in the platform repository; both carry a digest of their sources. Never hand-edit them, and do not add a
generator for them here. They are excluded from ruff (a formatter that rewrote one would put it
permanently at odds with its generator).

## Testing (`tests/`)

- pytest + pytest-asyncio in auto mode. `tests/conftest.py` has the `Recorder` seam (an
  `httpx.MockTransport` that collects what the eval upload sent and answers from a script), the
  `settings` fixture and the stream protocols.
- `tests/fakes.py` is the provider surface: fake OpenAI and Anthropic clients and response objects,
  sync and async, streamed and not. Excluded from vulture — every field exists to be read by
  `getattr` at runtime, which is exactly what vulture cannot see.
- `tests/fixture_app.py` is the fixture application: the README quickstart in a runnable shape, with a
  real sink. `tests/test_fixture_app.py` runs it end to end.
- A test asserts on what left the process — the exported spans, the upload's recorded request — not on
  internals, wherever that is what the behaviour is about.
- Files under `../contract/` are read by tests as fixtures (the vector files); never edit them to make
  a test pass.
