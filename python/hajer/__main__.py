"""`hajer` (also `python -m hajer`) — the five things an operator does from a shell.

    hajer attach-path     # the directory to put on PYTHONPATH to attach with no code change
    hajer tail --follow   # one line per observation this team has recorded, as they arrive
    hajer doctor [--json] # what this process's SDK is configured to do, and whether it can
    hajer proxy --upstream https://api.anthropic.com --listen 127.0.0.1:8091
    hajer eval [-c promptfooconfig.yaml] [--workflow ID] [--upload]   # run an eval on the pinned engine

All five read `HAJER_*` from the environment the way the SDK does, and none takes a credential on the
command line: a key in an argument is a key in the shell history and in every process listing.

`eval` is the eval runner (`hajer.evals`): everything after the word is its own, parsed by `hajer.evals._cli`,
and every flag it does not know is handed to the engine, so `hajer eval --help` is the runner's help and
`hajer eval --engine-help` the engine's.

`proxy` is the opt-in wire recorder: it sits in front of the provider on loopback, forwards the exchange
and records it as origin `BROKER`, which is evidence of the bytes rather than a self-report about them.
Its flags belong to it, so everything after the subcommand is handed to `hajer._proxy.main` unparsed and
`python -m hajer proxy --help` is the proxy's own help.

`doctor` is the first command to run when nothing is arriving. It answers the four questions that account
for most of it — is the SDK the version you think it is, is `HAJER_BASE_URL` answering at all, is each
setting coming from the environment or from a default, and would this client be inert — and it answers
them without submitting a trace by default: its request is a keyless GET of the liveness probe, and a
401 from that is a perfectly good answer (something is listening; the key is a separate question).
`doctor --emit` instead sends one authenticated synthetic observation. Without a backend-returned
permalink it reports configured but unverified and exits 6, even when an observation ID is accepted.

`tail` is the other half of attach mode. An attached process reports one observation per provider call and
the question in that minute is *is anything arriving, and what is it*; this answers it in one line per row
— the instant, the observation id, the mode, the verifier it named (`-` for none), the provider and model
its receipt names (`-` when the calls arrived as summaries and there is no receipt), how much redaction
removed, ingest's reading of the tuple, and whether an assessment exists. Never the request, the output or
the evidence: those are the customer's data and watching traffic arrive does not need them.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path
from typing import Final

import httpx

from hajer import _bootstrap, _proxy
from hajer._client import Hajer
from hajer._json import JsonObject, JsonValue
from hajer._models import ObservationRow
from hajer._paths import HEALTH_PATH, VERSION
from hajer._payload import wire_source
from hajer._self_check import emit_self_check, self_check
from hajer._settings import HajerSettings, SettingSource, settings_sources
from hajer._transport import probe
from hajer.evals import _cli
from hajer.evals._engine import engine_status

#: What an absent field prints as. One character, so the columns stay readable when half a page has none.
_ABSENT = "-"
#: Where the generated client-side redaction catalog lives, and the names it may publish its id under.
#: Read through `importlib` rather than imported: a build without the generated module must still be able
#: to answer `doctor`, and saying "absent" is the answer an operator needs.
_CATALOG_MODULE: Final[str] = "hajer._rules"
_CATALOG_IDS: Final[tuple[str, ...]] = ("CATALOG_ID", "CATALOG_DIGEST", "CATALOG_VERSION")
#: How wide the verifier column is before it stops being a column. A pinned verifier is `name@3` and the
#: name is bounded at 128 characters on the wire; this is what a terminal can read beside the rest.
_VERIFIER_WIDTH = 24


def _line(row: ObservationRow) -> str:
    """One observation, as one line. Fixed columns, because a tail is read by eye and not parsed."""
    provider = f"{row.provider}/{row.model}" if row.provider is not None else _ABSENT
    return "  ".join(
        (
            f"{row.created_at:<32}",
            f"{row.id:<40}",
            f"{row.mode:<7}",
            f"{(row.verifier or _ABSENT):<{_VERIFIER_WIDTH}}",
            f"{provider:<34}",
            f"redactions={row.redactions:<3}",
            f"{row.disposition:<20}",
            f"assessed={'yes' if row.assessed else 'no'}",
        )
    )


def _attach_path() -> int:
    """Print the directory holding `sitecustomize.py`. One line, so `$(…)` is the whole usage."""
    sys.stdout.write(f"{Path(_bootstrap.__file__).resolve().parent}\n")
    return 0


def _tail(*, since: str | None, follow: bool, limit: int | None, settings: HajerSettings) -> int:
    """Print every observation from `since` on, once — or until interrupted with `--follow`.

    Two things make this correct rather than approximately correct. The route's `since` is **inclusive**,
    because one `observe` flush writes its rows in one transaction and they share an instant, so a
    strictly-after read would lose the rest of a flush this had seen one row of. That means the boundary
    row comes back, so ids already printed at the boundary instant are remembered and dropped — and only
    those, so the set stays the size of one flush rather than of one day.

    `--follow` polls; it does not stream. `HAJER_TAIL_INTERVAL_MS` is the interval, and Ctrl-C is how it
    ends: an interrupt is the operator saying stop, and it is not an error.
    """
    if settings.inert:
        sys.stderr.write(
            "hajer tail: no credential. Set HAJER_API_KEY and HAJER_TEAM_ID (and HAJER_BASE_URL for a "
            "local backend); this reads a team's own observations and cannot do it anonymously.\n"
        )
        return 2
    printed: set[str] = set()
    boundary = since
    with Hajer(settings=settings) as client:
        while True:
            rows = client.observations(since=boundary, limit=limit or settings.tail_limit)
            fresh = [row for row in rows if row.id not in printed]
            for row in fresh:
                sys.stdout.write(f"{_line(row)}\n")
            sys.stdout.flush()
            if rows:
                boundary = rows[-1].created_at
                # Only the ids sharing the new boundary instant can come back, so only those are kept.
                printed = {row.id for row in rows if row.created_at == boundary}
            if not follow:
                return 0
            try:
                time.sleep(settings.tail_interval_ms / 1_000)
            except KeyboardInterrupt:  # pragma: no cover - the operator ending the tail is not an error
                return 0


def redaction_catalog() -> str:
    """The id of the client-side redaction catalog this build carries, or why there is none."""
    if importlib.util.find_spec(_CATALOG_MODULE) is None:
        return "absent: this build carries no client-side redaction catalog"
    module = importlib.import_module(_CATALOG_MODULE)
    for name in _CATALOG_IDS:
        found = getattr(module, name, None)
        if isinstance(found, str) and found:
            return found
    return f"present at {_CATALOG_MODULE}, which publishes no catalog id"


def _why_inert(settings: HajerSettings) -> str | None:
    """Why nothing would be sent, in the operator's own terms — or `None` when something would be."""
    if settings.disabled:
        return "HAJER_DISABLED is set: nothing is sent whatever else is configured"
    missing = [
        name for name, value in (("HAJER_API_KEY", settings.api_key), ("HAJER_TEAM_ID", settings.team_id)) if not value
    ]
    if missing:
        return f"{' and '.join(missing)} absent: verify returns unavailable{{DISABLED}} and observe is a no-op"
    return None


