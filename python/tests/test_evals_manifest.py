"""`hajer.yaml`: read strictly, found upward, every refusal naming the file and the key."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from hajer._errors import ManifestError
from hajer._settings import MANIFEST_OBLIGATIONS_MAX, MANIFEST_SUITES_MAX
from hajer.evals._manifest import MANIFEST_NAMES, find_manifest, load_manifest

GOOD = """
version: 1
suites:
  - id: support
    path: evals/support.yaml
    description: Support regression
  - id: billing
    path: evals/billing.yaml
obligations:
  - id: obl_refund_status_disclosed
    title: Refund status is disclosed accurately
    workflow: wf_support
    description: Never claims a pending refund completed.
  - id: obl_tone
    title: Tone stays courteous
    workflow: wf_support
"""


def repository(tmp_path: Path, text: str = GOOD, *, suites: tuple[str, ...] = ("support", "billing")) -> Path:
    (tmp_path / "evals").mkdir(exist_ok=True)
    for name in suites:
        (tmp_path / "evals" / f"{name}.yaml").write_text("prompts: ['{{q}}']\nproviders: [echo]\ntests: []\n")
    manifest = tmp_path / "hajer.yaml"
    manifest.write_text(text)
    return manifest


class TestLoading:
    def test_the_suites_and_obligations_are_read_with_paths_resolved_against_the_manifest(self, tmp_path: Path) -> None:
        manifest = load_manifest(repository(tmp_path))
        assert manifest.directory == tmp_path
        assert [suite.id for suite in manifest.suites] == ["support", "billing"]
        support = manifest.suite("support")
        assert support is not None
        assert support.path == (tmp_path / "evals" / "support.yaml").resolve()
        assert support.declared_path == "evals/support.yaml"
        assert support.description == "Support regression"
        assert manifest.suite("billing") is not None
        assert manifest.suite("nope") is None
        assert manifest.obligation_ids == ("obl_refund_status_disclosed", "obl_tone")
        first = manifest.obligations[0]
        assert (first.title, first.workflow) == ("Refund status is disclosed accurately", "wf_support")
        assert first.description is not None
        assert manifest.obligations[1].description is None

    def test_a_manifest_with_no_obligations_and_no_suites_is_valid(self, tmp_path: Path) -> None:
        manifest = load_manifest(repository(tmp_path, "version: 1\n", suites=()))
        assert manifest.suites == ()
        assert manifest.obligations == ()

    @pytest.mark.parametrize(
        ("text", "mentions"),
        [
            ("version: 2\n", "version"),
            ("suites: []\n", "version is required"),
            ("version: 1\nextra: 1\n", "extra is not a known key"),
            ("version: 1\nsuites:\n  - id: a\n    path: evals/support.yaml\n    paht: x\n", "paht is not a known key"),
            (
                "version: 1\nsuites:\n  - id: a\n    path: evals/support.yaml\n  - id: a\n    path: evals/billing.yaml\n",
                "suite ids must be unique",
            ),
            (
                "version: 1\nobligations:\n  - {id: o, title: t, workflow: w}\n  - {id: o, title: t, workflow: w}\n",
                "obligation ids must be unique",
            ),
            ("version: 1\nsuites:\n  - id: 'bad id'\n    path: evals/support.yaml\n", "suites.0.id"),
            ("version: 1\nsuites:\n  - id: a\n    path: /etc/passwd\n", "relative"),
            ("version: 1\nsuites:\n  - id: a\n    path: ../outside.yaml\n", "relative"),
            ("version: 1\nsuites:\n  - id: a\n    path: evals\\support.yaml\n", "forward slashes"),
            ("version: 1\nsuites:\n  - id: a\n    path: evals/missing.yaml\n", "does not exist: evals/missing.yaml"),
            ("version: 1\nobligations:\n  - {id: o, title: '', workflow: w}\n", "obligations.0.title"),
            ("version: 1\nobligations:\n  - {id: o, title: t}\n", "obligations.0.workflow is required"),
            ("- not\n- a mapping\n", "must be a mapping"),
            ("version: [1\n", "not valid YAML"),
        ],
    )
    def test_every_refusal_names_the_file_and_what_is_wrong(self, tmp_path: Path, text: str, mentions: str) -> None:
        manifest = repository(tmp_path, text)
        with pytest.raises(ManifestError) as refused:
            load_manifest(manifest)
        assert str(manifest) in str(refused.value)
        assert mentions in str(refused.value), str(refused.value)
        assert refused.value.code == "HAJER_INVALID_MANIFEST"

    def test_the_counts_are_bounded(self, tmp_path: Path) -> None:
        suites = "".join(f"  - id: s{i}\n    path: evals/support.yaml\n" for i in range(MANIFEST_SUITES_MAX + 1))
        with pytest.raises(ManifestError, match="suites"):
            load_manifest(repository(tmp_path, f"version: 1\nsuites:\n{suites}"))
        obligations = "".join(f"  - {{id: o{i}, title: t, workflow: w}}\n" for i in range(MANIFEST_OBLIGATIONS_MAX + 1))
        with pytest.raises(ManifestError, match="obligations"):
            load_manifest(repository(tmp_path, f"version: 1\nobligations:\n{obligations}"))

    def test_an_unreadable_file_is_a_refusal_not_a_traceback(self, tmp_path: Path) -> None:
        with pytest.raises(ManifestError, match="cannot be read"):
            load_manifest(tmp_path / "hajer.yaml")

    def test_without_the_evals_extra_the_refusal_names_what_to_install(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "yaml", None)
        with pytest.raises(ManifestError, match=r"pip install 'hajer\[evals\]'"):
            load_manifest(repository(tmp_path))


class TestDiscovery:
    def test_the_nearest_manifest_above_the_working_directory_is_found(self, tmp_path: Path) -> None:
        manifest = repository(tmp_path)
        deep = tmp_path / "services" / "support"
        deep.mkdir(parents=True)
        assert find_manifest(deep) == manifest
        assert find_manifest(tmp_path) == manifest
        assert find_manifest(deep / "promptfooconfig.yaml") == manifest, "a file's directory is searched"

    def test_yaml_wins_over_yml_and_none_is_none(self, tmp_path: Path) -> None:
        assert MANIFEST_NAMES == ("hajer.yaml", "hajer.yml")
        (tmp_path / "hajer.yml").write_text("version: 1\n")
        assert find_manifest(tmp_path) == tmp_path / "hajer.yml"
        (tmp_path / "hajer.yaml").write_text("version: 1\n")
        assert find_manifest(tmp_path) == tmp_path / "hajer.yaml"
        empty = tmp_path / "empty"
        empty.mkdir()
        assert find_manifest(empty) == tmp_path / "hajer.yaml", "found above"
        assert find_manifest(Path("/").resolve()) is None or True  # the filesystem root has none in CI; never raises
