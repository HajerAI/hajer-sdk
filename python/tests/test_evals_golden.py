"""The golden end-to-end test: `hajer eval` on the pinned engine, against the example application, for real.

Everything the unit tests fake is real here — Node, the pinned promptfoo installed from npm into the cache, its
OTLP receiver on loopback, the Python `beforeAll` hook, the example provider and grader, the spans the example
application emits. What is asserted is the spec's definition of done: an ordinary promptfoo configuration
runs; the trace reaches the engine; the trajectory and rubric assertions pass on it; the result is correlated
with the test case, the workflow, the obligations, the trace and the commit; the payload validates; the upload
is idempotent and never changes the exit code; a failing case exits with the engine's code; and nothing leaves
the machine but the loopback traffic.

Opt-in (`pytest --engine`): it needs Node >= 22.22 on PATH and, on a cold cache, the npm registry once.
"""

from __future__ import annotations

import io
import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

import httpx
import pytest

from hajer._json import JsonObject, JsonValue
from hajer._settings import HajerSettings, eval_engine_environment
from hajer.evals import _cli
from hajer.evals._cli import EXIT_USAGE, Runtime
from hajer.evals._engine import EngineReady, ensure_engine, pinned
from hajer.evals._payload import EvalRunPayload

pytestmark = [pytest.mark.engine, pytest.mark.loopback]

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "support"
GOLDEN_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "evals" / "results.golden.json"
ENGINE_FAILED_TESTS = 100
#: The hosts the pinned engine tries to reach on its own during a loopback-only run, documented in
#: docs/evals-engine.md ("What `hajer eval` switches off"): promptfoo's one `telemetry disabled` event, and the
#: cloud-identity probes two of its bundled provider SDKs make at start-up. The recording proxy refuses all of
#: them and the run is unaffected; anything outside this set fails the test.
KNOWN_ENGINE_EGRESS = frozenset({"r.promptfoo.app", "169.254.169.254", "metadata.google.internal."})


@pytest.fixture(scope="module")
def settings() -> HajerSettings:
    """The real cache (so CI's `actions/cache` on `~/.cache/hajer/engine` is what gets reused), no credential."""
    base = HajerSettings.from_env()
    return HajerSettings(cache_dir=base.cache_dir, eval_runs_keep=base.eval_runs_keep)


@pytest.fixture(scope="module")
def engine(settings: HajerSettings) -> EngineReady:
    found = ensure_engine(settings)
    assert isinstance(found, EngineReady), f"{found!r}: Node >= 22.22 and the npm registry are needed once"
    return found


def run(
    args: list[str], settings: HajerSettings, *, cwd: Path = EXAMPLE, **seams: object
) -> tuple[int, str, str, JsonObject | None]:
    stdout, stderr = io.StringIO(), io.StringIO()
    runtime = Runtime(cwd=cwd, stdout=stdout, stderr=stderr, **seams)  # pyright: ignore[reportArgumentType] - test seams
    code = _cli.main(args, settings=settings, runtime=runtime)
    payload_line = next((line for line in stdout.getvalue().splitlines() if "payload " in line), "")
    payload_path = payload_line.partition("payload ")[2].partition(";")[0].strip()
    document = json.loads(Path(payload_path).read_text()) if payload_path and Path(payload_path).exists() else None
    return code, stdout.getvalue(), stderr.getvalue(), cast(JsonObject | None, document)


def results_by_id(payload: JsonObject) -> dict[str, JsonObject]:
    rows = cast(list[JsonValue], payload["results"])
    return {str(cast(JsonObject, row)["testCaseId"]): cast(JsonObject, row) for row in rows}


def attributes_of(row: JsonObject) -> list[JsonObject]:
    return [cast(JsonObject, cast(JsonObject, span)["attributes"]) for span in cast(list[JsonValue], row["spans"])]


