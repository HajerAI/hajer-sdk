"""`hajer eval` without Node: every seam injected, the engine a fake that writes the files a real one would.

What is asserted is the contract around the engine — the command it is started with, the environment it runs
in, where the payload lands, which exit code comes back — and that an upload's failure is reported on its own
line and never changes that code.
"""

from __future__ import annotations

import io
import json
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import httpx
import pytest

from hajer._json import JsonObject, JsonValue
from hajer._settings import PROMPTFOO_DISABLE_FLAGS, HajerSettings
from hajer.evals import _cli
from hajer.evals._cli import EXIT_ENGINE_INSTALL, EXIT_NO_RESULTS, EXIT_NODE, EXIT_USAGE, Runtime
from hajer.evals._engine import EngineReady, EngineUnavailable
from hajer.evals._git import GitContext
from hajer.evals._upload import UploadReceipt

RUN_ID = "evalrun_0123456789abcdef0123456789abcdef"


def ready(tmp_path: Path) -> EngineReady:
    entry = tmp_path / "engine" / "node_modules" / "promptfoo" / "dist" / "src" / "entrypoint.js"
    return EngineReady(
        entrypoint=entry,
        node="/usr/bin/node",
        node_version="22.23.2",
        version="0.123.1",
        directory=tmp_path / "engine",
        installed_now=False,
    )


def results_document(*, success: bool = True) -> JsonObject:
    """The smallest promptfoo output document with one platform-correlated row and its trace."""
    row: JsonObject = {
        "success": success,
        "score": 1.0 if success else 0.0,
        "failureReason": 0 if success else 1,
        "latencyMs": 12,
        "testIdx": 0,
        "promptIdx": 0,
        "provider": {"id": "file://provider.py", "label": "Support agent"},
        "prompt": {"raw": "Has my refund gone through?"},
        "response": {"output": "Your refund is still pending."},
        "vars": {"message": "Has my refund gone through?"},
        "metadata": {"testCaseId": "eval_refund_pending", "hajer": {"workflowId": "wf_support"}},
        "testCase": {"description": "Pending refund", "vars": {}},
        "gradingResult": {"pass": success, "score": 1.0, "reason": "ok", "componentResults": []},
        "traceId": "a" * 32,
        "evaluationId": "eval-abc",
    }
    return {
        "evalId": "eval-abc-2026-10-04T00:00:00",
        "results": {
            "version": 3,
            "timestamp": "2026-10-04T00:00:00.000Z",
            "stats": {"successes": 1 if success else 0, "failures": 0 if success else 1, "errors": 0},
            "prompts": [],
            "results": [row],
        },
        "config": {"description": "Support suite"},
        "shareableUrl": None,
        "metadata": {"promptfooVersion": "0.123.1"},
        "traces": [
            {
                "traceId": "a" * 32,
                "evaluationId": "eval-abc",
                "testCaseId": "eval_refund_pending",
                "metadata": {"testIdx": 0, "promptIdx": 0},
                "spans": [
                    {
                        "spanId": "b" * 16,
                        "name": "workflow wf_support",
                        "startTime": 1,
                        "endTime": 2,
                        "attributes": {"hajer.workflow.id": "wf_support", "deployment.environment": "eval"},
                        "statusCode": "ok",
                    }
                ],
            }
        ],
    }