def _report(settings: HajerSettings, rows: tuple[SettingSource, ...], status: int | None) -> JsonObject:
    """Everything `doctor` knows, as one document. `--json` prints this; the prose prints the same facts."""
    settings_rows: list[JsonValue] = [
        {"name": row.name, "variable": row.variable, "value": row.value, "source": row.source} for row in rows
    ]
    return {
        "version": VERSION,
        "redactionCatalog": redaction_catalog(),
        "wireSource": wire_source(),
        "baseUrl": settings.base_url,
        "baseUrlPath": HEALTH_PATH,
        "baseUrlAnswers": status is not None,
        "baseUrlStatus": status,
        "inert": settings.inert,
        "inertBecause": _why_inert(settings),
        "settings": settings_rows,
    }


def doctor(
    *, as_json: bool, settings: HajerSettings, transport: httpx.BaseTransport | None = None, emit: bool = False
) -> int:
    """Print what this process's SDK is configured to do, and whether it could do it."""
    if emit:
        emission = emit_self_check(settings, transport=transport)
        sys.stdout.write(
            json.dumps(emission, indent=2, sort_keys=True) + "\n"
            if as_json
            else "\n".join(f"{key}: {value}" for key, value in emission.items()) + "\n"
        )
        return 6  # The existing backend receipt has no permalink; never manufacture verified delivery.
    rows = settings_sources(settings)
    status = probe(settings.base_url, HEALTH_PATH, timeout_ms=settings.deadline_ms_default, transport=transport)
    report = _report(settings, rows, status)
    report["selfCheck"] = self_check(settings)
    report["evals"] = engine_status(settings)
    if as_json:
        sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
        return 0
    reached = f"{status}" if status is not None else "no answer"
    lines = [
        f"hajer {VERSION}",
        f"wire            {wire_source()}",
        f"redaction       {redaction_catalog()}",
        f"base url        {settings.base_url}{HEALTH_PATH} -> {reached} (keyless GET)",
        f"inert           {'yes' if settings.inert else 'no'}",
        f"self check      {json.dumps(report['selfCheck'], sort_keys=True)}",
        f"eval engine     {json.dumps(report['evals'], sort_keys=True)}",
    ]
    because = _why_inert(settings)
    if because is not None:
        lines.append(f"                {because}")
    lines.append("")
    lines.append(f"{'setting':<26} {'variable':<34} {'source':<8} value")
    lines.extend(f"{row.name:<26} {row.variable:<34} {row.source:<8} {row.value}" for row in rows)
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hajer", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    # `eval` is listed so `hajer --help` names every command, and never *parsed* here: `main` hands everything
    # after the word to `hajer.evals._cli.main`, whose flags have one definition — the runner's own.
    sub.add_parser("eval", help="Run a promptfoo configuration on the pinned eval engine; `hajer eval --help`")
    sub.add_parser(
        "attach-path",
        help="Print the directory to put on PYTHONPATH so HAJER_ATTACH=1 attaches with no code change",
    )
    tail = sub.add_parser("tail", help="Print one line per recorded observation, oldest first")
    tail.add_argument("--since", default=None, help="An ISO instant; inclusive, so pass the last row you saw")
    tail.add_argument("--follow", action="store_true", help="Keep asking, every HAJER_TAIL_INTERVAL_MS")
    tail.add_argument("--limit", type=int, default=None, help="Rows per page; the route clips it to its own bound")
    doctor = sub.add_parser("doctor", help="What this process's SDK is configured to do, and whether it can")
    doctor.add_argument("--json", action="store_true", help="The same facts as one JSON document on stdout")
    doctor.add_argument(
        "--emit", action="store_true", help="Send one synthetic trace; exit 6 without a verified permalink"
    )
    # `proxy` is listed here so `python -m hajer` and `python -m hajer --help` name all four commands,
    # and it is never *parsed* here: `main` hands everything after the word to `hajer._proxy.main`
    # before argparse sees it, so the proxy's flags have one definition and it is the proxy's own.
    sub.add_parser("proxy", help="Record provider exchanges at the wire (origin BROKER); loopback only")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments[:1] == ["proxy"]:
        return _proxy.main(arguments[1:])
    if arguments[:1] == ["eval"]:
        return _cli.main(arguments[1:])
    options = build_parser().parse_args(arguments)
    if options.command == "attach-path":
        return _attach_path()
    if options.command == "tail":
        return _tail(
            since=options.since,
            follow=bool(options.follow),
            limit=options.limit,
            settings=HajerSettings.from_env(),
        )
    if options.command == "doctor":
        return doctor(as_json=bool(options.json), emit=bool(options.emit), settings=HajerSettings.from_env())
    return 2  # pragma: no cover - argparse's own `required=True` answers an unknown command first


if __name__ == "__main__":
    raise SystemExit(main())
