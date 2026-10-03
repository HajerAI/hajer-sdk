"""Execute the real app in customer CI with SDK replay, not a recorded output substitute.

Both modes run under the replay's guards (`_guard.Sandbox`, `_hooks.install`, `_database`): a database driver, a
process and every socket or name lookup are refused. In replay mode the model's answers are the recorded ones and
nothing leaves; in live mode the one place a byte may go is the model provider, reached directly: the `[provider]`
`.hajer/replay.toml` declares (`host`, `port`, and `scheme` when it is not the port's own — https on 443, http on any
other port), else the model APIs the SDK wraps (`LIVE_PROVIDERS`). Only a request addressed to that host, port and
scheme is sent, and only to an address the provider's name resolves to, at that port (`_guard._Window`): a proxy that
is not the provider, a Unix socket or a transport's own socket is refused at the connect. An adapter now drives a
function near its model call, and in a live run that function must not write to the customer's database, send an
email or call another paid API. Whatever was refused makes the attempt fail closed, naming it
(RUNTIME_SIDE_EFFECT_REFUSED); it is never a pass.
"""

import asyncio
import inspect
import json
import sys
import tomllib
from collections.abc import Coroutine
from pathlib import Path
from typing import Final, cast

from pydantic import JsonValue

from hajer._ci_spend import Spend
from hajer._settings import HajerSettings, clipped
from hajer.pytest_plugin._models import Attempt, Execution
from hajer.pytest_plugin._request_fingerprint import RequestFingerprint
from hajer.replay._capture import subject_of
from hajer.replay._case import as_json
from hajer.replay._database import install_database_guard
from hajer.replay._entry import prepare_call
from hajer.replay._guard import Endpoint, Sandbox, recordings_from
from hajer.replay._hooks import install
from hajer.replay._platform import prime

#: The model APIs a live run may reach when `.hajer/replay.toml` declares no `[provider]`: the ones the SDK wraps.
LIVE_PROVIDERS: Final = (
    Endpoint(host="api.openai.com", port=443, scheme="https"),
    Endpoint(host="api.anthropic.com", port=443, scheme="https"),
)
#: What `[provider].scheme` may say; absent (empty) means the port's own.
_SCHEMES: Final = frozenset({"", "http", "https"})


def live_providers(root: Path) -> tuple[Endpoint, ...]:
    """The declared `[provider]` of the suite's repository, else `LIVE_PROVIDERS`."""
    try:
        provider = tomllib.loads((root / ".hajer" / "replay.toml").read_text(encoding="utf-8")).get("provider")
    except (OSError, ValueError):
        return LIVE_PROVIDERS
    if not isinstance(provider, dict):
        return LIVE_PROVIDERS
    table = cast(dict[str, object], provider)
    host, port, scheme = table.get("host"), table.get("port"), table.get("scheme", "")
    if isinstance(host, str) and isinstance(port, int) and isinstance(scheme, str) and scheme in _SCHEMES:
        return (Endpoint(host=host.lower(), port=port, scheme=scheme),)
    return LIVE_PROVIDERS


def _refused(egress: Path) -> list[str]:
    events = [cast(dict[str, JsonValue], json.loads(line)) for line in egress.read_text().splitlines() if line.strip()]
    return sorted({str(item.get("host")) for item in events if not item.get("allowed") and not item.get("served")})


def run(argv: list[str]) -> None:
    spec, receipt, egress, root, mode = argv
    execution = Execution.model_validate_json(Path(spec).read_bytes())
    # Before the guard: `platform` answers from memory, so a provider client's platform headers start no process.
    prime(None)
    spend = Spend(HajerSettings.from_env()) if mode == "live" else None
    fingerprint = RequestFingerprint()
    sandbox = Sandbox(
        provider=None,
        hajer=None,
        recordings=recordings_from(cast(list[JsonValue], execution.boundaries)),
        egress_log=Path(egress),
        direct=live_providers(Path(root)) if mode == "live" else (),
        spend=spend,
        observer=fingerprint.observe,
    )
    install(sandbox)
    install_database_guard(sandbox)
    sys.path.insert(0, root)
    attempt = Attempt(mode="live" if mode == "live" else "replay")
    try:
        prepared = prepare_call(execution.adapter, execution.input)
        result = prepared.target(*prepared.args, **prepared.kwargs)
        if inspect.isawaitable(result):
            result = asyncio.run(cast(Coroutine[object, object, object], result))
        attempt.output = as_json(result)
    except Exception as error:  # noqa: BLE001 - customer exceptions are unavailable evidence; never include their text
        attempt.reason = type(error).__name__
    _, attempt.reply = subject_of([*sandbox.record.served_bodies, *sandbox.record.provider_bodies])
    if sandbox.record.refusals:
        attempt.reason = clipped(f"RUNTIME_SIDE_EFFECT_REFUSED: {', '.join(_refused(Path(egress)))}")
    elif sandbox.record.weak_matches:
        attempt.reason = "REPLAY_REFUSED_OR_WEAK_MATCH"
    if mode == "replay" and not sandbox.record.refusals:
        served = [json.loads(line).get("served") for line in Path(egress).read_text().splitlines()]
        if not any(served):
            attempt.reason = "NO_RECORDED_MODEL_ANSWER_USED"
    attempt.provider_successes = sum(200 <= status < 300 for status in sandbox.record.provider_statuses)
    attempt.request_fingerprint = fingerprint.digest()
    if spend and spend.reason:
        attempt.reason = spend.reason
    Path(receipt).write_text(attempt.model_dump_json())
