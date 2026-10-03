/**
 * A TypeScript fixture application for checking the SDK against a live Hajer server.
 *
 * It is the README's five lines with the model call removed: the point is that the
 * TypeScript client reaches a live server over the same wire and the same redaction catalog as the
 * Python one, and a provider call would spend money to prove nothing about that. **No provider is
 * called and nothing here reserves on a ledger.**
 *
 *     node --experimental-strip-types typescript/fixture-app.ts
 *
 * with `HAJER_BASE_URL`, `HAJER_API_KEY`, `HAJER_TEAM_ID` and `HAJER_VERIFIER` in the environment.
 * It prints one JSON object on stdout — the assessment's status and reason, the observation id the
 * server minted, and the client-side redaction report — and exits 0 whatever the assessment says.
 * Deciding whether `unavailable{SHADOW}` is the right answer is the caller's job, not this
 * program's: an application that exited non-zero on `unavailable` would be an application that
 * gates on a verifier nobody has qualified.
 */

import { Hajer } from "./src/index.ts";

const verifier = process.env.HAJER_VERIFIER ?? "employee-compile-title-cap@1";

// `HAJER_FIXTURE_REDACTABLE` puts a value the catalog removes into the brief, and nothing else about
// the envelope changes. The ordinary brief carries none, so its client-side redaction report is
// ABSENT — which is the honest record of a client that redacted nothing, and is not evidence that it
// ran no catalog. A check that wants to read `redaction-rules@6` off a stored row therefore needs
// an envelope that actually had something to remove, and this is it. Still no provider call.
const redactable = process.env.HAJER_FIXTURE_REDACTABLE === "1";
const brief = redactable
  ? "which titles should this compile forward? reach me at hiring@example.com or 4111 1111 1111 1111"
  : "which titles should this compile forward?";

const client = new Hajer();

const assessment = await client.verify(
  verifier,
  { role: "staff-engineer", brief },
  { job_titles: ["Staff Engineer", "Engineering Manager"] },
  { requested_roles: ["Staff Engineer", "Engineering Manager", "Director of Platform"] },
);

process.stdout.write(
  JSON.stringify(
    {
      sdk: "hajer-typescript",
      verifier,
      redactable,
      inert: client.inert,
      status: assessment.status,
      reason: assessment.reason,
      observationId: assessment.observationId,
      shadow: assessment.shadow,
      decidedLocally: assessment.decidedLocally,
      latencyMs: assessment.latencyMs,
    },
    null,
    2,
  ) + "\n",
);

await client.close();
