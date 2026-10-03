"""CI reruns the actual published predicates; missing or ineffective controls never pass."""

import json
from pathlib import Path

import pytest
from pydantic import JsonValue

from hajer._controls import main, verify_controls


def document(
    *, op: str = "in", values: str = '["accepted", "rejected"]', negative: bool = True
) -> dict[str, JsonValue]:
    return {
        "suiteId": "generated",
        "members": [
            {
                "check": {
                    "checkDraftDigest": "predicate",
                    "draft": {
                        "expression": {
                            "op": op,
                            "path": ["output", "label"],
                            "valueJson": values,
                        },
                        "requiredPaths": [["output", "label"]],
                    },
                }
            }
        ],
        "requirements": [
            {
                "check": {"digest": "predicate"},
                "controls": {
                    "discriminates": True,
                    "positive": [{"controls": [{"documentJson": '{"output":{"label":"accepted"}}'}]}],
                    "negative": [{"controls": [{"documentJson": '{"output":{"label":"invented"}}'}]}]
                    if negative
                    else [],
                },
            }
        ],
    }


def test_real_predicate_rejects_bad_output_and_accepts_valid_output(tmp_path: Path) -> None:
    path = tmp_path / "suite.json"
    path.write_text(json.dumps(document()))
    assert verify_controls(path)["status"] == "VERIFIED"
    assert verify_controls(path)["applicationExecuted"] is False


def test_loosened_predicate_cannot_pass_its_retained_negative_control(tmp_path: Path) -> None:
    value = document(values='["accepted", "rejected", "invented"]')
    path = tmp_path / "suite.json"
    path.write_text(json.dumps(value))
    assert verify_controls(path)["status"] == "UNVERIFIED"


def test_missing_negative_controls_and_empty_suites_never_pass(tmp_path: Path) -> None:
    value = document(negative=False)
    path = tmp_path / "suite.json"
    path.write_text(json.dumps(value))
    assert verify_controls(path)["status"] == "UNVERIFIED"
    path.write_text('{"suiteId":"empty","members":[]}')
    assert verify_controls(path)["status"] == "UNVERIFIED"
    assert main(["--suites", str(tmp_path / "absent"), "--results", str(tmp_path / "report.json")]) == 1


def test_unsupported_predicate_is_not_a_successful_negative_control(tmp_path: Path) -> None:
    value = document(op="unsupported")
    path = tmp_path / "suite.json"
    path.write_text(json.dumps(value))
    assert verify_controls(path)["status"] == "UNVERIFIED"


@pytest.mark.parametrize("extra", [{"check": {"kind": "model"}}, None, {"check": {"checkDraftDigest": ""}}])
def test_unidentified_member_cannot_hide_behind_verified_checks(tmp_path: Path, extra: JsonValue) -> None:
    value = document()
    members = value["members"]
    assert isinstance(members, list)
    members.append(extra)
    path = tmp_path / "suite.json"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match=r"Published (check|member)"):
        verify_controls(path)
    assert main(["--suites", str(tmp_path), "--results", str(tmp_path / "results" / "report.json")]) == 1


def advisory() -> dict[str, JsonValue]:
    return {
        "check": {
            "check": {"id": "hajer-judge:policy:one", "evaluator": "SCOPED_SEMANTIC_JUDGE"},
            "judge": {"quote": "Be polite", "promptVersion": "suite-judge-v1"},
            "liveOnly": True,
        }
    }


def test_advisory_controls_are_not_claimed_as_verified_or_a_reason_to_skip_code(tmp_path: Path) -> None:
    value = document()
    members = value["members"]
    assert isinstance(members, list)
    members.extend([advisory(), advisory()])
    path = tmp_path / "suite.json"
    path.write_text(json.dumps(value))
    report = verify_controls(path)
    assert report["status"] == "CODE_CONTROLS_VERIFIED"
    assert report["verifiedChecks"] == 1
    assert report["totalChecks"] == 2
    assert report["advisoryChecksNotReexecuted"] == 1
    assert main(["--suites", str(tmp_path), "--results", str(tmp_path / "results" / "report.json")]) == 0

    value["members"] = [advisory()]
    path.write_text(json.dumps(value))
    assert verify_controls(path)["status"] == "NOT_REEXECUTED"
    assert verify_controls(path)["verifiedChecks"] == 0


