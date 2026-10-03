"""A declared FACTORY is the application's own construction, its model client wrapped, its external
resources refused by name, and a JSON input read as the types the entry declares. Nothing here installs a guard."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import JsonValue

from hajer.replay._entry import EntryUnavailable, prepare_call, target_of
from hajer.replay._standin import TEXT, standin

APP = """\
import dataclasses
import enum

from pydantic import BaseModel


class Tone(enum.Enum):
    PLAIN = "plain"
    LOUD = "loud"


class Brief(BaseModel):
    topic: str
    tone: Tone
    words: int = 50


@dataclasses.dataclass
class Settings:
    model: str = "fake-model"


settings = Settings()


class Client:
    def __init__(self, model: str) -> None:
        self.model = model


def default_client() -> Client:
    return Client(model="default")


class Writer:
    def __init__(self, llm: Client, tone: str, retries: int = 2) -> None:
        self.llm = llm
        self.tone = tone

    def write(self, brief: Brief) -> dict[str, str]:
        return {"model": self.llm.model, "tone": self.tone, "topic": brief.topic, "brief_tone": brief.tone.value}


class Stored:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    def load(self, key: str) -> str:
        return key


def label(topic: str, llm: Client) -> str:
    return f"{llm.model}:{topic}"


def fetch(key: str, session: AsyncSession) -> str:
    return key


class _Holder:
    client: Client | None = None


def install(client: Client) -> None:
    _Holder.client = client


def installed(topic: str) -> str:
    if _Holder.client is None:
        raise RuntimeError("not installed")
    return f"{_Holder.client.model}:{topic}"
"""


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    name = f"adapter_app_{abs(hash(tmp_path))}"
    (tmp_path / f"{name}.py").write_text(APP.replace("AsyncSession", "'AsyncSession'"))
    monkeypatch.setattr(sys, "path", [str(tmp_path), *sys.path])
    yield name
    sys.modules.pop(name, None)


def _writer(module: str) -> dict[str, JsonValue]:
    return {
        "adapterId": f"{module}:Writer.write",
        "module": module,
        "qualname": "Writer.write",
        "constructor": "FACTORY",
        "factory": f"{module}:Writer",
        "factoryArguments": {
            "llm": {
                "construct": f"{module}:Client",
                "arguments": {"model": {"name": f"{module}:settings.model"}},
                "model_client": True,
            },
            "tone": {"value": "calm"},
        },
        "input": "SINGLE",
        "fields": {},
    }


def test_a_factory_is_built_from_its_recipes_and_the_input_read_as_the_declared_model(app: str) -> None:
    prepared = prepare_call(_writer(app), {"topic": "ships", "tone": "loud"})
    assert prepared.target(*prepared.args, **prepared.kwargs) == {
        "model": "fake-model",
        "tone": "calm",
        "topic": "ships",
        "brief_tone": "loud",
    }


def test_an_input_that_does_not_read_as_the_declared_type_is_parse_named(app: str) -> None:
    with pytest.raises(EntryUnavailable) as refused:
        prepare_call(_writer(app), {"topic": "ships", "tone": "whisper"})
    assert refused.value.reason == "PARSE"
    assert "input `brief`" in str(refused.value)


def test_a_bound_model_client_is_passed_beside_the_authored_input(app: str) -> None:
    adapter: dict[str, JsonValue] = {
        "module": app,
        "qualname": "label",
        "constructor": "NONE",
        "input": "SINGLE",
        "fields": {},
        "arguments": {"llm": {"call": f"{app}:default_client", "model_client": True}},
    }
    prepared = prepare_call(adapter, "ships")
    assert prepared.target(*prepared.args, **prepared.kwargs) == "default:ships"


@pytest.mark.parametrize(
    ("adapter", "named"),
    [
        (
            {"qualname": "Stored.load", "constructor": "FACTORY", "factory": "{app}:Stored", "input": "SINGLE"},
            "factory Stored: `db` is an external resource",
        ),
        ({"qualname": "fetch", "constructor": "NONE", "input": "SINGLE"}, "fetch: `session` is an external resource"),
    ],
)
def test_an_external_resource_is_refused_by_name(app: str, adapter: dict[str, str], named: str) -> None:
    document: dict[str, JsonValue] = {"module": app, "fields": {}}
    document |= {key: value.replace("{app}", app) for key, value in adapter.items()}
    with pytest.raises(EntryUnavailable) as refused:
        prepare_call(document, "key")
    assert refused.value.reason == "ADAPTER_MISSING"
    assert named in str(refused.value)


def test_a_recipe_naming_nothing_the_application_binds_is_adapter_missing(app: str) -> None:
    adapter = _writer(app)
    adapter["factoryArguments"] = {"llm": {"call": f"{app}:no_such_provider"}, "tone": {"value": "calm"}}
    with pytest.raises(EntryUnavailable) as refused:
        prepare_call(adapter, {"topic": "ships", "tone": "loud"})
    assert refused.value.reason == "ADAPTER_MISSING"


def test_a_stand_in_input_carries_every_required_field_of_the_declared_types(app: str) -> None:
    writer = _writer(app)
    assert standin(writer, target_of(writer)) == {"topic": TEXT, "tone": "plain"}
    keyword: dict[str, JsonValue] = {"module": app, "qualname": "label", "constructor": "NONE", "input": "KWARGS"}
    keyword["arguments"] = {"llm": {"call": f"{app}:default_client"}}
    assert standin(keyword, target_of(keyword)) == {"topic": TEXT}


def test_declared_setup_runs_before_the_adapter_is_built(app: str) -> None:
    adapter: dict[str, JsonValue] = {"module": app, "qualname": "installed", "constructor": "NONE", "input": "SINGLE"}
    adapter["fields"] = {}
    unset = prepare_call(adapter, "ships")
    with pytest.raises(RuntimeError, match="not installed"):
        unset.target(*unset.args, **unset.kwargs)
    client: JsonValue = {"construct": f"{app}:Client", "arguments": {"model": {"value": "set-up"}}}
    install: JsonValue = {"construct": f"{app}:install", "arguments": {"client": client}}
    adapter["setup"] = [install]
    prepared = prepare_call(adapter, "ships")
    assert prepared.target(*prepared.args, **prepared.kwargs) == "set-up:ships"
