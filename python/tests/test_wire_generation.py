"""The contract chain: the generator reads the backend snapshot and `_wire.py` is what it emits.

Two things are proved here. First, that the committed placeholder is exactly what today's snapshot
produces — the same gate `just contract-check` runs, so a snapshot that moves without a refresh fails
in CI rather than in a customer's process. Second, that the generator is **ready**: given a snapshot
that does declare `POST /api/teams/{team_id}/verify`, it emits pydantic models for the schemas that
operation reaches, and the module it writes imports and validates. That second test is the reason the
placeholder is a placeholder and not a stub: the day the backend routes land, one command finishes it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from hajer import _wire
from hajer._json import JsonObject
from hajer._paths import SDK_OPERATIONS, VERIFY_PATH
from scripts.generate_wire import DEFAULT_SNAPSHOT, SNAPSHOT_NAME, TARGET, main, render

SNAPSHOT = DEFAULT_SNAPSHOT


def _synthetic_snapshot() -> bytes:
    """A snapshot shaped like the backend's, carrying the operation that does not exist yet."""
    document: JsonObject = {
        "openapi": "3.1.0",
        "info": {"title": "hajer", "version": "0.1.0"},
        "paths": {
            VERIFY_PATH: {
                "post": {
                    "operationId": "verify",
                    "requestBody": {
                        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/VerifyIn"}}}
                    },
                    "responses": {
                        "200": {
                            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/AssessmentOut"}}}
                        }
                    },
                }
            }
        },
        "components": {
            "schemas": {
                "VerifyIn": {
                    "type": "object",
                    "description": "One verification submission.",
                    "properties": {
                        "verifier": {"type": "string"},
                        "deadlineMs": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                        "evidence": {"type": "object"},
                    },
                    "required": ["verifier"],
                },
                "AssessmentOut": {
                    "type": "object",
                    "properties": {
                        "status": {"enum": ["satisfied", "violated"], "type": "string"},
                        "findings": {"type": "array", "items": {"$ref": "#/components/schemas/FindingOut"}},
                        "costMicrousd": {"type": "integer"},
                    },
                    "required": ["status", "findings", "costMicrousd"],
                },
                "FindingOut": {
                    "type": "object",
                    "properties": {"statement": {"type": "string"}},
                    "required": ["statement"],
                },
                "UnreachedOut": {"type": "object", "properties": {"nothing": {"type": "string"}}},
            }
        },
    }
    return json.dumps(document).encode("utf-8")


class TestAgainstTodaysSnapshot:
    def test_the_committed_wire_module_is_what_the_generator_emits(self) -> None:
        assert TARGET.read_text(encoding="utf-8") == render(SNAPSHOT.read_bytes())

    def test_check_mode_passes(self) -> None:
        assert main(["--check", "--snapshot", str(DEFAULT_SNAPSHOT)]) == 0

    def test_a_missing_snapshot_is_reported_not_guessed(self, tmp_path: Path) -> None:
        assert main(["--check", "--snapshot", str(tmp_path / "openapi.json")]) == 1

    def test_the_generated_module_names_the_snapshot_it_came_from(self) -> None:
        assert _wire.SNAPSHOT_PATH == SNAPSHOT_NAME
        assert "PLACEHOLDER" not in TARGET.read_text(encoding="utf-8"), "the routes landed; this is generated now"
        assert _wire.WIRE_READY is True

    def test_a_vocabulary_is_a_type_alias_and_never_an_empty_model(self) -> None:
        """The backend emits `type CheckReason = Literal[...]` and `JsonValue` as named components.

        Rendering either as a `BaseModel` with no properties would turn a closed vocabulary into an
        object that accepts anything, which is the opposite of what a generated wire is for.
        """
        emitted = TARGET.read_text(encoding="utf-8")
        assert 'VerificationStatus: TypeAlias = Literal["satisfied", "violated"' in emitted
        assert "class VerificationStatus(BaseModel)" not in emitted
        assert "class JsonValue(BaseModel)" not in emitted
        assert "from hajer._json import JsonValue" in emitted

    def test_the_operations_the_sdk_calls_are_declared_in_one_place(self) -> None:
        assert [path for _, path in SDK_OPERATIONS] == [
            "/api/teams/{team_id}/verify",
            "/api/teams/{team_id}/observe",
            "/api/teams/{team_id}/observations",
            "/api/teams/{team_id}/observations/{observation_id}/assessment",
        ]

    def test_the_tails_row_is_generated_from_the_snapshot_and_not_written_here(self) -> None:
        """`python -m hajer tail` prints these fields, and the backend is where their names come from."""
        emitted = TARGET.read_text(encoding="utf-8")
        assert "class ObservationRowOut(BaseModel):" in emitted
        assert 'created_at: str = Field(alias="createdAt")' in emitted
        assert 'redacted_classes: tuple[str, ...] = Field(alias="redactedClasses")' in emitted
        assert "verifier: str | None" in emitted


class TestWhenTheRoutesLand:
    """The same generator, against a snapshot that has the operation. This is the finished state."""

    def test_it_emits_models_for_every_reached_schema_and_nothing_else(self) -> None:
        emitted = render(_synthetic_snapshot())
        assert "WIRE_READY: Final[bool] = True" in emitted
        assert 'OPERATIONS: Final[tuple[tuple[str, str], ...]] = (("post", "/api/teams/{team_id}/verify"),)' in emitted
        assert "class VerifyIn(BaseModel):" in emitted
        assert "class AssessmentOut(BaseModel):" in emitted
        assert "class FindingOut(BaseModel):" in emitted
        assert "UnreachedOut" not in emitted, "a schema the SDK's operations never reach is not generated"

    def test_the_emitted_types_are_the_snapshot_types(self) -> None:
        emitted = render(_synthetic_snapshot())
        assert "verifier: str" in emitted
        assert 'deadline_ms: int | None = Field(None, alias="deadlineMs")' in emitted
        assert 'status: Literal["satisfied", "violated"]' in emitted
        assert "findings: tuple[FindingOut, ...]" in emitted
        assert 'cost_microusd: int = Field(alias="costMicrousd")' in emitted
        assert "evidence: dict[str, JsonValue] | None = None" in emitted

    def test_the_emitted_module_executes_and_validates(self) -> None:
        namespace: dict[str, object] = {}
        # The generator's output is executed here on purpose: "it compiles" is not the claim; "the
        # models it wrote validate the payload the snapshot describes" is.
        exec(compile(render(_synthetic_snapshot()), "<generated wire>", "exec"), namespace)  # noqa: S102
        model = namespace["AssessmentOut"]
        assert isinstance(model, type)
        assert issubclass(model, BaseModel)
        assessment = model.model_validate(
            {"status": "violated", "findings": [{"statement": "promised a refund"}], "costMicrousd": 40}
        )
        dumped = assessment.model_dump()
        assert dumped["status"] == "violated"
        assert dumped["cost_microusd"] == 40
        assert dumped["findings"][0]["statement"] == "promised a refund"

    def test_a_document_that_is_not_openapi_3_is_refused(self) -> None:
        document = json.loads(_synthetic_snapshot())
        document["openapi"] = "2.0"
        with pytest.raises(ValueError, match=r"this generator reads 3\.x"):
            render(json.dumps(document).encode("utf-8"))
