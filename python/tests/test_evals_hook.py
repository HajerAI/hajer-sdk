"""The `beforeAll` hook in process: the whole context comes back, the suite is narrowed, the report says what happened."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from hajer import HajerSettings
from hajer._errors import EvalMetadataError
from hajer._json import JsonObject, JsonValue
from hajer.evals import _hook_entry
from hajer.evals._hook import HOOK_REPORT_SCHEMA_VERSION, WARNING_PREFIX, before_all, format_warnings, read_report
from hajer.evals._metadata import E_HAJER_INVALID, E_OBLIGATION_UNDECLARED, W_NO_TEST_CASE_ID

PROMPTS: list[JsonValue] = [{"raw": "Answer {{question}}", "label": "support"}]
PROVIDERS: list[JsonValue] = [{"id": "file://provider.py", "label": "app"}]


def _test(
    hajer: JsonValue = None, *, with_hajer: bool = True, test_case_id: str | None, description: str
) -> JsonObject:
    metadata: JsonObject = {"owner": "support-team"}
    if test_case_id is not None:
        metadata["testCaseId"] = test_case_id
    if with_hajer:
        metadata["hajer"] = hajer
    return {"description": description, "vars": {"question": description}, "metadata": metadata}


def _suite(tests: list[JsonValue], default_test: JsonObject | None = None) -> JsonObject:
    suite: JsonObject = {
        "prompts": PROMPTS,
        "providers": PROVIDERS,
        "tests": tests,
        "nunjucksFilters": {},
        "derivedMetrics": [{"name": "pass_rate", "value": "passed / total"}],
    }
    if default_test is not None:
        suite["defaultTest"] = default_test
    return {"suite": suite, "extra": "kept"}


def _settings(
    tmp_path: Path,
    *,
    eval_workflow: str | None = None,
    eval_obligations: str | None = None,
    eval_manifest: str | None = None,
    eval_manifest_obligations: str | None = None,
) -> HajerSettings:
    return HajerSettings(
        eval_hook_report=str(tmp_path / "r.json"),
        eval_run_id="run-1",
        eval_workflow=eval_workflow,
        eval_obligations=eval_obligations,
        eval_manifest=eval_manifest,
        eval_manifest_obligations=eval_manifest_obligations,
    )


def _report(tmp_path: Path) -> JsonObject:
    report = read_report(tmp_path / "r.json")
    assert report is not None
    return report


def _tests_of(result: JsonObject) -> list[JsonValue]:
    suite = result["suite"]
    assert isinstance(suite, dict)
    tests = suite["tests"]
    assert isinstance(tests, list)
    return tests


def _descriptions(result: JsonObject) -> list[str]:
    out: list[str] = []
    for test in _tests_of(result):
        assert isinstance(test, dict)
        description = test["description"]
        assert isinstance(description, str)
        out.append(description)
    return out


MIXED: list[JsonValue] = [
    _test(
        {"workflowId": "wf_support", "obligationIds": ["ob_a", "ob_b"]},
        test_case_id="tc-1",
        description="refund status",
    ),
    _test({"workflowId": "wf_support", "obligationIds": ["ob_c"]}, test_case_id="tc-2", description="late refund"),
    _test({"workflowId": "wf_sales"}, test_case_id="tc-3", description="upsell"),
    _test(with_hajer=False, test_case_id=None, description="plain promptfoo test"),
]


class TestTheContextComesBackWhole:
    def test_every_other_suite_key_and_every_other_context_key_is_kept(self, tmp_path: Path) -> None:
        context = _suite(list(MIXED))
        result = before_all(context, settings=_settings(tmp_path))
        suite = result["suite"]
        assert isinstance(suite, dict)
        assert suite["prompts"] == PROMPTS
        assert suite["providers"] == PROVIDERS
        assert suite["nunjucksFilters"] == {}
        assert suite["derivedMetrics"] == [{"name": "pass_rate", "value": "passed / total"}]
        assert result["extra"] == "kept"
        assert set(result) == set(context)
        assert set(suite) == {"prompts", "providers", "tests", "nunjucksFilters", "derivedMetrics"}

    def test_no_filter_keeps_every_test_including_plain_and_non_object_ones(self, tmp_path: Path) -> None:
        tests: list[JsonValue] = [*MIXED, "not even an object"]
        result = before_all(_suite(tests), settings=_settings(tmp_path))
        kept = _tests_of(result)
        assert len(kept) == 5
        assert kept[4] == "not even an object"
        report = _report(tmp_path)
        entries = report["tests"]
        assert isinstance(entries, list)
        assert len(entries) == 5
        last = entries[4]
        assert isinstance(last, dict)
        assert last["correlation"] == "none"
        assert last["label"] == "#4"

    def test_the_merged_hajer_object_is_written_back_and_other_metadata_keys_stay(self, tmp_path: Path) -> None:
        default_test: JsonObject = {"metadata": {"hajer": {"workflowId": "wf_support", "generatedBy": "gen"}}}
        tests: list[JsonValue] = [
            _test({"obligationIds": ["ob_a"]}, test_case_id="tc-1", description="inherits the workflow"),
            _test(with_hajer=False, test_case_id="tc-2", description="inherits everything"),
            _test(with_hajer=False, test_case_id=None, description="plain"),
        ]
        result = before_all(_suite(tests, default_test), settings=_settings(tmp_path))
        kept = _tests_of(result)
        first, second, third = kept
        assert isinstance(first, dict)
        assert isinstance(second, dict)
        assert isinstance(third, dict)
        assert first["metadata"] == {
            "owner": "support-team",
            "testCaseId": "tc-1",
            "hajer": {"workflowId": "wf_support", "generatedBy": "gen", "obligationIds": ["ob_a"]},
        }
        assert second["metadata"] == {
            "owner": "support-team",
            "testCaseId": "tc-2",
            "hajer": {"workflowId": "wf_support", "generatedBy": "gen"},
        }
        # Inheriting makes the plain test a platform test too: it now carries the object promptfoo would have given it.
        assert third["metadata"] == {
            "owner": "support-team",
            "hajer": {"workflowId": "wf_support", "generatedBy": "gen"},
        }
        assert first["vars"] == {"question": "inherits the workflow"}

    def test_the_input_is_not_mutated(self, tmp_path: Path) -> None:
        tests: list[JsonValue] = [_test({"obligationIds": ["ob_a"]}, test_case_id="tc-1", description="d")]
        context = _suite(tests, {"metadata": {"hajer": {"workflowId": "wf_support"}}})
        snapshot = json.dumps(context, sort_keys=True)
        before_all(context, settings=_settings(tmp_path))
        assert json.dumps(context, sort_keys=True) == snapshot

    def test_a_context_without_a_suite_is_returned_untouched(self, tmp_path: Path) -> None:
        contexts: tuple[JsonObject, ...] = (
            {},
            {"suite": "not an object"},
            {"suite": {"prompts": PROMPTS, "tests": "not a list"}},
        )
        for context in contexts:
            assert before_all(context, settings=_settings(tmp_path)) is context
        assert read_report(tmp_path / "r.json") is None


class TestErrors:
    def test_a_malformed_test_aborts_with_the_test_named_and_the_report_written(self, tmp_path: Path) -> None:
        tests: list[JsonValue] = [
            _test({"workflowId": "wf_support"}, test_case_id="tc-1", description="fine"),
            _test({"workflowID": "wf_support"}, test_case_id=None, description="refund status, unknown order"),
        ]
        with pytest.raises(EvalMetadataError) as raised:
            before_all(_suite(tests), settings=_settings(tmp_path, eval_workflow="wf_support"))
        message = str(raised.value)
        assert message.startswith("HAJER_INVALID_EVAL_METADATA: ")
        assert f'[{E_HAJER_INVALID}] test #1 "refund status, unknown order": metadata.hajer.workflowID' in message
        assert len(raised.value.errors) == 2
        report = _report(tmp_path)
        assert report["errors"] == list(raised.value.errors)
        assert report["filtered"] == {"before": 2, "after": 2, "workflowId": "wf_support", "obligationIds": []}

    def test_an_unwritable_report_path_does_not_change_the_outcome(self, tmp_path: Path) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("a file where the directory should be", encoding="utf-8")
        settings = HajerSettings(eval_hook_report=str(blocker / "r.json"))
        result = before_all(_suite(list(MIXED)), settings=settings)
        assert len(_tests_of(result)) == 4
        with pytest.raises(EvalMetadataError):
            before_all(_suite([_test(5, test_case_id="tc", description="d")]), settings=settings)

    def test_no_report_path_means_no_report_and_no_complaint(self, tmp_path: Path) -> None:
        result = before_all(_suite(list(MIXED)), settings=HajerSettings())
        assert len(_tests_of(result)) == 4
        assert list(tmp_path.iterdir()) == []


class TestFilters:
    def test_workflow_keeps_only_its_tests_and_drops_plain_ones(self, tmp_path: Path) -> None:
        result = before_all(_suite(list(MIXED)), settings=_settings(tmp_path, eval_workflow="wf_support"))
        assert _descriptions(result) == ["refund status", "late refund"]
        assert _report(tmp_path)["filtered"] == {
            "before": 4,
            "after": 2,
            "workflowId": "wf_support",
            "obligationIds": [],
        }

    def test_obligations_keep_any_test_that_intersects(self, tmp_path: Path) -> None:
        result = before_all(_suite(list(MIXED)), settings=_settings(tmp_path, eval_obligations="ob_b, ob_c,"))
        assert _descriptions(result) == ["refund status", "late refund"]
        assert _report(tmp_path)["filtered"] == {
            "before": 4,
            "after": 2,
            "workflowId": None,
            "obligationIds": ["ob_b", "ob_c"],
        }

    def test_both_filters_must_hold(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path, eval_workflow="wf_support", eval_obligations="ob_c")
        assert _descriptions(before_all(_suite(list(MIXED)), settings=settings)) == ["late refund"]

    def test_a_filter_nothing_matches_aborts_rather_than_running_an_empty_suite(self, tmp_path: Path) -> None:
        with pytest.raises(EvalMetadataError) as raised:
            before_all(_suite(list(MIXED)), settings=_settings(tmp_path, eval_workflow="wf_billing"))
        assert raised.value.errors == (
            "no test matches the filter (workflowId=wf_billing): 4 tests in the suite, 3 with metadata.hajer",
        )
        assert _report(tmp_path)["filtered"] == {
            "before": 4,
            "after": 0,
            "workflowId": "wf_billing",
            "obligationIds": [],
        }

    def test_the_filter_description_names_both_switches(self, tmp_path: Path) -> None:
        with pytest.raises(EvalMetadataError) as raised:
            before_all(
                _suite(list(MIXED)), settings=_settings(tmp_path, eval_workflow="wf_sales", eval_obligations="ob_a")
            )
        assert "(workflowId=wf_sales; obligationIds=ob_a)" in str(raised.value)


class TestTheReport:
    def test_the_report_is_camel_case_and_complete(self, tmp_path: Path) -> None:
        tests: list[JsonValue] = [
            _test(
                {"workflowId": "wf_support", "obligationIds": ["ob_a"], "componentIds": ["cmp_x"]},
                test_case_id="tc-1",
                description="refund",
            ),
            _test({"workflowId": "wf_support"}, test_case_id=None, description="refund status, unknown order"),
            _test(with_hajer=False, test_case_id=None, description="plain"),
        ]
        before_all(_suite(tests), settings=_settings(tmp_path))
        report = _report(tmp_path)
        assert report["schemaVersion"] == HOOK_REPORT_SCHEMA_VERSION == 1
        assert report["runId"] == "run-1"
        assert report["errors"] == []
        assert report["tests"] == [
            {
                "index": 0,
                "label": "tc-1",
                "testCaseId": "tc-1",
                "correlation": "platform",
                "workflowId": "wf_support",
                "obligationIds": ["ob_a"],
                "componentIds": ["cmp_x"],
                "warnings": [],
            },
            {
                "index": 1,
                "label": "refund status, unknown order",
                "testCaseId": None,
                "correlation": "platform",
                "workflowId": "wf_support",
                "obligationIds": None,
                "componentIds": None,
                "warnings": [
                    f'[{W_NO_TEST_CASE_ID}] test #1 "refund status, unknown order": no metadata.testCaseId; correlation '
                    "falls back to the test's position, which moves when tests are reordered; add metadata.testCaseId"
                ],
            },
            {
                "index": 2,
                "label": "plain",
                "testCaseId": None,
                "correlation": "none",
                "workflowId": None,
                "obligationIds": None,
                "componentIds": None,
                "warnings": [],
            },
        ]
        warnings = report["warnings"]
        assert isinstance(warnings, list)
        assert len(warnings) == 1
        assert report["filtered"] == {"before": 3, "after": 3, "workflowId": None, "obligationIds": []}
        assert set(report) == {"schemaVersion", "runId", "tests", "filtered", "warnings", "errors"}

    def test_format_warnings_names_the_row_the_test_and_the_workflow(self, tmp_path: Path) -> None:
        tests: list[JsonValue] = [
            _test({"workflowId": "wf_support"}, test_case_id="tc-1", description="fine"),
            _test({"workflowId": "wf_support"}, test_case_id=None, description="refund status, unknown order"),
            _test({"workflowId": "wf_sales"}, test_case_id=None, description=""),
        ]
        before_all(_suite(tests), settings=_settings(tmp_path))
        lines = format_warnings(_report(tmp_path))
        assert lines == (
            f'{WARNING_PREFIX} [{W_NO_TEST_CASE_ID}] test #1 "refund status, unknown order" (workflowId=wf_support): '
            "no metadata.testCaseId; correlation falls back to the test's position, which moves when tests are "
            "reordered; add metadata.testCaseId",
            f"{WARNING_PREFIX} [{W_NO_TEST_CASE_ID}] test #2 (workflowId=wf_sales): no metadata.testCaseId; correlation "
            "falls back to the test's position, which moves when tests are reordered; add metadata.testCaseId",
        )

    def test_format_warnings_tolerates_a_report_it_does_not_recognise(self) -> None:
        assert format_warnings({}) == ()
        assert format_warnings({"tests": "nope"}) == ()
        assert format_warnings({"tests": [1, {"warnings": "nope"}, {"warnings": ["[X] loose line", 3]}]}) == (
            f"{WARNING_PREFIX} [X] loose line",
        )

    def test_read_report_is_none_for_a_missing_or_garbled_file(self, tmp_path: Path) -> None:
        assert read_report(tmp_path / "missing.json") is None
        garbled = tmp_path / "garbled.json"
        garbled.write_text("{not json", encoding="utf-8")
        assert read_report(garbled) is None
        not_an_object = tmp_path / "list.json"
        not_an_object.write_text("[1, 2]", encoding="utf-8")
        assert read_report(not_an_object) is None


class TestTheEntryPoint:
    def test_before_all_reads_its_settings_from_the_engines_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HAJER_EVAL_HOOK_REPORT", str(tmp_path / "r.json"))
        monkeypatch.setenv("HAJER_EVAL_WORKFLOW", "wf_sales")
        monkeypatch.setenv("HAJER_EVAL_RUN_ID", "run-from-env")
        monkeypatch.delenv("HAJER_EVAL_OBLIGATIONS", raising=False)
        result = _hook_entry.beforeAll(_suite(list(MIXED)), {"hookName": "beforeAll"})
        assert _descriptions(result) == ["upsell"]
        report = _report(tmp_path)
        assert report["runId"] == "run-from-env"
        assert report["filtered"] == {"before": 4, "after": 1, "workflowId": "wf_sales", "obligationIds": []}

    def test_the_options_argument_is_optional(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HAJER_EVAL_HOOK_REPORT", str(tmp_path / "r.json"))
        monkeypatch.delenv("HAJER_EVAL_WORKFLOW", raising=False)
        monkeypatch.delenv("HAJER_EVAL_OBLIGATIONS", raising=False)
        assert len(_tests_of(_hook_entry.beforeAll(_suite(list(MIXED))))) == 4

    def test_the_file_works_the_way_the_engines_wrapper_loads_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """promptfoo's wrapper imports the file by path under its basename, looks the hook up by name, JSON-encodes the result."""
        monkeypatch.setenv("HAJER_EVAL_HOOK_REPORT", str(tmp_path / "r.json"))
        monkeypatch.setenv("HAJER_EVAL_WORKFLOW", "wf_support")
        monkeypatch.delenv("HAJER_EVAL_OBLIGATIONS", raising=False)
        path = Path(_hook_entry.__file__)
        spec = importlib.util.spec_from_file_location(path.stem, path)
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        hook = getattr(module, "beforeAll", None)
        assert callable(hook)
        context = json.loads(json.dumps(_suite(list(MIXED))))
        result = hook(context, {"hookName": "beforeAll"})
        encoded = json.dumps({"type": "final_result", "data": result}, ensure_ascii=False)
        assert _descriptions(json.loads(encoded)["data"]) == ["refund status", "late refund"]