def test_advisory_check_cannot_hide_a_broken_code_control(tmp_path: Path) -> None:
    value = document(values='["accepted", "rejected", "invented"]')
    members = value["members"]
    assert isinstance(members, list)
    members.append(advisory())
    path = tmp_path / "suite.json"
    path.write_text(json.dumps(value))
    assert verify_controls(path)["status"] == "UNVERIFIED"
    assert main(["--suites", str(tmp_path), "--results", str(tmp_path / "results" / "report.json")]) == 1


def test_strengthened_code_without_retained_controls_is_unverified(tmp_path: Path) -> None:
    value = document()
    members = value["members"]
    assert isinstance(members, list)
    members.append({"check": {"check": {"id": "schema", "evaluator": "REVIEWED_PREDICATE"}, "draft": {}}})
    path = tmp_path / "suite.json"
    path.write_text(json.dumps(value))
    report = verify_controls(path)
    assert report["status"] == "UNVERIFIED"
    assert report["verifiedChecks"] == 1
    assert report["totalChecks"] == 2


def test_the_report_names_each_verified_code_check_and_each_model_check_awaiting_a_live_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A customer's CI printed "Hajer check controls: verified" with nothing to say that the model-graded checks were not
    among them. The report and the printed line now name both sets, by check identity."""
    value = document()
    members = value["members"]
    assert isinstance(members, list)
    members.append(advisory())
    path = tmp_path / "suite.json"
    path.write_text(json.dumps(value))
    report = verify_controls(path)
    assert report["codeChecksVerified"] == ["predicate"]
    assert report["codeChecksUnverified"] == []
    assert report["modelChecksNeedingLiveEvaluation"] == ["hajer-judge:policy:one"]
    assert report["modelChecksUnreadable"] == []
    assert main(["--suites", str(tmp_path), "--results", str(tmp_path / "results" / "report.json")]) == 0
    printed = capsys.readouterr().out
    assert (
        "- generated: CODE_CONTROLS_VERIFIED. Code checks verified against their retained controls: 1 (predicate); "
        "code checks not verified: 0; model-graded checks that still need a live evaluation: 1 "
        "(hajer-judge:policy:one).\n"
    ) in printed
    assert "Model-graded checks are not re-executed offline and are not verified here." in printed
    written = json.loads((tmp_path / "results" / "report.json").read_text())
    assert written["suites"][0]["modelChecksNeedingLiveEvaluation"] == ["hajer-judge:policy:one"]


def test_a_broken_code_check_and_an_unreadable_model_check_are_each_named_in_their_own_set(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    value = document(values='["accepted", "rejected", "invented"]')
    members = value["members"]
    assert isinstance(members, list)
    unreadable = advisory()
    graded = unreadable["check"]
    assert isinstance(graded, dict)
    graded.pop("judge")
    members.append(unreadable)
    path = tmp_path / "suite.json"
    path.write_text(json.dumps(value))
    report = verify_controls(path)
    assert report["status"] == "UNVERIFIED"
    assert (report["codeChecksVerified"], report["codeChecksUnverified"]) == ([], ["predicate"])
    assert (report["modelChecksNeedingLiveEvaluation"], report["modelChecksUnreadable"]) == (
        [],
        ["hajer-judge:policy:one"],
    )
    (tmp_path / "broken.json").write_text("{")
    assert main(["--suites", str(tmp_path), "--results", str(tmp_path / "results" / "report.json")]) == 1
    printed = capsys.readouterr().out
    assert "code checks not verified: 1 (predicate)" in printed
    assert "model-graded checks unreadable: 1 (hajer-judge:policy:one)" in printed
    assert "- broken.json: unverified (CONTROL_DOCUMENT_UNREADABLE)." in printed


def _member(check: dict[str, JsonValue]) -> JsonValue:
    return {"check": check}


def test_one_predicate_prepared_for_two_obligations_is_one_control_identity(tmp_path: Path) -> None:
    """A classifier's v2 suite carried the same draft under two obligations; only provenance differed."""
    value = document()
    members = value["members"]
    assert isinstance(members, list)
    first = members[0]
    assert isinstance(first, dict)
    check = first["check"]
    assert isinstance(check, dict)
    value["members"] = [
        _member({**check, "obligation": {"id": "obligation-v1"}, "producer": {"recordRowId": "a"}}),
        _member({**check, "obligation": {"id": "obligation-v2"}, "producer": {"recordRowId": "b"}}),
    ]
    path = tmp_path / "suite.json"
    path.write_text(json.dumps(value))
    report = verify_controls(path)
    assert report["status"] == "VERIFIED"
    assert report["codeChecksVerified"] == ["predicate"]
    draft = check["draft"]
    assert isinstance(draft, dict)
    loosened: dict[str, JsonValue] = {**draft, "expression": {"op": "exists", "path": ["output", "label"]}}
    changed: dict[str, JsonValue] = {**check, "draft": loosened}
    value["members"] = [_member(check), _member(changed)]
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="Conflicting published check definitions"):
        verify_controls(path)