@dataclass
class FakeEngine:
    """Stands in for `subprocess.run`: records the command and the environment, then writes what promptfoo would."""

    exit_code: int = 0
    document: JsonObject | None = field(default_factory=results_document)
    report: JsonObject | None = None
    write_results: bool = True
    commands: list[list[str]] = field(default_factory=list)
    environments: list[Mapping[str, str]] = field(default_factory=list)

    def __call__(
        self,
        command: list[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        check: bool = False,
        **_options: object,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, check
        if command[-1:] == ["--version"]:
            return subprocess.CompletedProcess(command, 0, "v22.23.2\n", "")  # `engine_status` asking Node
        self.commands.append(list(command))
        self.environments.append(dict(env or {}))
        if "-o" in command and self.write_results and self.document is not None:
            Path(command[command.index("-o") + 1]).write_text(json.dumps(self.document))
        if self.report is not None and env is not None and env.get("HAJER_EVAL_HOOK_REPORT"):
            Path(env["HAJER_EVAL_HOOK_REPORT"]).write_text(json.dumps(self.report))
        return subprocess.CompletedProcess(command, self.exit_code, "", "")


def _which(name: str) -> str | None:
    return f"/usr/bin/{name}"


def _ensure_ready(tmp_path: Path) -> Callable[..., EngineReady | EngineUnavailable]:
    def ensure(_settings: HajerSettings, **_kwargs: object) -> EngineReady | EngineUnavailable:
        return ready(tmp_path)

    return ensure


def _skipped_upload(_payload: JsonObject, **_kwargs: object) -> UploadReceipt:
    return UploadReceipt("skipped", None, "INERT", 0, ())


def _no_git(_root: Path, _ci: Mapping[str, str], **_kwargs: object) -> GitContext | None:
    return None


def _no_ci() -> dict[str, str]:
    return {}


def _fixed_run_id() -> str:
    return RUN_ID


def runtime(tmp_path: Path, engine: FakeEngine, **overrides: object) -> Runtime:
    base: dict[str, object] = {
        "run": engine,
        "which": _which,
        "ensure": _ensure_ready(tmp_path),
        "upload": _skipped_upload,
        "git": _no_git,
        "ci_environment": _no_ci,
        "python_executable": "/venv/bin/python",
        "new_run_id": _fixed_run_id,
        "cwd": tmp_path / "project",
        "stdout": io.StringIO(),
        "stderr": io.StringIO(),
    }
    base.update(overrides)
    return Runtime(**base)  # pyright: ignore[reportArgumentType] - a test's seams, typed by the dataclass itself


@pytest.fixture
def project(tmp_path: Path) -> Path:
    directory = tmp_path / "project"
    directory.mkdir()
    (directory / "promptfooconfig.yaml").write_text("prompts: ['{{message}}']\nproviders: [echo]\ntests: []\n")
    return directory


@pytest.fixture
def settings(tmp_path: Path) -> HajerSettings:
    return HajerSettings(cache_dir=str(tmp_path / "cache"), eval_otlp_port=43180)


def out(rt: Runtime) -> str:
    return cast(io.StringIO, rt.stdout).getvalue()


def err(rt: Runtime) -> str:
    return cast(io.StringIO, rt.stderr).getvalue()


class TestTheEngineMustBeThere:
    @pytest.mark.parametrize(
        ("reason", "code"),
        [
            ("NODE_MISSING", EXIT_NODE),
            ("NODE_TOO_OLD", EXIT_NODE),
            ("NPM_MISSING", EXIT_NODE),
            ("INSTALL_FAILED", EXIT_ENGINE_INSTALL),
        ],
    )
    def test_an_unavailable_engine_names_itself_and_exits_with_its_own_code(
        self, tmp_path: Path, project: Path, settings: HajerSettings, reason: str, code: int
    ) -> None:
        engine = FakeEngine()
        unavailable = EngineUnavailable(reason, "install Node 22.22 or newer")  # pyright: ignore[reportArgumentType]

        def ensure(_settings: HajerSettings, **_kwargs: object) -> EngineReady | EngineUnavailable:
            return unavailable

        rt = runtime(tmp_path, engine, ensure=ensure)
        assert _cli.main([], settings=settings, runtime=rt) == code
        assert f"{reason}: install Node" in err(rt)
        assert engine.commands == [], "nothing is started without an engine"


class TestConfiguration:
    def test_no_configuration_is_a_usage_error(self, tmp_path: Path, settings: HajerSettings) -> None:
        (tmp_path / "project").mkdir()
        rt = runtime(tmp_path, FakeEngine())
        assert _cli.main([], settings=settings, runtime=rt) == EXIT_USAGE
        assert "no promptfoo configuration" in err(rt)

    def test_a_named_configuration_that_does_not_exist_is_a_usage_error(
        self, tmp_path: Path, project: Path, settings: HajerSettings
    ) -> None:
        rt = runtime(tmp_path, FakeEngine())
        assert _cli.main(["-c", "missing.yaml"], settings=settings, runtime=rt) == EXIT_USAGE
        assert "missing.yaml" in err(rt)


class TestARun:
    def test_the_engine_is_started_with_the_users_configs_then_the_overlay_then_the_passthrough(
        self, tmp_path: Path, project: Path, settings: HajerSettings
    ) -> None:
        engine = FakeEngine()
        rt = runtime(tmp_path, engine)
        code = _cli.main(["--no-cache", "-j", "2"], settings=settings, runtime=rt)
        assert code == 0
        (command,) = engine.commands
        run_dir = tmp_path / "cache" / "runs" / RUN_ID
        assert command[:3] == ["/usr/bin/node", str(ready(tmp_path).entrypoint), "eval"]
        assert command[3:5] == ["-c", str((project / "promptfooconfig.yaml").resolve())]
        assert command[5:7] == ["-c", str(run_dir / "overlay.json")]
        assert command[7:9] == ["-o", str(run_dir / "results.json")]
        assert command[9:] == ["--no-cache", "-j", "2"], "every unknown flag reaches promptfoo in order"
        overlay = json.loads((run_dir / "overlay.json").read_text())
        assert overlay["tracing"]["enabled"] is True
        assert overlay["extensions"][0].endswith("/hajer/evals/_hook_entry.py:beforeAll")

    def test_the_engine_environment_is_switched_off_from_phoning_home_and_points_at_this_python(
        self, tmp_path: Path, project: Path, settings: HajerSettings
    ) -> None:
        engine = FakeEngine()
        rt = runtime(tmp_path, engine)
        _cli.main([], settings=settings, runtime=rt)
        (environment,) = engine.environments
        assert all(environment[flag] == "1" for flag in PROMPTFOO_DISABLE_FLAGS)
        assert environment["PROMPTFOO_PYTHON"] == "/venv/bin/python"
        assert environment["HAJER_ENVIRONMENT"] == "eval"
        assert environment["HAJER_EVAL_RUN_ID"] == RUN_ID
        assert environment["HAJER_OTLP_ENDPOINT"].startswith("http://127.0.0.1:")
        assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in environment, "the engine reads it as a whole URL and would 404"
        assert "HAJER_API_KEY" not in environment
        assert environment["PROMPTFOO_CONFIG_DIR"] == str(tmp_path / "cache" / "runs" / RUN_ID / "promptfoo")

    def test_the_payload_is_written_to_the_run_directory_and_to_payload_out(
        self, tmp_path: Path, project: Path, settings: HajerSettings
    ) -> None:
        def committed(_root: Path, _ci: Mapping[str, str], **_kwargs: object) -> GitContext | None:
            return GitContext(commit_sha="c" * 40)

        rt = runtime(tmp_path, FakeEngine(), git=committed)
        code = _cli.main(["--payload-out", "out/run.json"], settings=settings, runtime=rt)
        assert code == 0
        kept = json.loads((tmp_path / "cache" / "runs" / RUN_ID / "payload.json").read_text())
        asked = json.loads((project / "out" / "run.json").read_text())
        assert kept == asked
        assert asked["schemaVersion"] == 1
        assert asked["runId"] == RUN_ID
        assert asked["status"] == "passed"
        assert asked["engine"]["version"] == "0.123.1"
        assert asked["git"]["commitSha"] == "c" * 40
        assert asked["config"]["path"] == "promptfooconfig.yaml"
        (result,) = asked["results"]
        assert (result["testCaseId"], result["workflowId"], result["traceId"]) == (
            "eval_refund_pending",
            "wf_support",
            "a" * 32,
        )
        assert "passed: 1/1 passed" in out(rt)
        assert f"run {RUN_ID}" in out(rt)

    def test_the_engines_failure_code_passes_through_and_the_payload_is_still_written(
        self, tmp_path: Path, project: Path, settings: HajerSettings
    ) -> None:
        rt = runtime(tmp_path, FakeEngine(exit_code=100, document=results_document(success=False)))
        assert _cli.main([], settings=settings, runtime=rt) == 100
        payload = json.loads((tmp_path / "cache" / "runs" / RUN_ID / "payload.json").read_text())
        assert payload["status"] == "failed"
        assert payload["engineExitCode"] == 100
        assert payload["results"][0]["outcome"] == "failed"

    def test_hook_warnings_are_printed_and_hook_errors_stop_with_a_usage_code(
        self, tmp_path: Path, project: Path, settings: HajerSettings
    ) -> None:
        warning: JsonObject = {
            "warnings": ["[W_NO_TEST_CASE_ID] test #3: add metadata.testCaseId"],
            "errors": [],
            "tests": [
                {
                    "index": 3,
                    "label": "#3",
                    "workflowId": "wf_support",
                    "warnings": ["[W_NO_TEST_CASE_ID] test #3: add metadata.testCaseId"],
                }
            ],
        }
        rt = runtime(tmp_path, FakeEngine(report=warning))
        assert _cli.main([], settings=settings, runtime=rt) == 0
        assert "W_NO_TEST_CASE_ID" in err(rt)
        failing: JsonObject = {
            "warnings": [],
            "errors": ["[E_HAJER_INVALID] test #1: workflowId must be a string"],
            "tests": [],
        }
        rt = runtime(tmp_path, FakeEngine(exit_code=1, report=failing, write_results=False))
        assert _cli.main([], settings=settings, runtime=rt) == EXIT_USAGE
        assert "E_HAJER_INVALID" in err(rt)

    def test_no_results_after_a_clean_exit_is_its_own_code_and_after_a_failed_one_is_the_engines(
        self, tmp_path: Path, project: Path, settings: HajerSettings
    ) -> None:
        assert (
            _cli.main([], settings=settings, runtime=runtime(tmp_path, FakeEngine(write_results=False)))
            == EXIT_NO_RESULTS
        )
        assert (
            _cli.main([], settings=settings, runtime=runtime(tmp_path, FakeEngine(exit_code=7, write_results=False)))
            == 7
        )

    def test_filters_reach_the_hook_through_the_environment_and_the_payload(
        self, tmp_path: Path, project: Path, settings: HajerSettings
    ) -> None:
        engine = FakeEngine()
        rt = runtime(tmp_path, engine)
        _cli.main(
            ["--workflow", "wf_support", "--obligation", "obl_a", "--obligation", "obl_b"],
            settings=settings,
            runtime=rt,
        )
        (environment,) = engine.environments
        assert environment["HAJER_EVAL_WORKFLOW"] == "wf_support"
        assert environment["HAJER_EVAL_OBLIGATIONS"] == "obl_a,obl_b"
        payload = json.loads((tmp_path / "cache" / "runs" / RUN_ID / "payload.json").read_text())
        assert payload["filters"] == {"workflowId": "wf_support", "obligationIds": ["obl_a", "obl_b"]}

    def test_install_only_prints_the_engine_status_and_starts_nothing(
        self, tmp_path: Path, project: Path, settings: HajerSettings
    ) -> None:
        engine = FakeEngine()
        rt = runtime(tmp_path, engine)
        assert _cli.main(["--install-only"], settings=settings, runtime=rt) == 0
        status = json.loads(out(rt))
        assert isinstance(status, dict)
        assert engine.commands == [] or all("eval" not in command[2:3] for command in engine.commands)


class TestUpload:
    def test_an_upload_is_only_attempted_when_asked_and_its_receipt_is_reported(
        self, tmp_path: Path, project: Path, settings: HajerSettings
    ) -> None:
        sent: list[JsonObject] = []

        def uploaded(payload: JsonObject, **kwargs: object) -> UploadReceipt:
            sent.append(payload)
            return UploadReceipt("uploaded", 201, None, 1, ())

        rt = runtime(tmp_path, FakeEngine(), upload=uploaded)
        assert _cli.main([], settings=settings, runtime=rt) == 0
        assert sent == [], "no --upload, no upload"
        assert "upload not requested" in out(rt)
        rt = runtime(tmp_path, FakeEngine(), upload=uploaded)
        assert _cli.main(["--upload"], settings=settings, runtime=rt) == 0
        assert len(sent) == 1
        assert sent[0]["runId"] == RUN_ID
        assert "upload uploaded" in out(rt)

    def test_a_failed_upload_is_reported_on_stderr_and_never_changes_the_exit_code(
        self, tmp_path: Path, project: Path, settings: HajerSettings
    ) -> None:
        def refused(payload: JsonObject, **kwargs: object) -> UploadReceipt:
            return UploadReceipt("failed", 503, "UNREACHABLE", 3, ())

        rt = runtime(tmp_path, FakeEngine(), upload=refused)
        assert _cli.main(["--upload"], settings=settings, runtime=rt) == 0
        assert "upload failed (UNREACHABLE)" in err(rt)
        assert "the payload is kept at" in err(rt)
        rt = runtime(tmp_path, FakeEngine(exit_code=100, document=results_document(success=False)), upload=refused)
        assert _cli.main(["--upload"], settings=settings, runtime=rt) == 100

    def test_the_project_id_flag_reaches_the_upload(
        self, tmp_path: Path, project: Path, settings: HajerSettings
    ) -> None:
        seen: list[object] = []

        def recording(payload: JsonObject, **kwargs: object) -> UploadReceipt:
            seen.append(kwargs.get("project_id"))
            return UploadReceipt("uploaded", 201, None, 1, ())

        rt = runtime(
            tmp_path,
            FakeEngine(),
            upload=recording,
            transport=httpx.MockTransport(lambda _request: httpx.Response(201)),
        )
        _cli.main(["--upload", "--project-id", "proj-9"], settings=settings, runtime=rt)
        assert seen == ["proj-9"]


def test_the_console_script_hands_eval_to_the_runner() -> None:
    """`hajer eval …` is parsed by the runner, not by the operator parser: its help is the runner's."""
    from hajer.__main__ import main  # noqa: PLC0415 - the module under test

    with pytest.raises(SystemExit) as raised:
        main(["eval", "--help"])
    assert raised.value.code == 0


def test_run_ids_are_minted_before_the_engine_starts_and_are_unique() -> None:
    first, second = _cli._new_run_id(), _cli._new_run_id()  # pyright: ignore[reportPrivateUsage]
    assert first.startswith("evalrun_")
    assert second.startswith("evalrun_")
    assert first != second


def test_a_payload_value_is_json(tmp_path: Path) -> None:
    value: JsonValue = {"a": [1, "b", None]}
    _cli._write_json(tmp_path / "x" / "y.json", {"v": value})  # pyright: ignore[reportPrivateUsage]
    assert json.loads((tmp_path / "x" / "y.json").read_text()) == {"v": {"a": [1, "b", None]}}
