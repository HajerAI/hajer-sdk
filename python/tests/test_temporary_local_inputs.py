"""Local path permission never replaces exact SDK binding authority, upload refusal or adapter verification."""

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path

import httpx
import pytest
from pydantic import JsonValue

from hajer import _verify_adapters
from hajer.__main__ import verify_adapters_command
from hajer._settings import HajerSettings
from hajer._verify_adapters import AdapterCheck, Authored, check_adapter, verify_adapters
from hajer.pytest_plugin import _adapters, _run
from hajer.pytest_plugin._load import load_suite
from hajer.pytest_plugin._models import Binding, Case, Execution, State, Suite
from hajer.pytest_plugin._run import input_digest
from tests.test_pytest_plugin import build_fixture, run_plugin


def _bundle(tmp_path: Path) -> tuple[Path, Path, dict[str, JsonValue]]:
    suite = tmp_path / "checkout/.hajer/suites/workflow.json"
    suite.parent.mkdir(parents=True)
    suite.write_text(
        json.dumps(
            {
                "suiteId": "suite",
                "version": 1,
                "site": "app.py:4",
                "members": [{"case": {"id": "case"}, "check": {"check": {"id": "check"}}}],
            }
        )
    )
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    digest = "sha256:" + hashlib.sha256(suite.read_bytes()).hexdigest()
    scope = directory / "scope.json"
    scope.write_text(json.dumps({"root": str(suite.parent.parent.parent), "suiteDigest": digest}))
    scope.chmod(0o600)
    binding: dict[str, JsonValue] = {
        "suiteId": "suite",
        "suiteVersion": 1,
        "suiteDigest": digest,
        "cases": {"case": {"adapter": {}, "input": {"private": "PRIVATE_SENTINEL"}, "boundaries": []}},
    }
    return suite, directory, binding


def _write(directory: Path, name: str, value: dict[str, JsonValue]) -> None:
    target = directory / name
    target.write_text(json.dumps(value))
    target.chmod(0o600)


def test_override_is_default_off_and_preserves_source_root(tmp_path: Path) -> None:
    suite, directory, binding = _bundle(tmp_path)
    _write(directory, suite.name, binding)
    assert load_suite(suite).cases[0].execution is None
    loaded = load_suite(suite, temporary=directory)
    assert loaded.root == suite.parent.parent.parent
    assert loaded.cases[0].execution is not None


@pytest.mark.parametrize(
    ("field", "value"), [("suiteId", "other"), ("suiteVersion", 2), ("suiteDigest", "other"), ("cases", {})]
)
def test_stale_cross_suite_and_missing_case_refuse(tmp_path: Path, field: str, value: JsonValue) -> None:
    suite, directory, binding = _bundle(tmp_path)
    binding[field] = value
    _write(directory, suite.name, binding)
    with pytest.raises(ValueError, match="BINDING_MISMATCH"):
        load_suite(suite, temporary=directory)


def test_cross_checkout_scope_and_symlink_refuse(tmp_path: Path) -> None:
    suite, directory, binding = _bundle(tmp_path)
    _write(directory, suite.name, binding)
    _write(directory, "scope.json", {"root": "other", "suiteDigest": binding["suiteDigest"]})
    with pytest.raises(ValueError, match="SCOPE_MISMATCH"):
        load_suite(suite, temporary=directory)
    (directory / "scope.json").unlink()
    (directory / "scope.json").symlink_to(suite)
    with pytest.raises(OSError, match=r".*"):
        load_suite(suite, temporary=directory)


def test_validation_failure_does_not_quote_private_input(tmp_path: Path) -> None:
    suite, directory, binding = _bundle(tmp_path)
    binding["cases"] = {"case": {"PRIVATE_SENTINEL": "not an Execution"}}
    _write(directory, suite.name, binding)
    with pytest.raises(ValueError, match="BINDING_MISMATCH") as error:
        load_suite(suite, temporary=directory)
    assert "PRIVATE_SENTINEL" not in str(error.value)


# ── Upload refusal, filesystem refusals, private attempt copies, adapter verification ─────────────────


def _bound(tmp_path: Path) -> tuple[Path, Path]:
    suite, directory, binding = _bundle(tmp_path)
    _write(directory, suite.name, binding)
    return suite, directory


