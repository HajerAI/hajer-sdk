# Hajer SDKs

At the point where a model output crosses into a side effect — an HTTP response, an email, a database
write — your code hands [Hajer](https://hajer.ai) the request, the output and the evidence it chose.
Hajer applies a named, versioned **verifier** and returns an **assessment**. Your application decides
what to do with it.

This repository holds the open-source clients for that call.

| Directory | Package | Status |
|---|---|---|
| [`python/`](python/) | [`hajer`](https://pypi.org/project/hajer/) on PyPI | 0.x |
| [`typescript/`](typescript/) | `@hajer/sdk` | not yet published |
| [`action/`](action/) | GitHub Action: run Hajer suites in your CI | `uses: HajerAI/hajer-sdk/action@<sha>` |
| [`contract/`](contract/) | The platform API snapshot and shared test vectors the SDKs are built against | copied from the platform; do not edit |

## Python in thirty seconds

```bash
pip install hajer
export HAJER_API_KEY=...  HAJER_TEAM_ID=...
```

```python
import hajer
from openai import OpenAI

hajer_client = hajer.Hajer()
openai_client = hajer.wrap(OpenAI())   # model calls are recorded beside the answer

reply = openai_client.chat.completions.create(model="gpt-5", messages=[{"role": "user", "content": question}])
answer = reply.choices[0].message.content

assessment = hajer_client.verify("refund-policy@1", {"question": question}, answer, {"orderState": order.state})
if assessment.status == "violated":
    ...  # your policy, your decision
```

Without `HAJER_API_KEY` and `HAJER_TEAM_ID` the client is inert: `verify` returns `unavailable`, nothing
opens a socket and nothing raises, so adding the SDK never breaks a test suite that has no credentials.
The [Python README](python/README.md) has the rest.

## Development

Each package is self-contained, with its own tooling and lockfile. Run commands from inside it:

```bash
cd python && just sync && just check && just test
cd typescript && npm ci && just ci
```

`python/hajer/_wire.py` and `typescript/src/wire.ts` are generated from `contract/openapi.json`
(`just contract-refresh` / `just generate-wire`) and never edited by hand.

Releases are tagged per package: `python-v0.2.0` publishes `hajer` 0.2.0 to PyPI
(`.github/workflows/release-python.yml`).

## License

[MIT](LICENSE)
