# Contributing to the Python SDK

Customer documentation lives at [docs.hajer.ai](https://docs.hajer.ai) (source: `HajerAI/hajer-docs`,
the monorepo's `docs-site/`). A change to the public API, a setting, a CLI flag or `hajer.yaml` updates the
matching page there in the same piece of work.

Everything runs from `python/`, with [uv](https://docs.astral.sh/uv/) and
[just](https://github.com/casey/just). [`CLAUDE.md`](CLAUDE.md) has the conventions and the rules the
gates enforce.

```bash
just sync                 # uv sync --locked --all-extras — run it first, in a clean checkout
just check                # ruff format · ruff · basedpyright strict · vulture · deptry · lock
just test                 # pytest; `just test -k wrap` for a subset
just build                # sdist and wheel into dist/
```

`just sync` has to succeed in a clean checkout, because `UV_NO_SYNC=1` is set for every other recipe:
nothing syncs behind your back, so a failed sync leaves `just check` unable to find ruff rather than
quietly installing something. It installs every extra, because the provider-surface tests import the
real `openai`, `anthropic` and OpenTelemetry SDKs and skip without them.

## Generated files

- `hajer/_rules.py` and `hajer/_checksums.py` are generated from the platform's redaction catalog by
  tooling that lived in the platform repository. Never hand-edit them.
- The support matrix on docs.hajer.ai (`docs/telemetry/_support-matrix.mdx` in `HajerAI/hajer-docs`, the
  monorepo's `docs-site/`) is generated from the `Target` declarations in `hajer/_attach.py` by
  `scripts/generate_support_matrix.py`. A change to a target regenerates it there:
  `uv run python scripts/generate_support_matrix.py --write --out ../../docs-site/docs/telemetry/_support-matrix.mdx`.

Files under `../contract/` (the redaction vectors) are vendored from the platform and are not edited here.

## Tests

The core suite uses the provider fakes in `tests/fakes.py` and, for the SDK's own HTTP requests,
`httpx.MockTransport`; no request reaches a network. The only tests that open a socket are marked `loopback`, in
`tests/test_http_libraries.py`, and bind only `127.0.0.1` (and one Unix socket file in a private `/tmp`
directory) to listeners the tests run. `just test -m "not loopback"` leaves them out.

With all extras installed, the provider tests also run the real OpenAI and Anthropic SDKs against
in-memory transports: OpenAI through `httpx.MockTransport`, the locked Anthropic SDK through
`httpx2.MockTransport` (declared only in the development dependency group). They skip if that provider
is not installed.

## Moving the eval engine pin

```bash
just evals-pin 0.124.0            # rewrites package.json, regenerates package-lock.json (needs node + npm)
just test --engine -m engine      # the golden test against the new engine (installs it into the cache)
just check
```

Then note the bump under `[Unreleased]` in `CHANGELOG.md` and release: the pin changes only with a `hajer`
release, so one package version names one engine version. `--package-lock-only` records every platform's
optional packages, which is what lets a lock made on macOS install under `npm ci` on Linux CI.

Also update the pinned version on docs.hajer.ai (`docs/evals/engine.md` in `HajerAI/hajer-docs`).
