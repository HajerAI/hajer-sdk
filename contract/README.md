# contract/

The Hajer platform's half of every agreement this SDK keeps. **Do not edit these files here**: they are
copied from the platform repository, and a pull request that changes one by hand will be overwritten by
the next sync.

| File | What it is | Read by |
|---|---|---|
| `openapi.json` | The platform's OpenAPI snapshot, trimmed to the operations the SDK calls and the schemas they reach | `python/scripts/generate_wire.py` → `python/hajer/_wire.py` |
| `case-key-vectors.json` | Shared vectors for how a `case_key` is derived | Python case-key tests |
| `check-evaluation-vectors.json`, `situation-witness-vectors.json`, `qualification-vectors.json` | Shared vectors for the platform's check evaluation, witnesses and the qualification rule | the platform's own tests (no longer read here) |
| `secret-field-vectors.json`, `overlap-vectors.json` | The platform's redaction vectors | Python redaction tests |
| `redaction-vectors.json` | Client-side redaction positives and negatives | Python redaction tests |
| `observed-app/` | A small invented application, and the export the SDK records from it | `python/tests/test_observed_capture.py` |
| `sdk-capture/export.json` | The exact request bodies the SDK sends for a reference workflow | `python/tests/test_suite_reference_capture.py` |

Two of these flow the other way: `observed-app/export.json` and `sdk-capture/export.json` are *produced*
by this repository's suite (`--update-observed-export`, `--update-suite-reference-capture`) and the
platform's own tests read them back.

Two generated modules come from the platform's source rather than from a file here —
`python/hajer/_rules.py` and `python/hajer/_checksums.py` (the client-side redaction catalog and its
checksums). Their generators run in the platform repository, which also checks that every file in this
directory is current.
