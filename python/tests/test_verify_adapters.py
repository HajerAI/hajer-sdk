"""`python -m hajer verify-adapters` calls each declared adapter once, under the full replay guards with a
fake model, and only a call made from the declared site with nothing refused is VERIFIED.

Each adapter runs in a real child process (the guards patch the socket layer, so never in this one). The model is the
fake's in-process answer to a request for a model endpoint; nothing opens a socket, and a side effect the application
tries is refused before it happens: the subprocess case's marker file is never written.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from pydantic import JsonValue

from hajer.__main__ import upload_results, verify_adapters_command
from hajer._settings import HajerSettings
from hajer._verify_adapters import AdapterCheck, judge, verify_adapters
from hajer.replay._reach import _exception_classes  # pyright: ignore[reportPrivateUsage]

APP = """\
import subprocess

import httpx

client = httpx.Client(base_url="https://api.anthropic.com")


def ask(prompt: str) -> str:
    reply = client.post("/v1/messages", json={"model": "m", "max_tokens": 8, "messages": [prompt]})
    return reply.json()["content"][0]["text"]


def summarise(text: str) -> str:
    return ask("summarise " + text)


def broken(text: str) -> str:
    raise LookupError(text)


def stored(text: str) -> str:
    import psycopg

    return ask(text)


def notified(text: str) -> str:
    httpx.post("https://crm.example.test/log", json={"text": text})
    return ask(text)


def shelled(text: str) -> str:
    subprocess.run(["touch", "MARKER"], check=False)
    return ask(text)
"""
SITE = "app/llm.py:9"
SETTINGS = HajerSettings(ci_case_timeout_seconds=60, adapter_check_answers=4)

STARTUP_APP = """import httpx
ready = False
client = None
def prepare():
    global ready
    ready = True
def install():
    global client
    assert ready
    client = httpx.Client(base_url="https://api.anthropic.com")
class Classifier:
    def __init__(self):
        if client is None:
            raise RuntimeError("startup missing")
    def classify(self, text):
        response = client.post("/v1/messages", json={"model": "m", "max_tokens": 8, "messages": [text]})
        return response.json()["content"][0]["text"]
"""


@pytest.mark.parametrize("with_local_setup", [True, False])
def test_adapter_local_startup_runs_after_global_setup_in_guarded_child(tmp_path: Path, with_local_setup: bool) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app/__init__.py").write_text("")
    (tmp_path / "app/startup.py").write_text(STARTUP_APP)
    (tmp_path / ".hajer").mkdir()
    config = """schema = "hajer-replay-v1"
