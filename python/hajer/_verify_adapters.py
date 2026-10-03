"""`python -m hajer verify-adapters`: does each declared adapter's call reach the model call site it says it drives?

Hajer proposes an adapter from static analysis (the lowest function every caller path to the site passes through,
its factory, its model client) and writes it into `.hajer/replay.toml` with the `site` it drives. Static analysis is a
claim; this is the check, run where the application's dependencies are installed: the customer's CI. Each adapter
with a `site` is called once, in a fresh child process under the full replay guards (`hajer.replay._reach`): no
network, no database, no process, and a fake model answering every model call. The input is one a model authored for
it when a committed suite's execution binding has one (`.hajer/executions/*.json`), else a stand-in built from the
callable's signature. It costs nothing and sends nothing. Under the explicit temporary local proof
(`--temporary-local-executions DIR`, `hajer._temporary`) there is no committed binding: each case bound to the adapter
is checked with that case's own authored input, as the pytest plugin checks it in session, and nothing uploads.

Each adapter reads one status, and only VERIFIED is a pass:
- VERIFIED: a model call was made from the declared site (`path:line`, anywhere in the call's span: the innermost
  application frame on the call's stack) and nothing the application tried was refused;
- RUNTIME_SIDE_EFFECT_REFUSED: the call tried a database, a network host or a process, which a replay refuses; the
  detail names it (and whether the site was reached). A host the customer wants the replay to reach is declared as
  the `[provider]` in `.hajer/replay.toml`;
- RUNTIME_UNREACHED: the call never reached the site: the adapter could not be built (named), the application
  raised (the exception's class, never its message), it reached other sites (named), or no model call was made;
- UNVERIFIED: the adapter declares no `site`, or the child could not run (its timeout).
The results are written to a local file and, on request, uploaded with the CI run (`suite-runs`), so an adapter the
check never passed reads UNVERIFIED, and the pytest plugin keeps its cases neutral, never green.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Final, Literal, TypeAlias, cast
from uuid import uuid4

from pydantic import JsonValue

from hajer._settings import CI_TEXT_MAX, HajerSettings, adapter_check_environment, clipped
from hajer._temporary import temporary_case_inputs, write_private

AdapterStatus: TypeAlias = Literal["VERIFIED", "RUNTIME_SIDE_EFFECT_REFUSED", "RUNTIME_UNREACHED", "UNVERIFIED"]
REPLAY_CONFIG: Final = ".hajer/replay.toml"
_BOOT: Final = Path(__file__).resolve().parent / "replay" / "_reach_boot.py"
_KEYS: Final = {"factory_arguments": "factoryArguments", "callable": "qualname"}


def upload_identity(value: str) -> str:
    """An overlong, refused identity remains distinguishable in the bounded diagnostic upload."""
    if len(value) <= CI_TEXT_MAX:
        return value
    suffix = ":sha256:" + sha256(value.encode()).hexdigest()
    return value[: CI_TEXT_MAX - len(suffix)] + suffix


@dataclass(frozen=True, slots=True)
class AdapterCheck:
    adapter_id: str
    verifier: str | None
    site: str | None
    status: AdapterStatus
    detail: str

    def document(self) -> dict[str, JsonValue]:
        """As a CI upload carries it: every string within the backend's cap, so one long detail never rejects a run."""
        return {
            "adapterId": upload_identity(self.adapter_id),
            "verifier": None if self.verifier is None else upload_identity(self.verifier),
            "site": None if self.site is None else upload_identity(self.site),
            "status": self.status,
            "detail": clipped(self.detail),
        }


@dataclass(frozen=True, slots=True)
class Authored:
    """One case's own input, checked in place of a committed binding's (the explicit temporary local proof)."""

    value: JsonValue


def adapter_document(adapter_id: str, table: Mapping[str, object]) -> dict[str, JsonValue]:
    """A `[adapters."<id>"]` table as the replay child reads an adapter (the backend's `EntryAdapter.document()`)."""
    document: dict[str, JsonValue] = {"adapterId": adapter_id, "fields": {}, "constructor": "NONE", "input": "SINGLE"}
    for key, value in table.items():
        document[_KEYS.get(key, key)] = cast(JsonValue, value)
    return document


