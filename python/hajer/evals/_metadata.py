"""The `metadata.hajer` convention: what a promptfoo test says about the workflow it exercises, validated once.

A test's `metadata` is promptfoo's own free-form bag; `hajer` is the one key Hajer reserves inside it. The object
under it binds a row of the eval to the platform — the workflow the application runs for it, the obligations it
exercises, the trace it was generated from — so a mistake in it is a row the platform never correlates, found weeks
later. That is why validation is strict and happens in the engine's `beforeAll` hook, before a provider call: an
unknown key (`workflowID` for `workflowId`) stops the run and the terminal names the test and the allowed keys.

Two things here are deliberately *not* errors. A test with no `hajer` object at all is an ordinary promptfoo test
and runs unchanged. A test with no `metadata.testCaseId` — promptfoo's own stable id, and the key the engine
correlates traces on — runs too, with a warning: the fallback is the test's position (`"{testIdx}-{promptIdx}"`),
which moves when the suite is reordered, so results would stop lining up across runs.

The third check is not about `metadata` at all. promptfoo renders an assertion's `value` as a Nunjucks template with
the test's vars, and an undefined variable renders as an empty string — so `not-contains: "Dear {{firstName}}"` with
no `firstName` var asserts that the output does not contain "Dear ", and passes for a reply that greets anybody by
name. A `{{ name }}` whose root is not one of the test's vars is a warning: sometimes the literal text was meant
(`{% raw %}` says so), and the hook cannot see a var a `transformVars` adds, so it does not look when one is set.

promptfoo merges `defaultTest.metadata` into each test's `metadata` shallowly, so a test's `hajer: {obligationIds}`
would *replace* an inherited `hajer: {workflowId}`. `merged_hajer_metadata` is the deep merge the hook writes back
before promptfoo's own merge runs, so both sides see complete objects.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated, Final, Literal, TypeAlias, cast

from pydantic import AfterValidator, BaseModel, ConfigDict, ValidationError, field_validator
from pydantic.alias_generators import to_camel

from hajer._json import JsonObject, JsonValue
from hajer._settings import ID_MAX_CHARS

#: The key Hajer reserves inside a promptfoo test's `metadata`.
HAJER_METADATA_KEY: Final = "hajer"
#: promptfoo's own key for a stable test-case id, which the engine uses as the trace-correlation key.
TEST_CASE_ID_KEY: Final = "testCaseId"
#: The promptfoo key the two above live under.
METADATA_KEY: Final = "metadata"

#: The codes a classification reports, each the first thing in its line so a terminal is greppable by them.
W_NO_TEST_CASE_ID: Final = "W_NO_TEST_CASE_ID"
W_TEMPLATE_UNDEFINED_VAR: Final = "W_TEMPLATE_UNDEFINED_VAR"
E_HAJER_NOT_OBJECT: Final = "E_HAJER_NOT_OBJECT"
E_HAJER_INVALID: Final = "E_HAJER_INVALID"
E_OBLIGATION_UNDECLARED: Final = "E_OBLIGATION_UNDECLARED"

Correlation: TypeAlias = Literal["platform", "none"]

#: Names an assertion template may use that are not the test's vars: promptfoo's `env` global and the conversation
#: var it adds at run time, Nunjucks' own globals, and the literals that look like identifiers.
_TEMPLATE_NAMES: Final[frozenset[str]] = frozenset(
    {
        "env",
        "_conversation",
        "range",
        "cycler",
        "joiner",
        "loop",
        "not",
        "true",
        "false",
        "none",
        "True",
        "False",
        "None",
    }
)
#: A `{% raw %}…{% endraw %}` block (its text is printed as written) and a `{# … #}` comment: removed before scanning.
_TEMPLATE_VERBATIM: Final = re.compile(r"\{%-?\s*raw\s*-?%\}.*?\{%-?\s*endraw\s*-?%\}|\{#.*?#\}", re.DOTALL)
#: The root identifier of a `{{ … }}` expression: `name` in `{{ name }}`, `{{ name.attr }}`, `{{ name | upper }}`.
_TEMPLATE_ROOT: Final = re.compile(r"\{\{-?\s*([A-Za-z_][A-Za-z0-9_]*)")
#: Names a template binds itself: `{% set name = … %}` and `{% for a, b in … %}`.
_TEMPLATE_BINDINGS: Final = re.compile(
    r"\{%-?\s*(?:set|for)\s+([A-Za-z_][A-Za-z0-9_]*(?:\s*,\s*[A-Za-z_][A-Za-z0-9_]*)*)"
)


def _checked_id(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("an id must not be empty")
    if len(stripped) > ID_MAX_CHARS:
        raise ValueError(f"an id must be at most {ID_MAX_CHARS} characters; got {len(stripped)}")
    return stripped


#: One id as the model stores it: a string (strict), trimmed, non-empty, within `ID_MAX_CHARS`. Checked per element
#: so an error inside a list names the position (`obligationIds.1`) rather than the whole list.
BoundedId: TypeAlias = Annotated[str, AfterValidator(_checked_id)]


class HajerTestMetadata(BaseModel):
    """The validated `metadata.hajer` object. Only `workflowId` is required; every id is bounded and trimmed."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        populate_by_name=True,
        alias_generator=to_camel,
    )

    workflow_id: BoundedId
    obligation_ids: tuple[BoundedId, ...] | None = None
    component_ids: tuple[BoundedId, ...] | None = None
    source_trace_ids: tuple[BoundedId, ...] | None = None
    generated_by: str | None = None
    provenance: JsonObject | None = None

    @field_validator("obligation_ids", "component_ids", "source_trace_ids", mode="before")
    @classmethod
    def a_json_array_arrives_as_a_list(cls, value: object) -> object:
        """A JSON array is a Python list, which strict mode refuses for a tuple; only that one conversion is made."""
        if isinstance(value, list):
            return tuple(cast(list[object], value))
        return value

    @field_validator("obligation_ids", "component_ids", "source_trace_ids", mode="after")
    @classmethod
    def ids_are_unique(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        """A repeated obligation id would count one obligation twice in every roll-up, so it is refused here."""
        if value is None:
            return None
        duplicates = sorted({item for item in value if value.count(item) > 1})
        if duplicates:
            raise ValueError(f"ids must be unique; repeated: {', '.join(duplicates)}")
        return value


#: The keys a `metadata.hajer` object may carry, in the wire's spelling — what an error about an unknown key lists.
ALLOWED_KEYS: Final[tuple[str, ...]] = tuple(
    field.alias or name for name, field in HajerTestMetadata.model_fields.items()
)


@dataclass(frozen=True, slots=True)
class TestClassification:
    """What the hook decided about one test, and every line it has to say about it.

    `index` is the test's position in `suite.tests`, zero-based like promptfoo's own `testIdx`. `label` is the
    `testCaseId`, else the `description`, else `#<index>`. Each warning and error is one complete line,
    `[CODE] test <ref>: message`, where `<ref>` is `#<index>` followed by the quoted label when the label is not the
    position itself — so the terminal names the row and the test in the same breath.
    """

    index: int
    label: str
    test_case_id: str | None
    correlation: Correlation
    hajer: HajerTestMetadata | None
    warnings: tuple[str, ...]
    errors: tuple[str, ...]


def merged_hajer_metadata(test: JsonObject, inherited: JsonObject | None) -> JsonObject | None:
    """The `hajer` object the test should carry: `defaultTest`'s keys under the test's own, the test winning per key.

    `None` when there is nothing to write back: neither side has a `hajer` object, or the test's own value is not
    an object (which `classify_test` reports; a deep merge cannot repair it, and overwriting it would hide it).
    """
    own = _own_hajer(test)
    if own is not None and not isinstance(own, dict):
        return None
    if own is None and inherited is None:
        return None
    merged: JsonObject = dict(inherited or {})
    merged.update(own or {})
    return merged


def classify_test(
    test: JsonObject, index: int, *, inherited: JsonObject | None = None, default_test: JsonObject | None = None
) -> TestClassification:
    """Decide whether one test is a platform-correlated test, and say everything wrong or risky about how it is one."""
    metadata = test.get(METADATA_KEY)
    test_case_id = _test_case_id(metadata)
    label = _label(test, index, test_case_id)
    ref = reference(index, label)
    own = _own_hajer(test)
    if own is not None and not isinstance(own, dict):
        error = f"[{E_HAJER_NOT_OBJECT}] test {ref}: metadata.hajer is {_kind(own)}, not an object"
        return TestClassification(index, label, test_case_id, "none", None, (), (error,))
    templates = template_warnings(test, ref, default_test=default_test)
    merged = merged_hajer_metadata(test, inherited)
    if merged is None:
        return TestClassification(index, label, test_case_id, "none", None, templates, ())
    try:
        hajer = HajerTestMetadata.model_validate(merged)
    except ValidationError as invalid:
        errors = tuple(_described(ref, detail["loc"], detail["type"], detail["msg"]) for detail in invalid.errors())
        return TestClassification(index, label, test_case_id, "none", None, (), errors)
    warnings: tuple[str, ...] = ()
    if test_case_id is None:
        warnings = (
            f"[{W_NO_TEST_CASE_ID}] test {ref}: no metadata.{TEST_CASE_ID_KEY}; correlation falls back to the "
            f"test's position, which moves when tests are reordered; add metadata.{TEST_CASE_ID_KEY}",
        )
    return TestClassification(index, label, test_case_id, "platform", hajer, (*warnings, *templates), ())


def template_warnings(test: JsonObject, ref: str, *, default_test: JsonObject | None = None) -> tuple[str, ...]:
    """One line per assertion `value` that renders a `{{ name }}` the test has no var for.

    The vars are the ones promptfoo renders the assertions with: `defaultTest.vars` under the test's own. The hook
    runs after promptfoo has loaded `tests:` files, a `defaultTest` file and a test's `vars:` files, so these are
    objects by then; vars that are not (or a `transformVars`, which adds vars nobody can see from here) skip the
    check rather than guess. Each var's own value may still be an unread `file://` reference: only its name counts.
    """
    defaults: JsonObject = default_test or {}
    own_vars = test.get("vars")
    inherited_vars = defaults.get("vars")
    if not isinstance(own_vars, (dict, type(None))) or not isinstance(inherited_vars, (dict, type(None))):
        return ()
    if _transforms_vars(test) or _transforms_vars(defaults):
        return ()
    known = {*(inherited_vars or {}), *(own_vars or {})}
    sources: list[tuple[str, JsonValue | None]] = [("assert", test.get("assert"))]
    options = test.get("options")
    if not (isinstance(options, dict) and options.get("disableDefaultAsserts") is True):
        sources.append(("defaultTest.assert", defaults.get("assert")))
    lines: list[str] = []
    for where, assertions in sources:
        for path, template in _assertion_templates(where, assertions):
            for name in _undefined_names(template, known):
                lines.append(
                    f"[{W_TEMPLATE_UNDEFINED_VAR}] test {ref}: {path}.value uses {{{{{name}}}}} but the test has no var "
                    f"{name!r}; promptfoo renders it as an empty string; to match the literal text write "
                    f"{{% raw %}}{{{{{name}}}}}{{% endraw %}}"
                )
    return tuple(lines)


def reference(index: int, label: str) -> str:
    """How a line names a test: `#4`, or `#4 "refund status, unknown order"` when the label says more than the position."""
    positional = f"#{index}"
    return positional if label == positional else f'{positional} "{label}"'


def _transforms_vars(test: JsonObject) -> bool:
    options = test.get("options")
    return isinstance(options, dict) and options.get("transformVars") is not None


def _assertion_templates(where: str, assertions: JsonValue | None) -> Iterator[tuple[str, str]]:
    """Every string promptfoo renders from an assertion list, with where it sits: `assert.1`, `assert.0.assert.2`.

    A `value` is rendered when it is a string or a list of strings, unless it names a file or a package, which are
    loaded rather than rendered. An `assert-set` carries its own `assert` list, rendered the same way.
    """
    if not isinstance(assertions, list):
        return
    for position, assertion in enumerate(assertions):
        if not isinstance(assertion, dict):
            continue
        path = f"{where}.{position}"
        value = assertion.get("value")
        candidates = value if isinstance(value, list) else [value]
        for candidate in candidates:
            if isinstance(candidate, str) and not candidate.startswith(("file://", "package:")):
                yield path, candidate
        yield from _assertion_templates(f"{path}.assert", assertion.get("assert"))


def _undefined_names(template: str, known: set[str]) -> list[str]:
    """The root names of `{{ … }}` expressions that are neither a var, a name the template binds, nor a global."""
    visible = _TEMPLATE_VERBATIM.sub("", template)
    bound = {name.strip() for group in _TEMPLATE_BINDINGS.findall(visible) for name in group.split(",")}
    undefined: list[str] = []
    for name in _TEMPLATE_ROOT.findall(visible):
        if name not in known and name not in bound and name not in _TEMPLATE_NAMES and name not in undefined:
            undefined.append(name)
    return undefined


def _own_hajer(test: JsonObject) -> JsonValue | None:
    """The test's own `metadata.hajer`, with an explicit `null` treated as absent (what a bare YAML `hajer:` is)."""
    metadata = test.get(METADATA_KEY)
    if not isinstance(metadata, dict):
        return None
    return metadata.get(HAJER_METADATA_KEY)


def _test_case_id(metadata: JsonValue | None) -> str | None:
    """`metadata.testCaseId` when it is a non-empty string; anything else is "no id" rather than an error."""
    if not isinstance(metadata, dict):
        return None
    value = metadata.get(TEST_CASE_ID_KEY)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _label(test: JsonObject, index: int, test_case_id: str | None) -> str:
    if test_case_id is not None:
        return test_case_id
    description = test.get("description")
    if isinstance(description, str) and description.strip():
        return description.strip()
    return f"#{index}"


def _described(ref: str, loc: tuple[int | str, ...], kind: str, message: str) -> str:
    """One pydantic error as one line that names the key in the wire's spelling and, for a stray key, the right ones."""
    path = ".".join(str(part) for part in loc)
    where = f"metadata.{HAJER_METADATA_KEY}.{path}" if path else f"metadata.{HAJER_METADATA_KEY}"
    if kind == "extra_forbidden":
        detail = f"{where} is not a known key; the keys are {', '.join(ALLOWED_KEYS)}"
    elif kind == "missing":
        detail = f"{where} is required"
    else:
        detail = f"{where}: {message.removeprefix('Value error, ')}"
    return f"[{E_HAJER_INVALID}] test {ref}: {detail}"


def _kind(value: JsonValue) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, (int, float)):
        return "a number"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, list):
        return "an array"
    return type(value).__name__