class TestTheEngine:
    def test_the_installed_engine_is_the_pinned_version_and_is_reused(
        self, settings: HajerSettings, engine: EngineReady
    ) -> None:
        assert engine.version == pinned().version
        again = ensure_engine(settings)
        assert isinstance(again, EngineReady)
        assert again.installed_now is False, "a second run never touches npm"
        assert engine.node_version.split(".")[0].lstrip("v").isdigit()
        assert int(engine.node_version.lstrip("v").split(".")[0]) >= 22


@pytest.fixture(scope="module")
def golden(
    settings: HajerSettings,
    engine: EngineReady,
    tmp_path_factory: pytest.TempPathFactory,
    request: pytest.FixtureRequest,
) -> tuple[int, str, str, JsonObject]:
    """One real run of the example suite, shared by every assertion about it.

    With `--update-evals-golden` the engine's own `results.json` from this run is recorded as
    `tests/fixtures/evals/results.golden.json`, with every path under the home directory scrubbed, so the
    translation is regression-tested against a real document on the Node-free matrix (`test_evals_golden_fixture.py`).
    """
    del engine
    out = tmp_path_factory.mktemp("golden") / "payload.json"
    code, stdout, stderr, payload = run(["--payload-out", str(out), "--no-cache"], settings)
    assert payload is not None, stderr + stdout
    if request.config.getoption("--update-evals-golden"):
        source = Path(settings.cache_dir) / "runs" / str(payload["runId"]) / "results.json"
        text = source.read_text(encoding="utf-8").replace(str(Path.home()), "~")
        GOLDEN_FIXTURE.write_text(json.dumps(json.loads(text), indent=2) + "\n", encoding="utf-8")
    return code, stdout, stderr, payload


class TestTheGoldenRun:
    def test_the_suite_passes_and_the_payload_validates(self, golden: tuple[int, str, str, JsonObject]) -> None:
        code, stdout, _stderr, payload = golden
        assert code == 0, stdout
        assert payload["schemaVersion"] == 1
        validated = EvalRunPayload.model_validate(payload)
        assert validated.status == "passed"
        assert validated.stats.total == 4
        assert validated.stats.passed == 4
        assert validated.engine.version == pinned().version
        assert validated.engine.lockfile_digest == pinned().lockfile_digest

    def test_every_row_is_classified_and_the_platform_rows_carry_a_trace(
        self, golden: tuple[int, str, str, JsonObject]
    ) -> None:
        payload = golden[3]
        rows = results_by_id(payload)
        pending, completed = rows["eval_refund_pending"], rows["eval_refund_completed"]
        assert pending["correlation"] == "platform"
        assert completed["correlation"] == "platform"
        assert pending["workflowId"] == "wf_support", "inherited from defaultTest through the hook's deep merge"
        assert pending["obligationIds"] == ["obl_refund_status_disclosed"]
        assert pending["componentIds"] == ["cmp_refund_agent"]
        assert isinstance(pending["traceId"], str)
        assert len(pending["traceId"]) == 32
        greeting = rows["eval_greeting"]
        assert greeting["correlation"] == "platform", "no hajer key of its own, but defaultTest names the workflow"
        assert greeting["workflowId"] == "wf_support"
        assert greeting["obligationIds"] == []
        assert all(row["correlation"] == "platform" for row in rows.values())
        positional = [
            row
            for row in rows.values()
            if row["correlation"] == "platform" and not str(row["testCaseId"]).startswith("eval_")
        ]
        assert len(positional) == 1, "the id-less test falls back to its position"

    def test_the_spans_carry_the_production_ids_and_the_eval_context(
        self, golden: tuple[int, str, str, JsonObject]
    ) -> None:
        payload = golden[3]
        pending = results_by_id(payload)["eval_refund_pending"]
        every = attributes_of(pending)
        attributes = [a for a in every if "hajer.workflow.id" in a]
        engine_spans = [a for a in every if "gen_ai.evaluation.name" in a]
        assert engine_spans, "the engine's own grader spans share the trace: the traceparent was honoured"
        assert any(a.get("hajer.workflow.id") == "wf_support" for a in attributes)
        assert any(a.get("hajer.component.id") == "cmp_refund_agent" for a in attributes)
        tool = next(a for a in attributes if a.get("hajer.tool.id") == "tool_refund_status")
        assert tool["gen_ai.tool.name"] == "get_refund_status"
        assert tool["gen_ai.operation.name"] == "execute_tool"
        assert tool["hajer.workflow.id"] == "wf_support"
        assert tool["hajer.component.id"] == "cmp_refund_agent"
        assert all(a.get("deployment.environment") == "eval" for a in attributes)
        assert all(a.get("hajer.eval.run.id") == payload["runId"] for a in attributes)
        assert all(a.get("hajer.eval.test_case.id") == "eval_refund_pending" for a in attributes)
        assert all(a.get("hajer.eval.obligation.ids") == ["obl_refund_status_disclosed"] for a in attributes)
        summary = cast(JsonObject, pending["spanSummary"])
        assert summary["toolNames"] == ["get_refund_status"]
        assert summary["componentIds"] == ["cmp_refund_agent"]

    def test_the_trajectory_rubric_and_trace_assertions_were_graded_on_the_trace(
        self, golden: tuple[int, str, str, JsonObject]
    ) -> None:
        pending = results_by_id(golden[3])["eval_refund_pending"]
        graded = {
            str(cast(JsonObject, a)["type"]): cast(JsonObject, a) for a in cast(list[JsonValue], pending["assertions"])
        }
        assert set(graded) == {"contains", "trajectory:tool-used", "trace-span-count", "llm-rubric"}
        assert all(bool(a["passed"]) for a in graded.values()), graded
        assert "forbidden" in str(graded["llm-rubric"]["reason"]), "graded by grader.py, with no model key anywhere"

    def test_the_run_knows_its_commit_and_warned_about_the_id_less_test(
        self, golden: tuple[int, str, str, JsonObject]
    ) -> None:
        _code, _stdout, stderr, payload = golden
        git = cast(JsonObject, payload["git"])
        assert isinstance(git["commitSha"], str)
        assert len(git["commitSha"]) == 40
        assert any("W_NO_TEST_CASE_ID" in str(w) for w in cast(list[JsonValue], payload["warnings"]))
        assert "W_NO_TEST_CASE_ID" in stderr
        assert cast(JsonObject, payload["config"])["path"] == "promptfooconfig.yaml"