class TestTheManifest:
    """With a manifest in force, an obligation a test names must be one the manifest declares."""

    def _suite(self) -> JsonObject:
        return _suite(
            [
                _test(
                    {"workflowId": "wf", "obligationIds": ["obl_a", "obl_nope"]}, test_case_id="t1", description="one"
                ),
                _test({"workflowId": "wf", "obligationIds": ["obl_a"]}, test_case_id="t2", description="two"),
            ]
        )

    def test_an_undeclared_obligation_stops_the_run_and_names_the_test(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path, eval_manifest="/repo/hajer.yaml", eval_manifest_obligations="obl_a,obl_b")
        with pytest.raises(EvalMetadataError) as stopped:
            before_all(self._suite(), settings=settings)
        (error,) = stopped.value.errors
        assert error.startswith(
            f"[{E_OBLIGATION_UNDECLARED}] test #0 \"t1\": metadata.hajer.obligationIds.1 'obl_nope'"
        )
        assert "hajer.yaml" in error
        assert "declared: obl_a, obl_b" in error
        report = _report(tmp_path)
        assert report["errors"] == [error]

    def test_without_a_manifest_nothing_is_checked(self, tmp_path: Path) -> None:
        context = before_all(self._suite(), settings=_settings(tmp_path))
        suite = context["suite"]
        assert isinstance(suite, dict)
        tests = suite["tests"]
        assert isinstance(tests, list)
        assert len(tests) == 2

    def test_a_manifest_that_declares_no_obligation_refuses_any(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path, eval_manifest="/repo/hajer.yaml", eval_manifest_obligations="")
        with pytest.raises(EvalMetadataError) as stopped:
            before_all(self._suite(), settings=settings)
        assert len(stopped.value.errors) == 3, (
            "every named obligation is one error: two on the first test, one on the second"
        )
        assert all("(it declares none)" in error for error in stopped.value.errors)

    def test_a_declared_obligation_passes(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path, eval_manifest="/repo/hajer.yaml", eval_manifest_obligations="obl_a,obl_nope")
        context = before_all(self._suite(), settings=settings)
        assert isinstance(context["suite"], dict)
        assert _report(tmp_path)["errors"] == []
