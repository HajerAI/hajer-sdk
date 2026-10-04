"""The `metadata.hajer` convention: strict on shape, a warning on a missing stable id, deep-merged from `defaultTest`."""

from __future__ import annotations

from hajer._json import JsonObject, JsonValue
from hajer.evals._metadata import (
    ALLOWED_KEYS,
    E_HAJER_INVALID,
    E_HAJER_NOT_OBJECT,
    ID_MAX_CHARS,
    W_NO_TEST_CASE_ID,
    HajerTestMetadata,
    classify_test,
    merged_hajer_metadata,
)

FULL: JsonObject = {
    "workflowId": "wf_support",
    "obligationIds": ["ob_refund_window", "ob_no_promises"],
    "componentIds": ["cmp_lookup_order"],
    "sourceTraceIds": ["trace-1", "trace-2"],
    "generatedBy": "hajer-generator",
    "provenance": {"from": "recorded", "sampled": True, "nested": {"n": [1, 2]}},
}


def _test(
    hajer: JsonValue = None,
    *,
    with_hajer: bool = True,
    test_case_id: str | None = "tc-refund-1",
    description: str | None = None,
) -> JsonObject:
    metadata: JsonObject = {}
    if test_case_id is not None:
        metadata["testCaseId"] = test_case_id
    if with_hajer:
        metadata["hajer"] = hajer
    test: JsonObject = {"vars": {"question": "where is my refund"}, "assert": [{"type": "contains", "value": "refund"}]}
    if description is not None:
        test["description"] = description
    if metadata:
        test["metadata"] = metadata
    return test


class TestTheSchema:
    def test_a_full_object_is_a_platform_test_with_every_field_read(self) -> None:
        item = classify_test(_test(FULL), 0)
        assert item.correlation == "platform"
        assert item.errors == ()
        assert item.warnings == ()
        assert item.hajer is not None
        assert item.hajer.workflow_id == "wf_support"
        assert item.hajer.obligation_ids == ("ob_refund_window", "ob_no_promises")
        assert item.hajer.component_ids == ("cmp_lookup_order",)
        assert item.hajer.source_trace_ids == ("trace-1", "trace-2")
        assert item.hajer.generated_by == "hajer-generator"
        assert item.hajer.provenance == {"from": "recorded", "sampled": True, "nested": {"n": [1, 2]}}

    def test_only_the_workflow_id_is_required(self) -> None:
        item = classify_test(_test({"workflowId": "wf_support"}), 0)
        assert item.correlation == "platform"
        assert item.hajer is not None
        assert item.hajer == HajerTestMetadata(workflow_id="wf_support")
        assert item.hajer.obligation_ids is None
        assert item.hajer.component_ids is None
        assert item.hajer.source_trace_ids is None
        assert item.hajer.generated_by is None
        assert item.hajer.provenance is None

    def test_ids_are_trimmed(self) -> None:
        item = classify_test(_test({"workflowId": "  wf_support ", "obligationIds": [" ob_a"]}), 0)
        assert item.hajer is not None
        assert item.hajer.workflow_id == "wf_support"
        assert item.hajer.obligation_ids == ("ob_a",)

    def test_a_plain_promptfoo_test_is_none_and_silent(self) -> None:
        item = classify_test(_test(with_hajer=False), 0)
        assert item.correlation == "none"
        assert item.hajer is None
        assert item.warnings == ()
        assert item.errors == ()

    def test_a_hajer_value_that_is_not_an_object_is_one_error(self) -> None:
        item = classify_test(_test(123, test_case_id=None, description="refund status, unknown order"), 4)
        assert item.correlation == "none"
        assert len(item.errors) == 1
        assert item.errors[0].startswith(f'[{E_HAJER_NOT_OBJECT}] test #4 "refund status, unknown order": ')
        assert "a number, not an object" in item.errors[0]

    def test_a_non_string_workflow_id_names_the_key(self) -> None:
        item = classify_test(_test({"workflowId": 123}), 0)
        assert item.correlation == "none"
        assert len(item.errors) == 1
        assert item.errors[0].startswith(f'[{E_HAJER_INVALID}] test #0 "tc-refund-1": metadata.hajer.workflowId')
        assert "string" in item.errors[0]

    def test_the_classic_typo_names_the_stray_key_and_lists_the_right_ones(self) -> None:
        item = classify_test(_test({"workflowID": "wf_support"}), 2)
        assert item.correlation == "none"
        stray = [line for line in item.errors if "metadata.hajer.workflowID is not a known key" in line]
        assert len(stray) == 1
        assert stray[0].startswith(f"[{E_HAJER_INVALID}] test #2")
        for key in ALLOWED_KEYS:
            assert key in stray[0]
        assert ALLOWED_KEYS == (
            "workflowId",
            "obligationIds",
            "componentIds",
            "sourceTraceIds",
            "generatedBy",
            "provenance",
        )
        # The typo also leaves the required key missing, and that is its own line.
        assert any("metadata.hajer.workflowId is required" in line for line in item.errors)
        assert len(item.errors) == 2

    def test_an_empty_id_is_refused(self) -> None:
        item = classify_test(_test({"workflowId": "   "}), 0)
        assert len(item.errors) == 1
        assert "metadata.hajer.workflowId: an id must not be empty" in item.errors[0]

    def test_an_overlong_id_is_refused_and_the_bound_is_named(self) -> None:
        item = classify_test(_test({"workflowId": "w" * (ID_MAX_CHARS + 1)}), 0)
        assert len(item.errors) == 1
        assert f"at most {ID_MAX_CHARS} characters" in item.errors[0]
        assert classify_test(_test({"workflowId": "w" * ID_MAX_CHARS}), 0).correlation == "platform"

    def test_an_empty_obligation_id_names_its_position(self) -> None:
        item = classify_test(_test({"workflowId": "wf", "obligationIds": ["ob_a", ""]}), 0)
        assert len(item.errors) == 1
        assert "metadata.hajer.obligationIds.1: an id must not be empty" in item.errors[0]

    def test_duplicate_obligation_ids_are_refused(self) -> None:
        item = classify_test(_test({"workflowId": "wf", "obligationIds": ["ob_a", "ob_b", "ob_a"]}), 0)
        assert len(item.errors) == 1
        assert "metadata.hajer.obligationIds: ids must be unique; repeated: ob_a" in item.errors[0]

    def test_an_obligation_list_that_is_not_a_list_is_refused(self) -> None:
        item = classify_test(_test({"workflowId": "wf", "obligationIds": "ob_a"}), 0)
        assert len(item.errors) == 1
        assert "metadata.hajer.obligationIds" in item.errors[0]

    def test_every_pydantic_error_is_its_own_line(self) -> None:
        item = classify_test(_test({"workflowId": 1, "generatedBy": 2, "extra": 3}), 0)
        assert len(item.errors) == 3
        assert all(line.startswith(f"[{E_HAJER_INVALID}] ") for line in item.errors)