[setup]
calls = [{call = "app.startup:prepare"}]
[adapters."app.startup:Classifier.classify"]
module = "app.startup"
callable = "Classifier.classify"
constructor = "NO_ARGS"
input = "SINGLE"
site = "app/startup.py:16"
"""
    if with_local_setup:
        config += 'setup = [{call = "app.startup:install"}]\n'
    (tmp_path / ".hajer/replay.toml").write_text(config)
    (check,) = verify_adapters(tmp_path, SETTINGS)
    assert check.status == ("VERIFIED" if with_local_setup else "RUNTIME_UNREACHED"), check


def _adapter(callable_name: str, site: str | None = SITE) -> str:
    lines = [f'[adapters."app.llm:{callable_name}"]', 'module = "app.llm"', f'callable = "{callable_name}"']
    lines += ['constructor = "NONE"', 'input = "SINGLE"', 'verifier = "wf"']
    return "\n".join(lines + ([f'site = "{site}"'] if site else [])) + "\n"


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("")
    (tmp_path / "app" / "llm.py").write_text(APP)
    (tmp_path / ".hajer").mkdir()
    adapters = [
        _adapter("ask"),
        _adapter("summarise", "app/llm.py:13"),
        _adapter("broken"),
        _adapter("stored"),
        _adapter("notified"),
        _adapter("shelled"),
    ]
    config = 'schema = "hajer-replay-v1"\n\n' + "\n".join(adapters)
    config += '\n[adapters."app.llm:nowhere"]\nmodule = "app.llm"\ncallable = "ask"\nconstructor = "NONE"\n'
    (tmp_path / ".hajer" / "replay.toml").write_text(config)
    return tmp_path


def test_only_a_call_made_from_the_declared_site_with_nothing_refused_is_verified(repository: Path) -> None:
    checks = {item.adapter_id: item for item in verify_adapters(repository, SETTINGS)}
    assert checks["app.llm:ask"].status == "VERIFIED", checks["app.llm:ask"]
    # A caller of the function making the call is on the path to the site, not the site.
    summarise = checks["app.llm:summarise"]
    assert (summarise.status, summarise.detail) == (
        "RUNTIME_UNREACHED",
        "the call reached app/llm.py:9, not app/llm.py:13",
    )
    assert checks["app.llm:broken"].status == "RUNTIME_UNREACHED"
    assert "raised builtins.LookupError" in checks["app.llm:broken"].detail
    assert checks["app.llm:nowhere"].status == "UNVERIFIED"


@pytest.mark.parametrize(
    ("endpoint", "route", "status"),
    [
        ("http://127.0.0.1:19283", "/v1/messages", "VERIFIED"),
        ("http://127.0.0.1:19284", "/v1/messages", "RUNTIME_SIDE_EFFECT_REFUSED"),
        ("http://127.0.0.1:19283", "/other", "RUNTIME_SIDE_EFFECT_REFUSED"),
    ],
)
def test_declared_gateway_uses_only_canned_model_routes(
    repository: Path, endpoint: str, route: str, status: str
) -> None:
    """No server runs on the gateway: successful reach is in-process, never network access."""
    (repository / "app/llm.py").write_text(
        APP.replace("https://api.anthropic.com", endpoint).replace("/v1/messages", route)
    )
    config = repository / ".hajer/replay.toml"
    config.write_text(config.read_text() + '\n[provider]\nhost = "127.0.0.1"\nport = 19283\n')
    (check,) = verify_adapters(repository, SETTINGS, only=["app.llm:ask"])
    assert check.status == status, check


@pytest.mark.parametrize(
    ("adapter_id", "refused"),
    [
        ("app.llm:stored", "import:psycopg"),
        ("app.llm:notified", "crm.example.test"),
        ("app.llm:shelled", "process"),
    ],
)
def test_a_side_effect_is_refused_before_it_happens_and_never_reads_as_a_pass(
    repository: Path, adapter_id: str, refused: str
) -> None:
    (check,) = verify_adapters(repository, SETTINGS, only=[adapter_id])
    assert check.status == "RUNTIME_SIDE_EFFECT_REFUSED", check
    assert refused in check.detail
    assert not (repository / "MARKER").exists()


def test_the_command_writes_the_results_and_uploads_them_with_the_run(repository: Path) -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(201)

    settings = HajerSettings(api_key="hjk_fixture", team_id="team", base_url="https://hajer.invalid")
    code = verify_adapters_command(
        repository,
        Path(".hajer/adapter-checks.json"),
        only=["app.llm:ask"],
        project_id="project",
        settings=settings,
        transport=httpx.MockTransport(handler),
    )
    assert code == 0
    written = json.loads((repository / ".hajer" / "adapter-checks.json").read_text())
    assert [item["status"] for item in written["adapters"]] == ["VERIFIED"]
    body = json.loads(sent[0].content)
    assert (body["suites"], body["adapters"]) == ([], written["adapters"])
    assert sent[0].url.path == "/api/teams/team/projects/project/suite-runs"
    assert UUID(written["receiptId"]).version == 4
    assert body["receiptId"] == written["receiptId"]
    assert (
        upload_results(
            repository / ".hajer/adapter-checks.json",
            "project",
            settings=settings,
            transport=httpx.MockTransport(handler),
        )
        == 0
    )
    assert json.loads(sent[1].content) == body


def test_judge_reads_an_entry_refusal_before_anything_else() -> None:
    receipt: dict[str, JsonValue] = {
        "calls": [{"host": "api.anthropic.com", "path": "/v1/messages", "frames": [["app/llm.py", 9]]}],
        "entry": {"reason": "ADAPTER_MISSING", "detail": "factory `db` is an external resource"},
        "refusedHosts": [],
    }
    status, detail = judge(receipt, SITE, frozenset({9}))
    assert (status, "external resource" in detail) == ("RUNTIME_UNREACHED", True)
    assert AdapterCheck("a", None, SITE, status, detail).document()["status"] == "RUNTIME_UNREACHED"


def test_wrapped_setup_failures_keep_causal_types_without_sensitive_messages() -> None:
    inner = ImportError("private customer value")
    outer = RuntimeError("secret wrapped value")
    outer.__cause__ = inner
    assert _exception_classes(outer) == "builtins.RuntimeError caused by builtins.ImportError"
    inner.__cause__ = outer
    assert _exception_classes(outer) == "builtins.RuntimeError caused by builtins.ImportError"
