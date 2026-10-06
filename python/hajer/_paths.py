"""The routes the SDK calls on the platform, and the package version.

Every route is team-scoped and authenticated with a team API key (`HAJER_API_KEY`, `Authorization: Bearer`).
"""

from __future__ import annotations

from typing import Final

VERSION: Final[str] = "0.2.2"

#: Where the span emitter exports to by default: the platform's OTLP/HTTP traces receiver for the team
#: (`hajer._telemetry`). The whole route, OTLP's own `/v1/traces` included.
OTEL_TRACES_PATH: Final[str] = "/api/teams/{team_id}/otel/v1/traces"

#: Where `hajer eval --upload` posts an eval run (`hajer.evals._upload`). The payload is the SDK's own versioned
#: schema (`hajer.evals._payload`); the platform resolves the repository the run belongs to from the run's own git
#: context, so the route names the team and nothing else.
EVAL_RUNS_PATH: Final[str] = "/api/teams/{team_id}/eval-runs"

#: The backend's liveness probe. Read only by `python -m hajer doctor`, to answer "is anything listening at
#: HAJER_BASE_URL" without a credential.
HEALTH_PATH: Final[str] = "/api/health"