class TestFiltersAndRefusals:
    def test_the_obligation_filter_keeps_only_the_tests_that_cover_it(
        self, settings: HajerSettings, engine: EngineReady
    ) -> None:
        del engine
        code, _stdout, _stderr, payload = run(["--obligation", "obl_refund_status_disclosed", "--no-cache"], settings)
        assert code == 0
        assert payload is not None
        assert cast(JsonObject, payload["stats"])["total"] == 3
        assert "eval_greeting" not in results_by_id(payload)
        assert payload["filters"] == {"workflowId": None, "obligationIds": ["obl_refund_status_disclosed"]}

    def test_a_workflow_filter_nothing_declares_stops_the_run(
        self, settings: HajerSettings, engine: EngineReady
    ) -> None:
        del engine
        code, _stdout, stderr, payload = run(["--workflow", "wf_other", "--no-cache"], settings)
        assert code == EXIT_USAGE
        assert payload is None
        assert "no test matches the filter" in stderr

    def test_malformed_hajer_metadata_stops_the_run_before_any_provider_call(
        self, settings: HajerSettings, engine: EngineReady, tmp_path: Path
    ) -> None:
        suite = tmp_path / "suite"
        shutil.copytree(EXAMPLE, suite)
        config = suite / "promptfooconfig.yaml"
        source = config.read_text()
        block = "      hajer:\n        obligationIds: [obl_refund_status_disclosed]\n        componentIds: [cmp_refund_agent]\n"
        assert source.count(block) == 2, "the example's first two tests declare the same hajer block"
        config.write_text(source.replace(block, block.replace("hajer:\n", "hajer:\n        workflowId: 123\n"), 1))
        code, _stdout, stderr, _payload = run(["--no-cache"], settings, cwd=suite)
        assert code == EXIT_USAGE
        assert "E_HAJER_INVALID" in stderr
        assert "workflowId" in stderr

    def test_a_failing_case_exits_with_the_engines_code_and_still_yields_a_payload(
        self, settings: HajerSettings, engine: EngineReady
    ) -> None:
        code, _stdout, _stderr, payload = run(["-c", "failing.yaml", "--no-cache"], settings)
        assert code == ENGINE_FAILED_TESTS
        assert payload is not None
        assert payload["status"] == "failed"
        assert payload["engineExitCode"] == ENGINE_FAILED_TESTS
        (row,) = results_by_id(payload).values()
        assert row["outcome"] == "failed"
        assert any(not a["passed"] for a in cast(list[JsonObject], row["assertions"]))


