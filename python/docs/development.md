# Developing the Python SDK

Everything runs from `python/`, with [uv](https://docs.astral.sh/uv/) and
[just](https://github.com/casey/just). [`CLAUDE.md`](../CLAUDE.md) has the conventions and the rules the
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
- `docs/support-matrix.md` is generated from the `Target` declarations in `hajer/_attach.py` by
  `scripts/generate_support_matrix.py`.

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
