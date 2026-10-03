"""Canonical JSON types, evidence requirements and array paths must survive CI projection."""

import json

import pytest
from pydantic import JsonValue

from hajer.pytest_plugin._evaluate import evaluate


@pytest.mark.parametrize(("score", "verdict"), [(0.8, "PASS"), (2.0, "FAIL")])
def test_declared_schema_checks_read_application_result_evidence(score: float, verdict: str) -> None:
    check: dict[str, JsonValue] = {
        "draft": {
            "requiredPaths": [["evidence", "application_result"]],
            "expression": {
                "op": "schema_valid",
                "path": ["evidence", "application_result"],
                "valueJson": json.dumps(
                    {
                        "type": "object",
                        "properties": {"score": {"type": "number", "minimum": 0, "maximum": 1}},
                        "required": ["score"],
                    }
                ),
            },
        }
    }
    assert evaluate(check, {"score": score}) == verdict


def test_raw_tool_call_projection_does_not_fabricate_application_result_evidence() -> None:
    check: dict[str, JsonValue] = {
        "draft": {
            "requiredPaths": [["evidence", "application_result"]],
            "expression": {"op": "exists", "path": ["output", "tool_calls", "Answer"]},
        }
    }
    assert evaluate(check, {"tool_calls": {"Answer": {"arguments": {"score": 0.8}}}}) == "UNABLE_TO_VERIFY"


@pytest.mark.parametrize(
    ("actual", "expected", "verdict"),
    [
        ({"allowed": 1}, {"allowed": True}, "FAIL"),
        ({"nested": [{"allowed": 1}]}, {"nested": [{"allowed": True}]}, "FAIL"),
        ([False], [0], "FAIL"),
        ({"count": 1}, {"count": 1.0}, "FAIL"),
        ({"nested": [{"allowed": True, "count": 1}]}, {"nested": [{"count": 1, "allowed": True}]}, "PASS"),
        ([True], [True, True], "FAIL"),
    ],
)
def test_equality_preserves_json_types_recursively(actual: JsonValue, expected: JsonValue, verdict: str) -> None:
    check: dict[str, JsonValue] = {
        "draft": {"expression": {"op": "eq", "path": ["output"], "valueJson": json.dumps(expected)}}
    }
    assert evaluate(check, actual) == verdict


@pytest.mark.parametrize(
    ("requirements", "output", "verdict"),
    [
        ([["output", "evidence"]], {"answer": "yes"}, "UNABLE_TO_VERIFY"),
        ([["output", "evidence"]], {"answer": "yes", "evidence": None}, "PASS"),
        ([["request", "evidence"]], {"answer": "yes"}, "UNABLE_TO_VERIFY"),
        ([["output", "evidence", "0"]], {"answer": "yes", "evidence": [None]}, "PASS"),
        ([["output", "evidence", "1"]], {"answer": "yes", "evidence": [None]}, "UNABLE_TO_VERIFY"),
        (["output.evidence"], {"answer": "yes", "evidence": True}, "UNABLE_TO_VERIFY"),
    ],
)
def test_required_paths_gate_an_otherwise_passing_check(
    requirements: JsonValue, output: JsonValue, verdict: str
) -> None:
    check: dict[str, JsonValue] = {
        "draft": {
            "requiredPaths": requirements,
            "expression": {"op": "eq", "path": ["output", "answer"], "valueJson": '"yes"'},
        }
    }
    assert evaluate(check, output) == verdict


@pytest.mark.parametrize(
    ("output", "path", "verdict"),
    [
        (["present"], ["output", "0"], "PASS"),
        ([None], ["output", "0"], "PASS"),
        ([{"value": "present"}], ["output", "0", "value"], "PASS"),
        (["present"], ["output", "1"], "FAIL"),
        (["present"], ["output", "00"], "FAIL"),
        (["present"], ["output", "-1"], "FAIL"),
        ({"0": "present"}, ["output", "0"], "PASS"),
    ],
)
def test_exists_supports_canonical_decimal_array_paths(output: JsonValue, path: list[str], verdict: str) -> None:
    check: dict[str, JsonValue] = {"draft": {"expression": {"op": "exists", "path": list(path)}}}
    assert evaluate(check, output) == verdict
