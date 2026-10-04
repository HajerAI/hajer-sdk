"""`hajer.yaml` — what a repository declares about its evals: the suites, and the obligations the tests cover.

A promptfoo configuration is a suite; a repository may have several, and a test names the obligations it covers
(`metadata.hajer.obligationIds`). The manifest is the one place both are declared, at the repository root, so that
`hajer eval` with no `-c` knows what to run and what an obligation id may be, and the platform — which reads the
same file through the repository's GitHub connection — knows what the suites and obligations *are* before a
single run has been uploaded:

    version: 1
    suites:
      - id: support                              # ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$, unique
        path: evals/support/promptfooconfig.yaml  # relative to this file, inside the repository
        description: Customer support regression  # optional
    obligations:
      - id: obl_refund_status_disclosed           # the same id pattern, unique
        title: Refund status is disclosed accurately
        workflow: wf_support                      # the workflow the obligation is about
        description: ...                           # optional

The schema is strict — an unknown key, a duplicate id, a path outside the repository or a suite file that does not
exist are refusals with the file and the key named — because the platform reads this file by the same rules, and a
typo that one side forgave and the other did not would be a suite that runs and is never seen. Refusals raise
`ManifestError`, in `hajer eval`'s own process, never in an application's.

`pyyaml` is a dependency of the `evals` extra and is imported here only, inside `load_manifest`: the `hajer`
command loads this module for every subcommand, so a plain `pip install hajer` must still import it.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Annotated, Final, TypeAlias, cast

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, field_validator

from hajer._errors import ManifestError
from hajer._json import JsonObject
from hajer._settings import (
    MANIFEST_DESCRIPTION_MAX_CHARS,
    MANIFEST_FILE_MAX_BYTES,
    MANIFEST_OBLIGATIONS_MAX,
    MANIFEST_SUITES_MAX,
    MANIFEST_TITLE_MAX_CHARS,
)

#: The file names looked for, in order, from the working directory up to the filesystem root.
MANIFEST_NAMES: Final[tuple[str, ...]] = ("hajer.yaml", "hajer.yml")
#: The one version this reader knows.
MANIFEST_VERSION: Final[int] = 1
#: What a suite id, an obligation id and a workflow id may be: a letter or digit first, then letters, digits,
#: dots, underscores and dashes, at most 128 characters — the platform's own bound on these ids.
ID_PATTERN: Final[str] = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
_ID: Final = re.compile(ID_PATTERN)


def _checked_id(value: str) -> str:
    if not _ID.fullmatch(value):
        raise ValueError(f"must match {ID_PATTERN}")
    return value


def _checked_path(value: str) -> str:
    """A repository-relative POSIX path: not absolute, no `..`, no backslashes, and something after the slashes."""
    if not value or value != value.strip() or "\\" in value:
        raise ValueError("must be a repository-relative path, written with forward slashes")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or value.startswith("/"):
        raise ValueError("must be relative to the manifest and inside the repository (no leading '/', no '..')")
    return value


BoundedId: TypeAlias = Annotated[str, AfterValidator(_checked_id)]
RelativePath: TypeAlias = Annotated[str, Field(max_length=512), AfterValidator(_checked_path)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class SuiteSpec(_Strict):
    id: BoundedId
    path: RelativePath
    description: Annotated[str, Field(max_length=MANIFEST_DESCRIPTION_MAX_CHARS)] | None = None


class ObligationSpec(_Strict):
    id: BoundedId
    title: Annotated[str, Field(min_length=1, max_length=MANIFEST_TITLE_MAX_CHARS)]
    workflow: BoundedId
    description: Annotated[str, Field(max_length=MANIFEST_DESCRIPTION_MAX_CHARS)] | None = None


class ManifestSpec(_Strict):
    """The file as written, validated. `Manifest` is what the rest of the package reads."""

    version: int
    suites: Annotated[list[SuiteSpec], Field(max_length=MANIFEST_SUITES_MAX)] = []
    obligations: Annotated[list[ObligationSpec], Field(max_length=MANIFEST_OBLIGATIONS_MAX)] = []

    @field_validator("version")
    @classmethod
    def the_one_version(cls, value: int) -> int:
        if value != MANIFEST_VERSION:
            raise ValueError(f"must be {MANIFEST_VERSION}; this reader knows no other")
        return value

    @field_validator("suites")
    @classmethod
    def suite_ids_are_unique(cls, value: list[SuiteSpec]) -> list[SuiteSpec]:
        _unique([suite.id for suite in value], "suite")
        return value

    @field_validator("obligations")
    @classmethod
    def obligation_ids_are_unique(cls, value: list[ObligationSpec]) -> list[ObligationSpec]:
        _unique([obligation.id for obligation in value], "obligation")
        return value


def _unique(ids: list[str], noun: str) -> None:
    repeated = sorted({item for item in ids if ids.count(item) > 1})
    if repeated:
        raise ValueError(f"{noun} ids must be unique; repeated: {', '.join(repeated)}")


@dataclass(frozen=True, slots=True)
class Suite:
    """One declared suite: its id, and the configuration file it names, resolved against the manifest's directory."""

    id: str
    path: Path
    #: As declared: relative to the manifest, which is what the payload and the platform spell.
    declared_path: str
    description: str | None