class TestTheStableId:
    def test_a_missing_test_case_id_is_a_warning_not_an_error(self) -> None:
        item = classify_test(_test({"workflowId": "wf_support"}, test_case_id=None), 0)
        assert item.correlation == "platform"
        assert item.test_case_id is None
        assert item.errors == ()
        assert len(item.warnings) == 1
        assert item.warnings[0].startswith(f"[{W_NO_TEST_CASE_ID}] test #0: ")
        assert "falls back to the test's position" in item.warnings[0]
        assert "add metadata.testCaseId" in item.warnings[0]

    def test_a_blank_test_case_id_counts_as_missing(self) -> None:
        item = classify_test(_test({"workflowId": "wf_support"}, test_case_id="  "), 0)
        assert item.test_case_id is None
        assert len(item.warnings) == 1

    def test_a_present_test_case_id_is_the_label_and_silences_the_warning(self) -> None:
        item = classify_test(_test({"workflowId": "wf_support"}, test_case_id="tc-refund-1"), 0)
        assert item.test_case_id == "tc-refund-1"
        assert item.label == "tc-refund-1"
        assert item.warnings == ()

    def test_a_plain_test_without_an_id_is_not_warned_about(self) -> None:
        item = classify_test(_test(with_hajer=False, test_case_id=None), 0)
        assert item.warnings == ()

    def test_the_label_falls_back_to_the_description_then_the_position(self) -> None:
        described = classify_test(_test(with_hajer=False, test_case_id=None, description=" refund status "), 7)
        assert described.label == "refund status"
        assert described.index == 7
        bare = classify_test(_test(with_hajer=False, test_case_id=None), 7)
        assert bare.label == "#7"

    def test_the_position_is_named_alongside_the_label_in_every_line(self) -> None:
        item = classify_test(_test({"workflowId": "wf"}, test_case_id=None, description="refund: unknown order"), 3)
        assert item.warnings[0].startswith(f'[{W_NO_TEST_CASE_ID}] test #3 "refund: unknown order": ')


class TestInheritance:
    def test_default_test_keys_are_deep_merged_under_the_tests_own(self) -> None:
        test = _test({"obligationIds": ["ob_a"]})
        merged = merged_hajer_metadata(test, {"workflowId": "wf_support"})
        assert merged == {"workflowId": "wf_support", "obligationIds": ["ob_a"]}
        item = classify_test(test, 0, inherited={"workflowId": "wf_support"})
        assert item.correlation == "platform"
        assert item.hajer is not None
        assert item.hajer.workflow_id == "wf_support"
        assert item.hajer.obligation_ids == ("ob_a",)

    def test_the_tests_own_key_wins(self) -> None:
        merged = merged_hajer_metadata(
            _test({"workflowId": "wf_mine"}), {"workflowId": "wf_default", "generatedBy": "g"}
        )
        assert merged == {"workflowId": "wf_mine", "generatedBy": "g"}

    def test_a_test_without_its_own_object_inherits_the_whole_default(self) -> None:
        test = _test(with_hajer=False)
        assert merged_hajer_metadata(test, {"workflowId": "wf_default"}) == {"workflowId": "wf_default"}
        assert classify_test(test, 0, inherited={"workflowId": "wf_default"}).correlation == "platform"

    def test_an_explicit_null_is_absent_and_inherits(self) -> None:
        assert merged_hajer_metadata(_test(None), {"workflowId": "wf_default"}) == {"workflowId": "wf_default"}

    def test_nothing_on_either_side_is_none(self) -> None:
        assert merged_hajer_metadata(_test(with_hajer=False), None) is None
        assert merged_hajer_metadata(_test(with_hajer=False), inherited=None) is None

    def test_a_non_object_on_the_test_is_not_merged_over(self) -> None:
        test = _test("wf_support")
        assert merged_hajer_metadata(test, {"workflowId": "wf_default"}) is None
        item = classify_test(test, 0, inherited={"workflowId": "wf_default"})
        assert len(item.errors) == 1
        assert E_HAJER_NOT_OBJECT in item.errors[0]
        assert "a string, not an object" in item.errors[0]

    def test_the_merge_does_not_touch_the_test(self) -> None:
        test = _test({"obligationIds": ["ob_a"]})
        before = {"obligationIds": ["ob_a"]}
        merged_hajer_metadata(test, {"workflowId": "wf_support"})
        metadata = test["metadata"]
        assert isinstance(metadata, dict)
        assert metadata["hajer"] == before