def test_controls_retained_outside_a_requirement_verify_a_seed_and_a_strong_check(tmp_path: Path) -> None:
    """A schema seed's and a strong check's controls are read from `checkControls`."""
    schema = json.dumps({"type": "object", "properties": {"limit": {"type": "integer"}}, "required": ["limit"]})
    draft: JsonValue = {
        "expression": {"op": "schema_valid", "path": ["output"], "valueJson": schema},
        "requiredPaths": [["output"]],
    }
    controls: JsonValue = {
        "discriminates": True,
        "positive": [{"controls": [{"documentJson": '{"output":{"limit":1}}'}]}],
        "negative": [{"controls": [{"documentJson": '{"output":{}}'}]}],
    }
    value: dict[str, JsonValue] = {
        "suiteId": "seeded",
        "members": [
            _member({"checkDraftDigest": "sha256:seed", "draft": draft}),
            _member({"check": {"id": "hajer-predicate:schema:1", "evaluator": "REVIEWED_PREDICATE"}, "draft": draft}),
        ],
        "requirements": [],
        "checkControls": [
            {"digest": "sha256:seed", "controls": controls},
            {"digest": "hajer-predicate:schema:1", "controls": controls},
        ],
    }
    path = tmp_path / "suite.json"
    path.write_text(json.dumps(value))
    report = verify_controls(path)
    assert report["status"] == "VERIFIED"
    assert report["codeChecksVerified"] == ["sha256:seed", "hajer-predicate:schema:1"]
    value["checkControls"] = []
    path.write_text(json.dumps(value))
    assert verify_controls(path)["status"] == "UNVERIFIED"  # no retained control is never verified


def test_a_control_kept_as_the_application_result_observation_is_read_as_the_output(tmp_path: Path) -> None:
    """A schema seed that reads `evidence.application_result`; its controls are kept in that form."""
    schema = json.dumps({"type": "object", "properties": {"limit": {"type": "integer"}}, "required": ["limit"]})
    path_: list[JsonValue] = ["evidence", "application_result"]
    draft: JsonValue = {
        "expression": {"op": "schema_valid", "path": path_, "valueJson": schema},
        "requiredPaths": [path_],
    }
    controls: JsonValue = {
        "discriminates": True,
        "positive": [{"controls": [{"documentJson": '{"evidence":{"application_result":{"limit":1}}}'}]}],
        "negative": [{"controls": [{"documentJson": '{"evidence":{"application_result":{}}}'}]}],
    }
    value: dict[str, JsonValue] = {
        "suiteId": "seeded",
        "members": [_member({"checkDraftDigest": "sha256:seed", "draft": draft})],
        "checkControls": [{"digest": "sha256:seed", "controls": controls}],
    }
    path = tmp_path / "suite.json"
    path.write_text(json.dumps(value))
    assert verify_controls(path)["status"] == "VERIFIED"