@dataclass(frozen=True, slots=True)
class Obligation:
    id: str
    title: str
    workflow: str
    description: str | None


@dataclass(frozen=True, slots=True)
class Manifest:
    path: Path
    suites: tuple[Suite, ...]
    obligations: tuple[Obligation, ...]

    @property
    def directory(self) -> Path:
        return self.path.parent

    @property
    def obligation_ids(self) -> tuple[str, ...]:
        return tuple(obligation.id for obligation in self.obligations)

    def suite(self, suite_id: str) -> Suite | None:
        return next((suite for suite in self.suites if suite.id == suite_id), None)


def find_manifest(start: Path) -> Path | None:
    """The nearest `hajer.yaml` (then `hajer.yml`) in `start` or a directory above it, or `None`."""
    for directory in _upwards(start.resolve()):
        for name in MANIFEST_NAMES:
            candidate = directory / name
            if candidate.is_file():
                return candidate
    return None


def _upwards(start: Path) -> Iterator[Path]:
    current = start if start.is_dir() else start.parent
    while True:
        yield current
        if current.parent == current:
            return
        current = current.parent


def load_manifest(path: Path) -> Manifest:
    """Read and validate one manifest, or `ManifestError` naming the file and what is wrong with it."""
    try:
        if path.stat().st_size > MANIFEST_FILE_MAX_BYTES:
            raise ManifestError(path, f"is larger than {MANIFEST_FILE_MAX_BYTES} bytes")
        text = path.read_text(encoding="utf-8")
    except OSError as unreadable:
        raise ManifestError(path, f"cannot be read: {unreadable.strerror or unreadable}") from None
    try:
        import yaml  # noqa: PLC0415 - the `evals` extra; imported where it is used so `hajer` loads without it
    except ImportError:
        raise ManifestError(path, "needs a YAML reader: pip install 'hajer[evals]'") from None
    try:
        loaded: object = yaml.safe_load(text)
    except yaml.YAMLError as invalid:
        raise ManifestError(path, f"is not valid YAML: {invalid}") from None
    if not isinstance(loaded, dict):
        raise ManifestError(path, "must be a mapping with `version`, `suites` and `obligations`")
    try:
        spec = ManifestSpec.model_validate(_keys_as_strings(cast(dict[object, object], loaded)))
    except ValidationError as invalid:
        raise ManifestError(
            path, "; ".join(_described(detail["loc"], detail["type"], detail["msg"]) for detail in invalid.errors())
        ) from None
    suites = tuple(
        Suite(
            id=item.id, path=(path.parent / item.path).resolve(), declared_path=item.path, description=item.description
        )
        for item in spec.suites
    )
    missing = [suite.declared_path for suite in suites if not suite.path.is_file()]
    if missing:
        raise ManifestError(path, f"names a suite file that does not exist: {', '.join(missing)}")
    return Manifest(
        path=path,
        suites=suites,
        obligations=tuple(
            Obligation(id=item.id, title=item.title, workflow=item.workflow, description=item.description)
            for item in spec.obligations
        ),
    )


def _keys_as_strings(loaded: dict[object, object]) -> JsonObject:
    """YAML keys may be anything; pydantic wants strings. A non-string key is a stray key, named as such."""
    return {str(key): value for key, value in loaded.items()}  # pyright: ignore[reportReturnType] - validated next


def _described(loc: tuple[int | str, ...], kind: str, message: str) -> str:
    where = ".".join(str(part) for part in loc) or "the document"
    if kind == "extra_forbidden":
        return f"{where} is not a known key"
    if kind == "missing":
        return f"{where} is required"
    return f"{where}: {message.removeprefix('Value error, ')}"
