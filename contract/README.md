# contract/

The Hajer platform's half of the one agreement this SDK keeps: the client-side redaction pass removes the
same shapes the platform removes. **Do not edit these files here**: they are copied from the platform
repository, and a pull request that changes one by hand will be overwritten by the next sync.

| File | What it is | Read by |
|---|---|---|
| `redaction-vectors.json` | Client-side redaction positives and negatives | `python/tests/test_redact_vectors.py` |
| `secret-field-vectors.json`, `overlap-vectors.json` | The platform's redaction vectors | `python/tests/test_redaction_spec.py` |

Two generated modules come from the platform's source rather than from a file here —
`python/hajer/_rules.py` and `python/hajer/_checksums.py` (the client-side redaction catalog and its
checksums). Their generators ran in the platform repository; the modules are vendored as they stand.
