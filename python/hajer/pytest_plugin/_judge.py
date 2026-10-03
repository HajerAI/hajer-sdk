"""A judge check in customer CI is judged live on Hajer's JUDGE route, never locally and never in replay.

A live attempt posts the check's id (Hajer judges it with the rubric it stored, and only when the team's current suite
publishes it calibrated), the CI run id and the attempt number (so a repeat is a new judging, never a refused replay),
the case input and the app's output, with the team key; Hajer pays for and meters the call. SATISFIES and
NOT_APPLICABLE are PASS, VIOLATES is FAIL. A team with no JUDGE funding (409 JUDGE_UNFUNDED) is SKIPPED: reported
apart, never a failure (a soft stop). Anything else — no key, no project, a refused or unreadable answer —
is UNABLE_TO_VERIFY.
"""

from collections.abc import Callable
from typing import Final, Literal, cast
from uuid import uuid4

import httpx
from pydantic import JsonValue

from hajer._errors import BodyOverBoundError
from hajer._settings import HajerSettings
from hajer._transport import Deadline, SyncTransport, encode_body
from hajer.pytest_plugin._models import Verdict

SKIPPED: Final = "SKIPPED"
JUDGE_UNFUNDED: Final = "JUDGE_UNFUNDED"
JudgeAnswer = Verdict | Literal["SKIPPED"]
Judge = Callable[[dict[str, JsonValue], JsonValue, JsonValue, int], JudgeAnswer]
_VERDICTS: dict[str, Verdict] = {"SATISFIES": "PASS", "NOT_APPLICABLE": "PASS", "VIOLATES": "FAIL"}


def remote_judge(
    settings: HajerSettings,
    project_id: str | None,
    run_id: str,
    *,
    suite_id: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> Judge:
    """The judge a live run uses: the team's project judging route, or UNABLE_TO_VERIFY without one."""

    def judge(check: dict[str, JsonValue], output: JsonValue, request: JsonValue, attempt: int) -> JudgeAnswer:
        identity = check.get("check")
        check_id = identity.get("id") if isinstance(identity, dict) else None
        if not settings.api_key or not settings.team_id or not project_id or not isinstance(check_id, str):
            return "UNABLE_TO_VERIFY"
        body: dict[str, JsonValue] = {
            "checkId": check_id,
            "suiteId": suite_id,
            "runId": run_id,
            "attempt": attempt,
            "output": output,
            "input": request,
        }
        deadline = Deadline(settings.ci_case_timeout_seconds * 1000)
        client = SyncTransport(base_url=settings.base_url, api_key=settings.api_key, transport=transport)
        try:
            response = client.post(
                f"/api/teams/{settings.team_id}/projects/{project_id}/suite-runs/judgings",
                encode_body(body, limit=settings.body_max_bytes),
                idempotency_key=str(uuid4()),
                timeout_s=deadline.timeout_s(),
            )
            if response.status_code == 409 and JUDGE_UNFUNDED in response.text:
                return SKIPPED
            answer: object = response.json() if response.status_code == 200 else None
        except (httpx.HTTPError, BodyOverBoundError, ValueError):
            return "UNABLE_TO_VERIFY"
        finally:
            client.close()
        verdict = cast(dict[str, object], answer).get("verdict") if isinstance(answer, dict) else None
        return _VERDICTS.get(verdict, "UNABLE_TO_VERIFY") if isinstance(verdict, str) else "UNABLE_TO_VERIFY"

    return judge
