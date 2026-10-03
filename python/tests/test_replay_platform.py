"""The replay child's `platform` module answers from the parent's recorded values, never a process.

Each case runs in a fresh interpreter, because installing the guard patches httpx and the process layer for the rest
of the process it runs in (`test_replay.py`). The child primes `platform` from recorded values, installs the real
guard, then makes the calls a model SDK client makes for its platform headers and the rest of the introspection
surface; only then does it run `uname -p` itself, which must still be refused. The end-to-end path — the platform's
runner, the OS sandbox and a stand-in client reaching a recorded boundary — is covered by the Hajer
platform's own tests.
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
from pathlib import Path
from typing import cast

from pydantic import JsonValue

SDK = Path(__file__).resolve().parents[1]
# The parent records no hostname; the child's `node` is always the fixed `hajer-replay`.
RECORDED: dict[str, JsonValue] = {
    "system": "Linux",
    "release": "6.8.0-45-generic",
    "version": "#45-Ubuntu SMP PREEMPT_DYNAMIC",
    "machine": "x86_64",
    "processor": "x86_64",
    "platform": "Linux-6.8.0-45-generic-x86_64-with-glibc2.39",
    "platformTerse": "Linux-6.8.0-45-generic-x86_64-with-glibc",
}
CHILD = """
import json
import platform
import subprocess
import sys
from pathlib import Path

# Every process event from the first line on, the priming included: nothing may start before the guard either.
events = []
sys.addaudithook(lambda event, _args: events.append(event) if event in {
    "subprocess.Popen", "os.system", "os.posix_spawn", "os.spawn", "os.exec", "os.fork", "os.forkpty"} else None)

from hajer.replay._guard import Sandbox
from hajer.replay._hooks import install
from hajer.replay._platform import prime

answered = prime(json.loads(sys.argv[1]))
sandbox = Sandbox(provider=None, hajer=None, recordings={}, egress_log=Path(sys.argv[2]))
install(sandbox)
seen = {
    "system": platform.system(),
    "node": platform.node(),
    "release": platform.release(),
    "version": platform.version(),
    "machine": platform.machine(),
    "processor": platform.processor(),
    "uname": list(platform.uname()),
    "platform": platform.platform(),
    "aliased": platform.platform(aliased=True),
    "terse": platform.platform(terse=True),
    "runtime": [platform.python_implementation(), platform.python_version()],
}
introspection = list(sandbox.record.refusals)
started = list(events)
try:
    subprocess.run(["uname", "-p"], check=False, capture_output=True)
    spawned = "ran"
except PermissionError:
    spawned = "refused"
print(json.dumps({"answered": answered, "seen": seen, "introspection": introspection, "started": started,
                  "refusals": sandbox.record.refusals, "spawned": spawned}))
"""


def child(tmp_path: Path, recorded: JsonValue) -> dict[str, JsonValue]:
    completed = subprocess.run(  # noqa: S603 - this interpreter, a fixed script, owned arguments
        [sys.executable, "-c", CHILD, json.dumps(recorded), str(tmp_path / "egress.jsonl")],
        cwd=SDK,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return cast(dict[str, JsonValue], json.loads(completed.stdout))


def test_platform_answers_the_recorded_values_without_a_process(tmp_path: Path) -> None:
    report = child(tmp_path, RECORDED)
    assert report["started"] == []
    answered = {**RECORDED, "node": "hajer-replay"}
    uname = [answered[name] for name in ("system", "node", "release", "version", "machine", "processor")]
    assert report["answered"] == answered
    seen = cast(dict[str, JsonValue], report["seen"])
    assert [seen[name] for name in ("system", "node", "release", "version", "machine", "processor")] == uname
    assert seen["uname"] == uname
    assert (seen["platform"], seen["aliased"], seen["terse"]) == (
        RECORDED["platform"],
        RECORDED["platform"],
        RECORDED["platformTerse"],
    )
    assert report["introspection"] == []


def test_a_hostname_in_the_spec_is_never_answered(tmp_path: Path) -> None:
    report = child(tmp_path, {**RECORDED, "node": "worker-7.internal"})
    seen = cast(dict[str, JsonValue], report["seen"])
    assert (seen["node"], cast(list[JsonValue], seen["uname"])[1]) == ("hajer-replay", "hajer-replay")
    assert "worker-7.internal" not in json.dumps(report)


def test_priming_opens_no_spawn_exemption(tmp_path: Path) -> None:
    report = child(tmp_path, RECORDED)
    assert (report["spawned"], report["refusals"]) == ("refused", ["BLOCKED_EFFECT"])


def test_without_recorded_values_the_child_seeds_this_hosts_answers_itself_without_a_process(tmp_path: Path) -> None:
    """SDK replay runs in the customer's CI, where no parent records the values.
    `prime` computes them in-process before the guard is armed; a direct spawn is still BLOCKED_EFFECT."""
    host = platform.uname()
    for recorded in (None, {**RECORDED, "processor": 7}):
        report = child(tmp_path, recorded)
        seen = cast(dict[str, JsonValue], report["seen"])
        assert report["started"] == []
        assert report["introspection"] == []
        assert (seen["processor"], seen["node"]) == (host.processor, "hajer-replay")
        assert [seen[name] for name in ("system", "release", "version", "machine")] == [
            host.system,
            host.release,
            host.version,
            host.machine,
        ]
        assert (seen["platform"], seen["terse"]) == (platform.platform(), platform.platform(terse=True))
        assert report["answered"] == {
            "system": host.system,
            "release": host.release,
            "version": host.version,
            "machine": host.machine,
            "processor": host.processor,
            "platform": platform.platform(),
            "platformTerse": platform.platform(terse=True),
            "node": "hajer-replay",
        }
        assert (report["spawned"], report["refusals"]) == ("refused", ["BLOCKED_EFFECT"])
