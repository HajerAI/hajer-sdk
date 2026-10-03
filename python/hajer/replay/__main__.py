"""One replay attempt, entered from `_boot.py` (`python -I -S -B …/hajer/replay/_boot.py SPEC RECEIPT EGRESS_LOG`).

Reads the case spec (a JSON file the runner wrote) and the Hajer API key from stdin (never an argument or a
file the application could name), seeds `platform` with the parent's recorded answers (`_platform.py`), installs
the sandbox — and only then lets the revision in: the
interpreter's `.pth` files are processed, the revision's working copy goes on `sys.path` and a
`sitecustomize` it ships is imported, all under the guard. Then it runs the case and writes the receipt as a
new owner-only file. The receipt never contains a credential.
"""

from __future__ import annotations

import json
import os
import site
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import httpx
from pydantic import JsonValue

from hajer.replay._boot import site_directories
from hajer.replay._case import run_case
from hajer.replay._database import install_database_guard
from hajer.replay._guard import Endpoint, Sandbox, recordings_from
from hajer.replay._hooks import install
from hajer.replay._platform import prime

RECEIPT_SCHEMA = "hajer-replay-receipt-v1"


def _tunnel(spec: dict[str, JsonValue], name: str) -> str | None:
    tunnels = spec.get("tunnels")
    path = tunnels.get(name) if isinstance(tunnels, dict) else None
    return path if isinstance(path, str) else None


def _endpoint(spec: dict[str, JsonValue]) -> Endpoint | None:
    value = spec.get("provider")
    if not isinstance(value, dict):
        return None
    host, port = str(value["host"]).lower(), int(cast(int, value["port"]))
    return Endpoint(host=host, port=port, tunnel=_tunnel(spec, "provider"))


def _hajer_endpoint(spec: dict[str, JsonValue]) -> Endpoint | None:
    value = spec.get("hajer")
    if not isinstance(value, dict):
        return None
    url = httpx.URL(str(value["baseUrl"]))
    port = url.port or (443 if url.scheme == "https" else 80)
    return Endpoint(host=url.host.lower(), port=port, tunnel=_tunnel(spec, "hajer"), scheme=url.scheme)


def _admit_revision(source_root: str) -> None:
    """After the guard: the interpreter's `.pth` hooks, the revision's own path, then its `sitecustomize`."""
    for directory in site_directories():
        site.addsitedir(directory)
    sys.path.insert(1, source_root)
    site.execsitecustomize()


def _write(path: Path, receipt: Mapping[str, JsonValue]) -> None:
    data = json.dumps({"schema": RECEIPT_SCHEMA, **receipt}, sort_keys=True, allow_nan=False, default=str)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(data)


def main(argv: list[str]) -> int:
    spec_path, receipt_path, egress_path = (Path(item) for item in argv)
    secrets: object = json.loads(sys.stdin.read() or "{}")
    api_key = cast(dict[str, object], secrets).get("hajerApiKey") if isinstance(secrets, dict) else None
    spec = cast(dict[str, JsonValue], json.loads(spec_path.read_text(encoding="utf-8")))
    # Before the guard: `platform` answers from the parent's recorded values and never needs a process.
    answered = prime(spec.get("platform"))
    sandbox = Sandbox(
        provider=_endpoint(spec),
        hajer=_hajer_endpoint(spec),
        recordings=recordings_from(spec.get("boundaries")),
        egress_log=egress_path,
    )
    install(sandbox)
    install_database_guard(sandbox)
    _admit_revision(str(spec["sourceRoot"]))
    receipt = run_case(spec, sandbox, api_key if isinstance(api_key, str) else None)
    _write(receipt_path, {**receipt, "platform": answered})
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
