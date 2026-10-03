# Running against a local or self-hosted Hajer platform

The SDK talks to the hosted service at `https://api.hajer.ai` unless told otherwise. To point it at a
platform you run yourself — a local development stack or your own deployment — set the third variable:

```bash
export HAJER_API_KEY=...                      # issued by that platform
export HAJER_TEAM_ID=...                      # a team on that platform
export HAJER_BASE_URL=http://localhost:8000   # where that platform's API listens
```

Nothing else changes. `python -m hajer doctor` shows the base URL in force, whether it came from the
environment, and whether something answers a keyless GET of its liveness probe there.

## Your first row

The quickstart in the [README](../README.md#quickstart) is the shape you fill in with your own
workflow: it names your `ticket`, your `order` and your own verifier, so there is nothing to run until
you have written that much. This section is the smaller thing, and it runs as written — one `verify`
against a fixture verifier, and the row it leaves behind.

It needs two things from a platform operator: a key for your team, and the fixture verifier
`employee-compile-title-cap@1` installed into that team. Its one obligation is *forward at most five job
titles, and only titles that were asked for*; its evidence contract requires one field,
`requested_roles`.

Then, with `HAJER_API_KEY`, `HAJER_TEAM_ID` and `HAJER_BASE_URL` exported:

<!-- The `hajer:quickstart-run` tag on the fence is extracted and executed by an external check against
a platform of its own. Keep the tag, and keep the block runnable bash that reads HAJER_API_KEY,
HAJER_TEAM_ID and HAJER_BASE_URL from the environment: nothing in it may hard-code a base URL. -->

```bash hajer:quickstart-run
python - <<'PY'
import hajer

client = hajer.Hajer()
assessment = client.verify(
    "employee-compile-title-cap@1",
    {"role": "staff-engineer", "brief": "which titles should this compile forward?"},
    {"job_titles": ["Staff Engineer", "Engineering Manager"]},
    {"requested_roles": ["Staff Engineer", "Engineering Manager", "Director of Platform"]},
)
print("verify:", assessment.status, assessment.reason or "-")
client.close()
PY

python -m hajer tail --limit 1
```

```
verify: unavailable SHADOW
2026-09-19T07:21:55.174187Z       ing-36f84d853579edf7b88904a7991d84cc      VERIFY   employee-compile-title-cap@1  -                                   redactions=0    OUTPUT_OBSERVED       assessed=yes
```

`unavailable{SHADOW}` is the expected answer. The fixture verifier is in state `SHADOW`, which means it
has not qualified: its one check ran, the assessment exists in full (`assessed=yes`), and what reaches
your code is `unavailable` — because a verifier nobody has qualified is not something you should be
gating on. What you were checking is the second line: the row landed, it is your team's, and `tail`
reads it back.

`tail` prints oldest first, so `--limit 1` is the first observation this team ever recorded. On a team
that already has traffic, use `python -m hajer tail --follow` in a second terminal instead and watch the
row arrive.
