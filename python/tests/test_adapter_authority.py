"""Saved adapter claims never authorize a new CI execution; identity mismatches remain neutral."""

from pathlib import Path

import pytest

from hajer._settings import CI_TEXT_MAX, HajerSettings
from hajer._verify_adapters import AdapterCheck, check_adapter, upload_identity
from hajer.pytest_plugin import _adapters
from hajer.pytest_plugin._models import Case, Execution, State, Suite


def bound_case(site: str = "app.py:3") -> Case:
    return Case(
        "case",
        [],
        Execution(
            adapter={"adapterId": "adapter", "module": "app", "qualname": "ask", "site": site},
            input="hello",
            boundaries=[],
        ),
    )


def test_a_saved_verified_claim_is_rechecked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Path] = []

    def check(root: Path, *_args: object, **_kwargs: object) -> AdapterCheck:
        calls.append(root)
        return AdapterCheck("adapter", None, "app.py:3", "RUNTIME_UNREACHED", "changed code")

    monkeypatch.setattr(_adapters, "check_adapter", check)
    state = State(adapters={("adapter", "app.py:3"): ("VERIFIED", "old report")})
    suite = Suite("suite", 1, tmp_path, [], site="app.py:3")
    assert _adapters.adapter_status(state, suite, bound_case(), HajerSettings()) == (
        "RUNTIME_UNREACHED",
        "changed code",
    )
    assert calls == [tmp_path]


def test_a_suite_cannot_borrow_verification_of_another_site(tmp_path: Path) -> None:
    state = State(adapters={("adapter", "app.py:3"): ("VERIFIED", "old report")})
    suite = Suite("suite", 1, tmp_path, [], site="other.py:7")
    status = _adapters.adapter_status(state, suite, bound_case(), HajerSettings())
    assert status is not None
    assert status[0] == "UNVERIFIED"
    assert "ADAPTER_SITE_MISMATCH" in status[1]


def test_adapter_binding_changes_are_not_cached_by_name_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Path] = []

    def check(root: Path, *_args: object, **_kwargs: object) -> AdapterCheck:
        calls.append(root)
        return AdapterCheck("adapter", None, "app.py:3", "VERIFIED", "reached")

    monkeypatch.setattr(_adapters, "check_adapter", check)
    state, suite = State(), Suite("suite", 1, tmp_path, [], site="app.py:3")
    case = bound_case()
    _adapters.adapter_status(state, suite, case, HajerSettings())
    _adapters.adapter_status(state, suite, case, HajerSettings())
    assert len(calls) == 1
    assert case.execution is not None
    case.execution.adapter["qualname"] = "another"
    _adapters.adapter_status(state, suite, case, HajerSettings())
    assert len(calls) == 2


@pytest.mark.parametrize("site", ["app.py", "app.py:zero", "app.py:0", "../app.py:1", "/outside/app.py:1"])
def test_bad_site_is_refused_without_launching_a_child(tmp_path: Path, site: str) -> None:
    check = check_adapter(tmp_path, {"adapterId": "adapter", "site": site}, environment={}, settings=HajerSettings())
    assert check.status == "UNVERIFIED"
    assert "ADAPTER_SITE_INVALID" in check.detail


def test_overlong_identity_is_not_truncated_into_a_verified_claim(tmp_path: Path) -> None:
    identity = "a" * (CI_TEXT_MAX + 1)
    check = check_adapter(
        tmp_path, {"adapterId": identity, "site": "app.py:1"}, environment={}, settings=HajerSettings()
    )
    assert check.status == "UNVERIFIED"
    assert len(str(check.document()["adapterId"])) == CI_TEXT_MAX
    assert upload_identity(identity) != upload_identity(identity + "b")