def test_upload_is_refused_before_any_case_runs(tmp_path: Path) -> None:
    suite, directory = _bound(tmp_path)
    root = suite.parent.parent.parent
    result = run_plugin(
        root,
        "--hajer-temporary-local-executions",
        str(directory),
        "--hajer-upload",
        "--hajer-project-id",
        "project",
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "Temporary local proof execution cannot upload" in result.stdout + result.stderr
    assert "PRIVATE_SENTINEL" not in result.stdout + result.stderr
    assert not (root / "results.json").exists()


def test_a_directory_that_is_not_owner_only_refuses(tmp_path: Path) -> None:
    suite, directory = _bound(tmp_path)
    directory.chmod(0o755)
    with pytest.raises(ValueError, match="TEMPORARY_LOCAL_INPUT_DIRECTORY_REFUSED"):
        load_suite(suite, temporary=directory)


def test_a_binding_readable_by_others_refuses(tmp_path: Path) -> None:
    suite, directory = _bound(tmp_path)
    (directory / suite.name).chmod(0o644)
    with pytest.raises(ValueError, match="TEMPORARY_LOCAL_INPUT_FILE_REFUSED"):
        load_suite(suite, temporary=directory)


def test_a_hard_linked_binding_refuses(tmp_path: Path) -> None:
    suite, directory = _bound(tmp_path)
    os.link(directory / suite.name, tmp_path / "second-name")
    with pytest.raises(ValueError, match="TEMPORARY_LOCAL_INPUT_FILE_REFUSED"):
        load_suite(suite, temporary=directory)


def test_a_bundle_under_a_git_ancestor_or_inside_the_checkout_refuses(tmp_path: Path) -> None:
    suite, directory = _bound(tmp_path)
    (tmp_path / ".git").mkdir()
    with pytest.raises(ValueError, match="TEMPORARY_LOCAL_INPUT_PUBLICATION_PATH_REFUSED"):
        load_suite(suite, temporary=directory)
    (tmp_path / ".git").rmdir()
    inside = suite.parent.parent.parent / "private"
    directory.rename(inside)
    with pytest.raises(ValueError, match="TEMPORARY_LOCAL_INPUT_PUBLICATION_PATH_REFUSED"):
        load_suite(suite, temporary=inside)


def test_each_attempt_copy_of_an_input_is_owner_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    modes: list[int] = []

    def child(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        # _boot.py spec receipt egress root mode: the spec is the attempt's copy of the case's input.
        modes.append(stat.S_IMODE(os.stat(argv[5]).st_mode))
        return subprocess.CompletedProcess(argv, 1, b"", b"")

    monkeypatch.setattr(_run.subprocess, "run", child)
    case = Case("case", [], Execution(adapter={}, input={"private": "PRIVATE_SENTINEL"}, boundaries=[]))
    attempt = _run.execute(Suite("suite", 1, tmp_path, [case]), case, live=False, settings=HajerSettings())
    assert attempt.reason == "CHILD_FAILED"
    assert modes == [0o600]

    def reach(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        modes.append(stat.S_IMODE(os.stat(argv[5]).st_mode))
        return subprocess.CompletedProcess(argv, 1, b"", b"")

    monkeypatch.setattr(_verify_adapters.subprocess, "run", reach)
    (tmp_path / "app.py").write_text("def ask(value):\n    return value\n")
    check = check_adapter(
        tmp_path,
        {"adapterId": "adapter", "module": "app", "qualname": "ask", "site": "app.py:1"},
        environment={},
        settings=HajerSettings(),
        authored=Authored({"private": "PRIVATE_SENTINEL"}),
    )
    assert check.status == "UNVERIFIED"
    assert modes == [0o600, 0o600]


def test_in_session_verification_of_a_temporary_suite_uses_each_cases_own_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[object] = []

    def check(
        root: Path,
        adapter: dict[str, JsonValue],
        *,
        environment: object,
        settings: object,
        authored: Authored | None = None,
    ) -> AdapterCheck:
        seen.append("NO_AUTHORED_INPUT" if authored is None else authored.value)
        return AdapterCheck("adapter", None, "app.py:3", "VERIFIED", "reached")

    monkeypatch.setattr(_adapters, "check_adapter", check)

    def case(value: JsonValue) -> Case:
        adapter: dict[str, JsonValue] = {"adapterId": "adapter", "module": "app", "qualname": "ask", "site": "app.py:3"}
        return Case("case", [], Execution(adapter=adapter, input=value, boundaries=[]))

    temporary = Suite("suite", 1, tmp_path, [], site="app.py:3", temporary=True)
    state = State()
    for value in ("first", "first", "second"):
        assert _adapters.adapter_status(state, temporary, case(value), HajerSettings()) == ("VERIFIED", "reached")
    # Verified again for every distinct input, never skipped, never borrowed from another case's input.
    assert seen == ["first", "second"]
    # Normal mode is unchanged: no authored input is handed over, so the check reads a committed binding or stand-in.
    committed = Suite("suite", 1, tmp_path, [], site="app.py:3")
    _adapters.adapter_status(State(), committed, case("first"), HajerSettings())
    assert seen[-1] == "NO_AUTHORED_INPUT"


GATED_APP = """\
import httpx

client = httpx.Client(base_url="https://api.anthropic.com")


def gated(value):
    if value != {"ticket": "authored"}:
        raise LookupError("not the authored input")
    reply = client.post("/v1/messages", json={"model": "m", "max_tokens": 8, "messages": [value["ticket"]]})
    return reply.json()["content"][0]["text"]
"""
GATED_SITE = "app/llm.py:9"
CHECK_SETTINGS = HajerSettings(ci_case_timeout_seconds=60, adapter_check_answers=4)


def _gated_checkout(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A checkout whose one adapter reaches its site only with the authored input, and a bundle binding it."""
    root = tmp_path / "checkout"
    (root / "app").mkdir(parents=True)
    (root / "app" / "__init__.py").write_text("")
    (root / "app" / "llm.py").write_text(GATED_APP)
    table = [
        '[adapters."app.llm:gated"]',
        'module = "app.llm"',
        'callable = "gated"',
        'constructor = "NONE"',
        'input = "SINGLE"',
        'verifier = "wf"',
        f'site = "{GATED_SITE}"',
    ]
    (root / ".hajer").mkdir()
    (root / ".hajer" / "replay.toml").write_text('schema = "hajer-replay-v1"\n\n' + "\n".join(table) + "\n")
    suite = root / ".hajer" / "suites" / "workflow.json"
    suite.parent.mkdir()
    suite.write_text(
        json.dumps(
            {
                "suiteId": "suite",
                "version": 1,
                "site": GATED_SITE,
                "members": [{"case": {"id": "case"}, "check": {"check": {"id": "check"}}}],
            }
        )
    )
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    digest = "sha256:" + hashlib.sha256(suite.read_bytes()).hexdigest()
    _write(directory, "scope.json", {"root": str(root.resolve()), "suiteDigest": digest})
    adapter: dict[str, JsonValue] = {
        "adapterId": "app.llm:gated",
        "module": "app.llm",
        "qualname": "gated",
        "constructor": "NONE",
        "input": "SINGLE",
        "site": GATED_SITE,
    }
    execution: dict[str, JsonValue] = {"adapter": adapter, "input": {"ticket": "authored"}, "boundaries": []}
    binding: dict[str, JsonValue] = {
        "suiteId": "suite",
        "suiteVersion": 1,
        "suiteDigest": digest,
        "cases": {"case": execution},
    }
    _write(directory, suite.name, binding)
    return root, suite, directory


def test_the_real_check_observes_the_authored_input_not_a_signature_stand_in(tmp_path: Path) -> None:
    root, suite, directory = _gated_checkout(tmp_path)
    adapter: dict[str, JsonValue] = {"adapterId": "app.llm:gated", "module": "app.llm", "qualname": "gated"}
    adapter |= {"constructor": "NONE", "input": "SINGLE", "site": GATED_SITE}
    authored = check_adapter(
        root, adapter, environment={}, settings=CHECK_SETTINGS, authored=Authored({"ticket": "authored"})
    )
    assert authored.status == "VERIFIED", authored
    standin = check_adapter(root, adapter, environment={}, settings=CHECK_SETTINGS)
    assert standin.status == "RUNTIME_UNREACHED", standin
    assert "LookupError" in standin.detail

    # The standalone step reads the same bundle the plugin reads: verified on the case's own input.
    (checked,) = verify_adapters(root, CHECK_SETTINGS, temporary=directory)
    assert (checked.adapter_id, checked.status) == ("app.llm:gated", "VERIFIED"), checked
    # Without the bundle nothing changed: the signature's stand-in, which this adapter refuses.
    (normal,) = verify_adapters(root, CHECK_SETTINGS)
    assert normal.status == "RUNTIME_UNREACHED", normal
    # A loaded temporary suite carries the mode, so the plugin hands each case's own input to the same check.
    loaded = load_suite(suite, temporary=directory)
    assert loaded.temporary
    assert not load_suite(suite).temporary


def test_verify_adapters_never_uploads_a_temporary_run_and_names_a_refused_bundle(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, suite, directory = _gated_checkout(tmp_path)

    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"a temporary local proof run uploaded to {request.url}")

    settings = HajerSettings(api_key="hjk_fixture", team_id="team", base_url="https://hajer.invalid")
    results = tmp_path / "checks.json"
    code = verify_adapters_command(
        root,
        results,
        only=[],
        project_id="project",
        settings=settings,
        transport=httpx.MockTransport(refuse),
        temporary=directory,
    )
    assert code == 2
    assert not results.exists()
    assert "cannot upload" in capsys.readouterr().err

    (directory / suite.name).chmod(0o644)
    code = verify_adapters_command(root, results, only=[], project_id=None, settings=settings, temporary=directory)
    assert code == 2
    error = capsys.readouterr().err
    assert "TEMPORARY_LOCAL_INPUT_FILE_REFUSED" in error
    assert "authored" not in error


# ── What each attempt executed, and each case's own adapter check ──────────────────────────────────────────────

TWO_CASE_APP = """\
import httpx

client = httpx.Client(base_url="https://api.anthropic.com")


def ask(value):
    reply = client.post("/v1/messages", json={"model": "m", "max_tokens": 8, "messages": [value["ticket"]]})
    return reply.json()["content"][0]["text"]
"""
TWO_CASE_SITE = "app/llm.py:7"


def _canonical_digest(value: JsonValue) -> str:
    """The backend's custody digest (`request_digest`, `hajer-canonical-request-v1`), computed independently here."""
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _two_case_checkout(tmp_path: Path, inputs: dict[str, JsonValue]) -> tuple[Path, Path]:
    """A checkout whose one adapter reaches its site with any input carrying a ticket, and a bundle binding one input
    per case: the adapter is checked once per case, each on that case's own input."""
    root = tmp_path / "checkout"
    (root / "app").mkdir(parents=True)
    (root / "app" / "__init__.py").write_text("")
    (root / "app" / "llm.py").write_text(TWO_CASE_APP)
    table = [
        '[adapters."app.llm:ask"]',
        'module = "app.llm"',
        'callable = "ask"',
        'constructor = "NONE"',
        'input = "SINGLE"',
        'verifier = "wf"',
        f'site = "{TWO_CASE_SITE}"',
    ]
    (root / ".hajer").mkdir()
    (root / ".hajer" / "replay.toml").write_text('schema = "hajer-replay-v1"\n\n' + "\n".join(table) + "\n")
    suite = root / ".hajer" / "suites" / "workflow.json"
    suite.parent.mkdir()
    members = [{"case": {"id": case}, "check": {"check": {"id": f"check-{case}"}}} for case in inputs]
    suite.write_text(json.dumps({"suiteId": "suite", "version": 1, "site": TWO_CASE_SITE, "members": members}))
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    digest = "sha256:" + hashlib.sha256(suite.read_bytes()).hexdigest()
    _write(directory, "scope.json", {"root": str(root.resolve()), "suiteDigest": digest})
    adapter: dict[str, JsonValue] = {
        "adapterId": "app.llm:ask",
        "module": "app.llm",
        "qualname": "ask",
        "constructor": "NONE",
        "input": "SINGLE",
        "site": TWO_CASE_SITE,
    }
    cases: dict[str, JsonValue] = {
        case: {"adapter": adapter, "input": value, "boundaries": []} for case, value in inputs.items()
    }
    _write(directory, suite.name, {"suiteId": "suite", "suiteVersion": 1, "suiteDigest": digest, "cases": cases})
    return root, directory


def test_each_attempt_records_only_the_digest_of_the_input_it_executed(tmp_path: Path) -> None:
    first, second = f"PRIVATE_SENTINEL_{'a' * 8}", f"PRIVATE_SENTINEL_{'b' * 8}"
    inputs: dict[str, JsonValue] = {"one": {"ticket": first}, "two": {"ticket": second, "note": "ü"}}
    root, directory = _two_case_checkout(tmp_path, inputs)
    result = run_plugin(root, "--hajer-temporary-local-executions", str(directory))
    raw = (root / "results.json").read_text()
    payload = json.loads(raw)
    cases = {case["caseId"]: case for case in payload["suites"][0]["cases"]}
    assert set(cases) == {"one", "two"}, result.stdout + result.stderr
    for case_id, value in inputs.items():
        evidence = cases[case_id]["executionEvidence"]
        assert evidence, cases[case_id]
        # Every attempt names the digest of exactly the input its child was handed, in the custody's own namespace.
        assert {item["inputDigest"] for item in evidence} == {_canonical_digest(value)}
    # Digests only: no input byte reaches the receipt, the terminal or the summary.
    for text in (raw, result.stdout, result.stderr):
        assert first not in text
        assert second not in text


def test_a_changed_input_byte_changes_the_executed_digest(tmp_path: Path) -> None:
    original: JsonValue = {"ticket": "PRIVATE_SENTINEL_cafe"}
    changed: JsonValue = {"ticket": "PRIVATE_SENTINEL_cafd"}
    root, directory = _two_case_checkout(tmp_path, {"one": changed})
    run_plugin(root, "--hajer-temporary-local-executions", str(directory))
    (case,) = json.loads((root / "results.json").read_text())["suites"][0]["cases"]
    executed = {item["inputDigest"] for item in case["executionEvidence"]}
    assert executed == {_canonical_digest(changed)}
    assert _canonical_digest(original) not in executed


def test_each_temporary_case_carries_its_own_adapter_check(tmp_path: Path) -> None:
    # The session keeps one adapter result per adapter and site (the last case's), so each case that ran in
    # temporary mode, checked on its own input, also names its own check in the receipt.
    root, directory = _two_case_checkout(tmp_path, {"one": {"ticket": "a"}, "two": {"ticket": "b"}})
    result = run_plugin(root, "--hajer-temporary-local-executions", str(directory))
    payload = json.loads((root / "results.json").read_text())
    assert len(payload["adapters"]) == 1, payload["adapters"]
    cases = {case["caseId"]: case for case in payload["suites"][0]["cases"]}
    assert set(cases) == {"one", "two"}, result.stdout + result.stderr
    for case in cases.values():
        check = case["adapterCheck"]
        assert (check["adapterId"], check["site"], check["status"]) == ("app.llm:ask", TWO_CASE_SITE, "VERIFIED")


def test_normal_mode_keeps_the_sessions_adapter_list_only(tmp_path: Path) -> None:
    # One check per adapter and site authorizes every case of a committed binding: no per-case entry is added.
    build_fixture(tmp_path)
    run_plugin(tmp_path)
    payload = json.loads((tmp_path / "results.json").read_text())
    cases = payload["suites"][0]["cases"]
    assert cases
    assert all("adapterCheck" not in case for case in cases)
    assert payload["adapters"]


# The Hajer platform pins the same constant for the same value in its own tests (`request_digest`): the two
# canonical forms cannot drift apart unseen.
CANONICAL_SAMPLE: dict[str, JsonValue] = {
    "tenth": 0.1,
    "big": 1e16,
    "huge": 1e21,
    "huger": 1e22,
    "tiny": 5e-324,
    "max": 1.7976931348623157e308,
    "negativeZero": -0.0,
    "hundred": 100.0,
    "int": 2**70,
    "negative": -12,
    # LINE SEPARATOR (U+2028) is written by its code point: a JSON dumper must keep it raw, never escape it.
    "text": f'ü {chr(0x2028)} é \U0001f600 "quoted" \\ /',
    "nested": {"b": [1, 2.5, {"z": "ñ"}], "a": None, "t": True},
}
CANONICAL_PINNED = "sha256:53e130684bf0388066ddc607cfd7de7ea1c36332e6fada6a4e7932a3674eb108"


def test_the_executed_input_digest_agrees_with_the_custody_digest_for_numbers_and_unicode() -> None:
    """The digest of what a child is handed, through the delivery path (the product writes the binding with
    `json.dumps(sort_keys=True)`, ASCII-escaped; the plugin validates it and hands the child `model_dump_json`)."""
    binding: dict[str, JsonValue] = {
        "suiteId": "s",
        "suiteVersion": 1,
        "suiteDigest": "d",
        "cases": {"c": {"adapter": {}, "input": CANONICAL_SAMPLE, "boundaries": []}},
    }
    delivered = Binding.model_validate_json(json.dumps(binding, sort_keys=True))
    assert input_digest(delivered.cases["c"].model_dump_json()) == CANONICAL_PINNED
    assert input_digest(json.dumps({"input": CANONICAL_SAMPLE}, ensure_ascii=False)) == CANONICAL_PINNED
    assert _canonical_digest(CANONICAL_SAMPLE) == CANONICAL_PINNED
    changed = {**CANONICAL_SAMPLE, "hundred": 100}
    assert input_digest(json.dumps({"input": changed})) != CANONICAL_PINNED
