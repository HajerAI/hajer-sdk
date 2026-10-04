"""`hajer eval`: run the pinned engine on the user's promptfoo configuration and produce the platform payload.

Everything the engine does stays the engine's — configuration, providers, assertions, the exit code. This
command adds what the platform needs around it: a run id minted before the engine starts (so the spans carry
it), tracing switched on and the Hajer hook attached through an overlay config, the engine's phone-home paths
switched off, the results translated into the versioned payload, and an optional upload whose failure is
reported on its own line and **never changes the exit code**. A green eval with a failed upload is a green eval.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, TextIO
from uuid import uuid4

import httpx
from pydantic import ValidationError

from hajer._json import JsonObject
from hajer._settings import HajerSettings, ci_environment_snapshot, eval_engine_environment
from hajer.evals import _hook_entry
from hajer.evals._config import (
    HOOK_REPORT_NAME,
    PAYLOAD_NAME,
    RESULTS_NAME,
    discover_configs,
    free_port,
    run_directory,
    write_overlay,
)
from hajer.evals._engine import EngineReady, EngineUnavailable, engine_status, ensure_engine, pinned
from hajer.evals._git import GitContext, git_context
from hajer.evals._hook import format_warnings, read_report
from hajer.evals._payload import EngineInfo, Filters
from hajer.evals._results import read_results, translate
from hajer.evals._upload import UploadReceipt, upload_eval_run

#: `hajer eval`'s own exit codes. The engine's code (0, or 100 when a test fails) passes through untouched;
#: these name the things that stopped the engine from running or from being read.
EXIT_USAGE: Final[int] = 2
EXIT_NODE: Final[int] = 3
EXIT_ENGINE_INSTALL: Final[int] = 4
EXIT_NO_RESULTS: Final[int] = 5

#: The engine failures that are the machine's, not the install's: the user installs Node.
_NODE_REASONS: Final[frozenset[str]] = frozenset({"NODE_MISSING", "NODE_TOO_OLD", "NPM_MISSING"})

Run = Callable[..., subprocess.CompletedProcess[str]]


def _new_run_id() -> str:
    return f"evalrun_{uuid4().hex}"


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Runtime:
    """Every seam `main` reaches the world through, so a test can run it without Node, a socket or a clock."""

    run: Run = subprocess.run
    which: Callable[[str], str | None] = shutil.which
    ensure: Callable[..., EngineReady | EngineUnavailable] = ensure_engine
    upload: Callable[..., UploadReceipt] = upload_eval_run
    git: Callable[..., GitContext | None] = git_context
    ci_environment: Callable[[], Mapping[str, str]] = ci_environment_snapshot
    engine_environment: Callable[..., dict[str, str]] = eval_engine_environment
    python_executable: str = sys.executable
    new_run_id: Callable[[], str] = _new_run_id
    now: Callable[[], datetime] = _now
    cwd: Path | None = None
    transport: httpx.BaseTransport | None = None
    stdout: TextIO = field(default_factory=lambda: sys.stdout)
    stderr: TextIO = field(default_factory=lambda: sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hajer eval",
        description=(
            "Run a promptfoo configuration on the pinned eval engine and produce the Hajer payload. "
            "Every flag not listed here is handed to `promptfoo eval` unchanged (`hajer eval --engine-help`)."
        ),
    )
    parser.add_argument(
        "-c",
        "--config",
        action="append",
        default=[],
        metavar="PATH",
        help="A promptfoo configuration file (repeatable). Default: promptfoo's own promptfooconfig.* in the working directory.",
    )
    parser.add_argument("--workflow", metavar="ID", help="Run only the tests whose metadata.hajer.workflowId is ID")
    parser.add_argument(
        "--obligation",
        action="append",
        default=[],
        metavar="ID",
        help="Run only the tests whose metadata.hajer.obligationIds contain ID (repeatable; any of them)",
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help="Upload the payload to Hajer (needs HAJER_API_KEY and HAJER_TEAM_ID)",
    )
    parser.add_argument(
        "--no-upload", action="store_true", help="Do not upload (the default; named so a CI script can say it)"
    )
    parser.add_argument(
        "--payload-out", type=Path, metavar="PATH", help="Also write the payload here (default: the run directory)"
    )
    parser.add_argument("--install-only", action="store_true", help="Install the pinned engine into the cache and stop")
    parser.add_argument(
        "--engine-help", action="store_true", help="Print `promptfoo eval --help` for the pinned engine"
    )
    return parser


def main(argv: list[str], *, settings: HajerSettings | None = None, runtime: Runtime | None = None) -> int:
    runtime = runtime or Runtime()
    settings = settings or HajerSettings.from_env()
    options, passthrough = build_parser().parse_known_args(argv)
    cwd = (runtime.cwd or Path.cwd()).resolve()
    err = runtime.stderr

    engine = runtime.ensure(settings, run=runtime.run, which=runtime.which)
    if isinstance(engine, EngineUnavailable):
        err.write(f"hajer eval: {engine.reason}: {engine.detail}\n")
        return EXIT_NODE if engine.reason in _NODE_REASONS else EXIT_ENGINE_INSTALL
    if options.install_only:
        status = engine_status(settings, run=runtime.run, which=runtime.which)
        runtime.stdout.write(json.dumps(status, indent=2, sort_keys=True) + "\n")
        return 0
    if options.engine_help:
        return int(
            runtime.run([engine.node, str(engine.entrypoint), "eval", "--help"], cwd=str(cwd), check=False).returncode
        )

    configs = discover_configs(list(options.config), cwd)
    if not configs:
        err.write(f"hajer eval: no promptfoo configuration in {cwd}; pass one with -c PATH\n")
        return EXIT_USAGE
    missing = [path for path in configs if not path.is_file()]
    if missing:
        err.write(f"hajer eval: configuration not found: {', '.join(str(path) for path in missing)}\n")
        return EXIT_USAGE

    run_id = runtime.new_run_id()
    started = runtime.now()
    run_dir = run_directory(settings, run_id)
    port = free_port(settings.eval_otlp_port)
    overlay = write_overlay(run_dir, port=port, hook_entry=Path(_hook_entry.__file__).resolve())
    obligations = tuple(str(item) for item in options.obligation)
    environment = runtime.engine_environment(
        settings,
        run_id=run_id,
        run_dir=str(run_dir),
        otlp_port=port,
        python_executable=runtime.python_executable,
        workflow=options.workflow,
        obligations=obligations,
    )
    results_path = run_dir / RESULTS_NAME
    command: list[str] = [engine.node, str(engine.entrypoint), "eval"]
    for path in configs:
        command.extend(["-c", str(path)])
    command.extend(["-c", str(overlay), "-o", str(results_path), *passthrough])
    completed = runtime.run(command, cwd=str(cwd), env=environment, check=False)
    exit_code = int(completed.returncode)

    report = read_report(run_dir / HOOK_REPORT_NAME)
    if report is not None:
        for line in format_warnings(report):
            err.write(line + "\n")
        errors = report.get("errors")
        if isinstance(errors, list) and errors:
            for item in errors:
                err.write(f"hajer eval: error {item}\n")
            return EXIT_USAGE

    document = read_results(results_path)
    if document is None:
        if exit_code != 0:
            return exit_code  # the engine said what went wrong; there is nothing to translate
        err.write(f"hajer eval: the engine finished but {results_path} is not a readable results document\n")
        return EXIT_NO_RESULTS

    git = runtime.git(cwd, runtime.ci_environment(), timeout_s=float(settings.eval_git_timeout_s))
    eval_id = document.get("evalId")
    try:
        payload = translate(
            document,
            run_id=run_id,
            created_at=started.isoformat().replace("+00:00", "Z"),
            engine=EngineInfo(
                name="promptfoo",
                version=engine.version,
                lockfile_digest=pinned().lockfile_digest,
                node_version=engine.node_version,
                eval_id=eval_id if isinstance(eval_id, str) else None,
            ),
            exit_code=exit_code,
            git=git.to_wire() if git is not None else None,
            config_path=_relative(configs[0], cwd),
            hook_report=report,
            filters=Filters(workflow_id=options.workflow, obligation_ids=obligations),
            settings=settings,
        )
    except ValidationError as error:
        err.write(
            f"hajer eval: the engine's results document is not one this version can read: {error.error_count()} problems\n"
        )
        return exit_code if exit_code != 0 else EXIT_NO_RESULTS
    wire = payload.to_wire()
    payload_path = run_dir / PAYLOAD_NAME
    _write_json(payload_path, wire)
    if options.payload_out is not None:
        target = options.payload_out if options.payload_out.is_absolute() else cwd / options.payload_out
        _write_json(target, wire)
        payload_path = target

    upload_status = "not requested"
    if options.upload and not options.no_upload:
        receipt = runtime.upload(wire, settings=settings, transport=runtime.transport)
        upload_status = receipt.status if receipt.reason is None else f"{receipt.status} ({receipt.reason})"
        if receipt.status != "uploaded":
            err.write(f"hajer eval: upload {upload_status}; the payload is kept at {payload_path}\n")

    stats = payload.stats
    runtime.stdout.write(
        f"hajer eval: {payload.status}: {stats.passed}/{stats.total} passed, {stats.failed} failed, "
        f"{stats.errored} errored; run {run_id}; payload {payload_path}; upload {upload_status}\n"
    )
    return exit_code


def _relative(path: Path, cwd: Path) -> str:
    try:
        return str(path.relative_to(cwd))
    except ValueError:
        return str(path)


def _write_json(target: Path, document: JsonObject) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
