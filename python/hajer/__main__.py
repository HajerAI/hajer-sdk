"""`hajer` (also `python -m hajer`) — the three things an operator does from a shell.

    hajer attach-path     # the directory to put on PYTHONPATH to attach with no code change
    hajer doctor [--json] # what this process's SDK is configured to do, and whether it can
    hajer eval [-c promptfooconfig.yaml] [--workflow ID] [--upload]   # run an eval on the pinned engine

All three read `HAJER_*` from the environment the way the SDK does, and none takes a credential on the
command line: a key in an argument is a key in the shell history and in every process listing.

`eval` is the eval runner (`hajer.evals`): everything after the word is its own, parsed by `hajer.evals._cli`,
and every flag it does not know is handed to the engine, so `hajer eval --help` is the runner's help and
`hajer eval --engine-help` the engine's.

`doctor` is the first command to run when nothing is arriving. It answers the questions that account for
most of it — is the SDK the version you think it is, is the OpenTelemetry SDK installed, where would this
process's spans go, is `HAJER_BASE_URL` answering at all, is each setting coming from the environment or from a
default, and would this process be inert — and it answers them without sending a span: its one request is a
keyless GET of the liveness probe, and a 401 from that is a perfectly good answer (something is listening; the
key is a separate question).
"""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import sys
from pathlib import Path
from typing import Final

import httpx

from hajer import _bootstrap
from hajer._json import JsonObject, JsonValue
from hajer._paths import HEALTH_PATH, VERSION
from hajer._settings import HajerSettings, SettingSource, settings_sources
from hajer._telemetry import export_target
from hajer._transport import probe
from hajer.evals import _cli
from hajer.evals._engine import engine_status

#: Where the generated client-side redaction catalog lives, and the names it may publish its id under.
#: Read through `importlib` rather than imported: a build without the generated module must still be able
#: to answer `doctor`, and saying "absent" is the answer an operator needs.
_CATALOG_MODULE: Final[str] = "hajer._rules"
_CATALOG_IDS: Final[tuple[str, ...]] = ("CATALOG_ID", "CATALOG_DIGEST", "CATALOG_VERSION")
#: The distribution the `otel` extra installs, and what `doctor` says when it is missing.
_OTEL_DISTRIBUTION: Final[str] = "opentelemetry-sdk"
OTEL_ABSENT: Final[str] = "missing: pip install 'hajer[otel]' to export spans"


def _attach_path() -> int:
    """Print the directory holding `sitecustomize.py`. One line, so `$(…)` is the whole usage."""
    sys.stdout.write(f"{Path(_bootstrap.__file__).resolve().parent}\n")
    return 0


def redaction_catalog() -> str:
    """The id of the client-side redaction catalog this build carries, or why there is none."""
    if importlib.util.find_spec(_CATALOG_MODULE) is None:
        return "absent: this build carries no client-side redaction catalog"
    module = importlib.import_module(_CATALOG_MODULE)
    for name in _CATALOG_IDS:
        found = getattr(module, name, None)
        if isinstance(found, str) and found:
            return found
    return f"present at {_CATALOG_MODULE}, which publishes no catalog id"


def otel_status() -> str:
    """The OpenTelemetry SDK's version when the `otel` extra is installed, else what to install."""
    try:
        return f"opentelemetry-sdk {importlib.metadata.version(_OTEL_DISTRIBUTION)}"
    except importlib.metadata.PackageNotFoundError:
        return OTEL_ABSENT


def traces_status(settings: HajerSettings) -> str:
    """Where this process's spans would go, or why they would go nowhere — in the operator's own terms."""
    target = export_target(settings)
    if target is not None:
        return f"{target.url} (auth: {'bearer' if target.authenticated else 'none'})"
    because = _why_inert(settings)
    if because is not None:
        return f"nowhere: {because}"
    return "nowhere: HAJER_TRACES_ENABLED is off"


def _why_inert(settings: HajerSettings) -> str | None:
    """Why nothing would be sent, in the operator's own terms — or `None` when something would be."""
    if settings.disabled:
        return "HAJER_DISABLED is set: nothing is sent whatever else is configured"
    missing = [
        name for name, value in (("HAJER_API_KEY", settings.api_key), ("HAJER_TEAM_ID", settings.team_id)) if not value
    ]
    if missing:
        return f"{' and '.join(missing)} absent: no span leaves this process"
    return None


def _report(settings: HajerSettings, rows: tuple[SettingSource, ...], status: int | None) -> JsonObject:
    """Everything `doctor` knows, as one document. `--json` prints this; the prose prints the same facts."""
    settings_rows: list[JsonValue] = [
        {"name": row.name, "variable": row.variable, "value": row.value, "source": row.source} for row in rows
    ]
    return {
        "version": VERSION,
        "redactionCatalog": redaction_catalog(),
        "otel": otel_status(),
        "traces": traces_status(settings),
        "baseUrl": settings.base_url,
        "baseUrlPath": HEALTH_PATH,
        "baseUrlAnswers": status is not None,
        "baseUrlStatus": status,
        "inert": settings.inert,
        "inertBecause": _why_inert(settings),
        "settings": settings_rows,
    }


def doctor(*, as_json: bool, settings: HajerSettings, transport: httpx.BaseTransport | None = None) -> int:
    """Print what this process's SDK is configured to do, and whether it could do it."""
    rows = settings_sources(settings)
    status = probe(settings.base_url, HEALTH_PATH, timeout_ms=settings.trace_export_timeout_ms, transport=transport)
    report = _report(settings, rows, status)
    report["evals"] = engine_status(settings)
    if as_json:
        sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
        return 0
    reached = f"{status}" if status is not None else "no answer"
    lines = [
        f"hajer {VERSION}",
        f"otel            {otel_status()}",
        f"traces          {traces_status(settings)}",
        f"redaction       {redaction_catalog()}",
        f"base url        {settings.base_url}{HEALTH_PATH} -> {reached} (keyless GET)",
        f"inert           {'yes' if settings.inert else 'no'}",
        f"eval engine     {json.dumps(report['evals'], sort_keys=True)}",
    ]
    because = _why_inert(settings)
    if because is not None:
        lines.append(f"                {because}")
    lines.append("")
    lines.append(f"{'setting':<26} {'variable':<34} {'source':<8} value")
    lines.extend(f"{row.name:<26} {row.variable:<34} {row.source:<8} {row.value}" for row in rows)
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hajer", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    # `eval` is listed so `hajer --help` names every command, and never *parsed* here: `main` hands everything
    # after the word to `hajer.evals._cli.main`, whose flags have one definition — the runner's own.
    sub.add_parser("eval", help="Run a promptfoo configuration on the pinned eval engine; `hajer eval --help`")
    sub.add_parser(
        "attach-path",
        help="Print the directory to put on PYTHONPATH so HAJER_ATTACH=1 attaches with no code change",
    )
    doctor = sub.add_parser("doctor", help="What this process's SDK is configured to do, and whether it can")
    doctor.add_argument("--json", action="store_true", help="The same facts as one JSON document on stdout")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments[:1] == ["eval"]:
        return _cli.main(arguments[1:])
    options = build_parser().parse_args(arguments)
    if options.command == "attach-path":
        return _attach_path()
    if options.command == "doctor":
        return doctor(as_json=bool(options.json), settings=HajerSettings.from_env())
    return 2  # pragma: no cover - argparse's own `required=True` answers an unknown command first


if __name__ == "__main__":
    raise SystemExit(main())