def _site_lines(root: Path, site: str) -> frozenset[int]:
    """Every line of the model call written at `site` (a call spans lines), or just its own when unreadable."""
    path, _, line = site.rpartition(":")
    start = int(line)
    try:
        tree = ast.parse((root / path).read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return frozenset({start})
    ends = [node.end_lineno or start for node in ast.walk(tree) if isinstance(node, ast.Call) and node.lineno == start]
    return frozenset(range(start, max(ends, default=start) + 1))


def _reached(receipt: Mapping[str, JsonValue], site: str, lines: frozenset[int]) -> tuple[bool, list[str]]:
    """Whether a model call was made from the site, and where every other one was made from. A call is made from the
    innermost application frame on its stack: a caller further out (a function that calls the one making the call)
    is on the path to a site, not the site."""
    path = site.rpartition(":")[0]
    hit, elsewhere = False, list[str]()
    for call in cast(list[dict[str, JsonValue]], receipt.get("calls") or []):
        frames = [(str(item[0]), int(cast(int, item[1]))) for item in cast(list[list[JsonValue]], call["frames"])]
        if frames and frames[0][0] == path and frames[0][1] in lines:
            hit = True
        elif frames:
            elsewhere.append(f"{frames[0][0]}:{frames[0][1]}")
    return hit, elsewhere


def judge(receipt: Mapping[str, JsonValue], site: str, lines: frozenset[int]) -> tuple[AdapterStatus, str]:
    """One adapter's status from what its child reported."""
    hit, elsewhere = _reached(receipt, site, lines)
    refused = sorted({str(item) for item in cast(list[JsonValue], receipt.get("refusedHosts") or [])})
    entry = receipt.get("entry")
    if isinstance(entry, dict):
        return "RUNTIME_UNREACHED", f"the adapter could not be built: {entry.get('reason')}: {entry.get('detail')}"
    if refused:
        reached = "the site was reached, but " if hit else ""
        return "RUNTIME_SIDE_EFFECT_REFUSED", f"{reached}the call tried what a replay refuses: {', '.join(refused)}"
    if hit:
        return "VERIFIED", f"a model call was made from {site}"
    raised = receipt.get("exceptionClass")
    if isinstance(raised, str):
        return "RUNTIME_UNREACHED", (
            f"the call raised {raised} before any model call from {site} (state the application installs at startup "
            "is declared under [setup] in .hajer/replay.toml)"
        )
    if elsewhere:
        return "RUNTIME_UNREACHED", f"the call reached {', '.join(sorted(set(elsewhere)))}, not {site}"
    return "RUNTIME_UNREACHED", "the call made no model call at all"


def _authored_input(root: Path, adapter_id: str) -> JsonValue | None:
    """A model-authored case input for this adapter from a committed execution binding, when there is one."""
    for binding in sorted((root / ".hajer" / "executions").glob("*.json")):
        try:
            document = cast(JsonValue, json.loads(binding.read_text(encoding="utf-8")))
            cases = document.get("cases") if isinstance(document, dict) else None
        except (OSError, ValueError):
            continue
        for execution in (cases if isinstance(cases, dict) else {}).values():
            if not isinstance(execution, dict):
                continue
            adapter = execution.get("adapter")
            if isinstance(adapter, dict) and adapter.get("adapterId") == adapter_id:
                return execution.get("input")
    return None


def _fake_provider(root: Path) -> dict[str, JsonValue] | None:
    """The declared gateway may receive canned model answers, never a real preflight connection."""
    path = root / REPLAY_CONFIG
    if not path.is_file():
        return None
    provider = cast(dict[str, object], tomllib.loads(path.read_text())).get("provider")
    if not isinstance(provider, dict):
        return None
    table = cast(dict[str, object], provider)
    host, port = table.get("host"), table.get("port")
    scheme = table.get("scheme", "https" if port == 443 else "http")
    if not isinstance(host, str) or not isinstance(port, int) or isinstance(port, bool):
        return None
    if not host or not 1 <= port <= 65535 or not isinstance(scheme, str) or scheme not in ("http", "https"):
        return None
    return {"host": host, "port": port, "scheme": scheme}


def check_adapter(
    root: Path,
    adapter: dict[str, JsonValue],
    *,
    environment: Mapping[str, str],
    settings: HajerSettings,
    authored: Authored | None = None,
) -> AdapterCheck:
    """Call one adapter once under the guards with the fake model, and read what it reached. `authored` is the input
    of the case the check is for; without it, a committed binding's input or the signature's stand-in."""
    named = adapter.get("adapterId")
    adapter_id = named if isinstance(named, str) else f"{adapter.get('module')}:{adapter.get('qualname')}"
    site = adapter.get("site")
    verifier = adapter.get("verifier")
    label = verifier if isinstance(verifier, str) else None
    if not isinstance(site, str):
        return AdapterCheck(adapter_id, label, None, "UNVERIFIED", "the adapter declares no `site` to check")
    if any(len(value) > CI_TEXT_MAX for value in (adapter_id, site, label or "")):
        return AdapterCheck(
            adapter_id, label, site, "UNVERIFIED", "ADAPTER_IDENTITY_TOO_LONG: identity exceeds the upload contract"
        )
    path, _, line = site.rpartition(":")
    relative = Path(path)
    if (
        not path
        or relative.is_absolute()
        or ".." in relative.parts
        or not (root / relative).resolve().is_relative_to(root.resolve())
        or not line.isdecimal()
        or int(line) < 1
    ):
        return AdapterCheck(
            adapter_id,
            label,
            site,
            "UNVERIFIED",
            "ADAPTER_SITE_INVALID: expected repository-relative path:positive-line",
        )
    project = root / str(adapter["root"]) if isinstance(adapter.get("root"), str) else root
    spec = {
        "adapter": adapter,
        "input": _authored_input(root, adapter_id) if authored is None else authored.value,
        "repositoryRoot": str(root),
        "projectRoot": str(project),
        "answers": settings.adapter_check_answers,
        "provider": _fake_provider(root),
    }
    with tempfile.TemporaryDirectory(prefix="hajer-reach-") as directory:
        work = Path(directory)
        write_private(work / "spec.json", json.dumps(spec))
        argv = [
            sys.executable,
            "-I",
            "-S",
            "-B",
            str(_BOOT),
            *(str(work / name) for name in ("spec.json", "receipt.json", "egress.jsonl")),
        ]
        try:
            subprocess.run(  # noqa: S603 - fixed interpreter and boot; the application is the declared adapter
                argv,
                cwd=project,
                env=adapter_check_environment(environment),
                capture_output=True,
                check=False,
                timeout=settings.ci_case_timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return AdapterCheck(adapter_id, label, site, "UNVERIFIED", "the check timed out")
        except OSError:
            return AdapterCheck(adapter_id, label, site, "UNVERIFIED", "the check's process could not start")
        receipt_path = work / "receipt.json"
        if not receipt_path.is_file():
            return AdapterCheck(adapter_id, label, site, "UNVERIFIED", "the check's process ended without a receipt")
        try:
            receipt = cast(JsonValue, json.loads(receipt_path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return AdapterCheck(adapter_id, label, site, "UNVERIFIED", "the check wrote an unreadable receipt")
    if not isinstance(receipt, dict):
        return AdapterCheck(adapter_id, label, site, "UNVERIFIED", "the check wrote a non-object receipt")
    try:
        status, detail = judge(receipt, site, _site_lines(root, site))
    except (KeyError, TypeError, ValueError, IndexError):
        return AdapterCheck(adapter_id, label, site, "UNVERIFIED", "the check wrote a malformed reachability receipt")
    return AdapterCheck(adapter_id, label, site, status, detail)


def declared_adapters(root: Path) -> tuple[list[dict[str, JsonValue]], dict[str, str]]:
    """Every `[adapters."<id>"]` table of `.hajer/replay.toml` as an adapter document, and its `[environment]`."""
    document = tomllib.loads((root / REPLAY_CONFIG).read_text(encoding="utf-8"))
    tables = cast(dict[str, dict[str, object]], document.get("adapters") or {})
    environment = cast(dict[str, object], document.get("environment") or {})
    declared = {name: value for name, value in environment.items() if isinstance(value, str)}
    setup: object = cast(dict[str, object], document.get("setup") or {}).get("calls")
    adapters = [adapter_document(key, table) for key, table in sorted(tables.items())]
    for item in adapters:
        local = item.get("setup", [])
        if not isinstance(local, list) or (setup is not None and not isinstance(setup, list)):
            raise ValueError("adapter setup and global setup calls must be lists")
        if setup or local:
            item["setup"] = [*cast(list[JsonValue], setup or []), *local]
        else:
            item.pop("setup", None)
    return adapters, declared


def _temporary_inputs(root: Path, directory: Path) -> dict[str, list[JsonValue]]:
    """Each adapter id's case inputs, in case id order, from the bundle's binding of each suite it holds one for."""
    found: dict[str, list[JsonValue]] = {}
    for suite in sorted((root / ".hajer" / "suites").glob("*.json")):
        if not (directory / suite.name).exists():
            continue
        cases = temporary_case_inputs(directory, suite)
        for case_id in sorted(cases):
            adapter_id, value = cases[case_id]
            found.setdefault(adapter_id, []).append(value)
    return found


def verify_adapters(
    root: Path, settings: HajerSettings, *, only: Sequence[str] = (), temporary: Path | None = None
) -> list[AdapterCheck]:
    """Check every declared adapter (or the ones `only` names) in the repository at `root`. With `temporary`, an
    adapter a bundled case is bound to is checked once per such case, with that case's own input."""
    adapters, environment = declared_adapters(root)
    chosen = [item for item in adapters if not only or item["adapterId"] in only]
    inputs = {} if temporary is None else _temporary_inputs(root, temporary)
    checks: list[AdapterCheck] = []
    for item in chosen:
        values = inputs.get(str(item["adapterId"]), [])
        if not values:
            checks.append(check_adapter(root, item, environment=environment, settings=settings))
        for value in values:
            authored = Authored(value)
            checks.append(check_adapter(root, item, environment=environment, settings=settings, authored=authored))
    return checks


def results_document(checks: Sequence[AdapterCheck], settings: HajerSettings) -> dict[str, JsonValue]:
    """The results as a CI run carries them (`suite-runs`): no suite, every adapter check; `upload-results` sends the
    file unchanged."""
    return {
        "receiptId": str(uuid4()),
        "commitSha": settings.ci_commit_sha,
        "branch": settings.ci_branch,
        "mode": "replay",
        "suites": [],
        "adapters": [item.document() for item in checks],
    }
