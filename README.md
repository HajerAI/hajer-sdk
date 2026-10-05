# Hajer SDK

The open-source client for [Hajer](https://hajer.ai): your application's model calls as traces, its
conversations as sessions, and `hajer eval` — the repository's promptfoo suites, run on a pinned engine and
reported against the workflows and obligations they cover.

**Documentation: [docs.hajer.ai](https://docs.hajer.ai)**

| Directory | Package | Status |
|---|---|---|
| [`python/`](python/) | [`hajer`](https://pypi.org/project/hajer/) on PyPI | 0.x |
| [`contract/`](contract/) | Shared test vectors the SDK is built against | copied from the platform; do not edit |

## Python in thirty seconds

```bash
pip install "hajer[otel]"
export HAJER_API_KEY=...  HAJER_TEAM_ID=...
```

```python
import hajer
from openai import OpenAI

client = hajer.wrap(OpenAI())   # every model call this client makes is recorded

@hajer.workflow("answer-support-question")
def answer(question: str) -> str:
    reply = client.chat.completions.create(model="gpt-5", messages=[{"role": "user", "content": question}])
    return reply.choices[0].message.content
```

Without `HAJER_API_KEY` and `HAJER_TEAM_ID` the SDK is inert: nothing opens a socket and nothing raises,
so adding it never breaks a test suite that has no credentials. Start with the
[telemetry quickstart](https://docs.hajer.ai/telemetry/quickstart) or the
[evals quickstart](https://docs.hajer.ai/evals/quickstart).

## Development

The package is self-contained, with its own tooling and lockfile; [`python/CONTRIBUTING.md`](python/CONTRIBUTING.md)
has the workflow and [`python/CLAUDE.md`](python/CLAUDE.md) the rules.

```bash
cd python && just sync && just check && just test
```

Customer documentation lives in the docs site ([docs.hajer.ai](https://docs.hajer.ai), source in
`HajerAI/hajer-docs`), not in this repository: a change to the public surface updates the matching page there.

Releases are tagged per package: `python-v0.2.0` publishes `hajer` 0.2.0 to PyPI
(`.github/workflows/release-python.yml`).

## License

[MIT](LICENSE)
