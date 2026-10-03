"""The SDK API operation catalog used to generate and check its wire types.

All cataloged routes are team-scoped and authenticated with a team API key. The private
input route is used only by the opt-in customer-CI suite runner, never by application capture.
"""

from __future__ import annotations

from typing import Final

VERSION: Final[str] = "0.1.0"

VERIFY_PATH: Final[str] = "/api/teams/{team_id}/verify"
OBSERVE_PATH: Final[str] = "/api/teams/{team_id}/observe"
OBSERVATIONS_PATH: Final[str] = "/api/teams/{team_id}/observations"
PRIVATE_INPUTS_PATH: Final[str] = "/api/teams/{team_id}/projects/{project_id}/suite-runs/execution-inputs"
ASSESSMENT_PATH: Final[str] = "/api/teams/{team_id}/observations/{observation_id}/assessment"

#: The backend's liveness probe. **Not one of the SDK's calls** and deliberately outside
#: `SDK_OPERATIONS`: it is read only by `python -m hajer doctor`, to answer "is anything listening at
#: HAJER_BASE_URL" without a credential. The client never touches it.
HEALTH_PATH: Final[str] = "/api/health"

SDK_OPERATIONS: Final[tuple[tuple[str, str], ...]] = (
    ("post", VERIFY_PATH),
    ("post", OBSERVE_PATH),
    ("get", OBSERVATIONS_PATH),
    ("get", ASSESSMENT_PATH),
    ("post", PRIVATE_INPUTS_PATH),
)