class TestUploadAndEgress:
    def test_upload_posts_once_with_the_run_id_as_idempotency_key_and_a_dead_server_leaves_the_code_alone(
        self, settings: HajerSettings, engine: EngineReady
    ) -> None:
        keyed = HajerSettings(
            cache_dir=settings.cache_dir,
            api_key="k",
            team_id="team-1",
            project_id="proj-1",
            base_url="https://hajer.test",
            observe_backoff_initial_ms=1,
            observe_backoff_max_ms=2,
        )
        seen: list[httpx.Request] = []

        def stored(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(201, json={})

        code, stdout, _stderr, payload = run(["--upload", "--no-cache"], keyed, transport=httpx.MockTransport(stored))
        assert code == 0
        assert payload is not None
        (request,) = seen
        assert request.url.path == "/api/teams/team-1/projects/proj-1/eval-runs"
        assert request.headers["Idempotency-Key"] == payload["runId"]
        assert request.headers["Authorization"] == "Bearer k"
        assert json.loads(request.content)["runId"] == payload["runId"]
        assert "upload uploaded" in stdout

        def dead(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        code, _stdout, stderr, _payload = run(["--upload", "--no-cache"], keyed, transport=httpx.MockTransport(dead))
        assert code == 0, "a green eval with a failed upload is a green eval"
        assert "upload failed" in stderr

    def test_the_engine_contacts_nothing_but_loopback(self, settings: HajerSettings, engine: EngineReady) -> None:
        """Every non-loopback request the engine would make is forced through a recording proxy that refuses it.

        What the proxy records must be within the documented set, and refusing it must not touch the result."""
        hosts: list[str] = []

        class Refuse(BaseHTTPRequestHandler):
            def _record(self) -> None:
                hosts.append(f"{self.command} {self.headers.get('Host', '')} {self.path}")
                self.send_response(403)
                self.end_headers()

            do_GET = do_POST = do_PUT = do_HEAD = do_CONNECT = _record  # noqa: N815 - http.server's own method names

            def log_message(self, format: str, *args: object) -> None:
                del format, args

        server = ThreadingHTTPServer(("127.0.0.1", 0), Refuse)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:

            def proxied(settings: HajerSettings, **kwargs: object) -> dict[str, str]:
                environment = eval_engine_environment(settings, **kwargs)  # pyright: ignore[reportArgumentType] - the CLI's own keywords
                environment.update(
                    {
                        "NODE_USE_ENV_PROXY": "1",
                        "HTTP_PROXY": f"http://127.0.0.1:{port}",
                        "HTTPS_PROXY": f"http://127.0.0.1:{port}",
                        "NO_PROXY": "127.0.0.1,localhost",
                    }
                )
                return environment

            code, _stdout, _stderr, payload = run(["--no-cache"], settings, engine_environment=proxied)
        finally:
            server.shutdown()
            server.server_close()
        assert code == 0
        assert payload is not None
        contacted = {line.split()[1].rsplit(":", 1)[0] for line in hosts}  # the Host header carries the port
        assert contacted <= KNOWN_ENGINE_EGRESS, f"the engine reached beyond the documented hosts: {hosts}"
        assert all(line.startswith("CONNECT ") for line in hosts), "every attempt was a refused tunnel, not a request"
