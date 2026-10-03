/**
 * The bodies the SDK sends and the assessment it reads back, mapped onto the generated wire.
 *
 * `wire.ts` is the authority for every name below: `VerifyIn` and `ObserveIn` resolve through the
 * snapshot's own `paths`, so a field this module spells differently from the backend is a compile
 * error in this package rather than a 422 in a customer's request path.
 *
 * The body is built as a plain object on purpose. It runs inside somebody else's request path, and
 * validating every submission against a schema there would spend the caller's latency proving
 * something the type checker already proved.
 */

import { createHash } from "node:crypto";

import { caseIdentity, type CaseKeySource } from "./caseKey.ts";
import { canonical, type JsonObject, type JsonValue } from "./json.ts";
import { redactSubmission, type ClientRedactionPolicy } from "./redact.ts";
import type { WrappedCall } from "./attach.ts";

/**
 * `hj1` names the derivation, not the key: a change to what the digest covers is `hj2`, so two SDK
 * versions can never disagree silently about whether two payloads are the same submission.
 */
export const IDEMPOTENCY_SCHEME = "hj1";

export const VERSION = "0.1.0";

const SDK = { name: "hajer-typescript", version: VERSION } as const;

/** The two modes the ingest route takes. */
export type IngestMode = "VERIFY" | "OBSERVE";

/** The keys of a submission whose values are customer data and are therefore walked by redaction. */
const REDACTED_KEYS = ["request", "output", "evidence", "wrappedCalls"] as const;

export interface BodyInput {
  readonly mode: IngestMode;
  readonly verifier: string | null;
  readonly request: JsonValue;
  readonly output: JsonValue;
  readonly evidence?: JsonObject | null;
  readonly wrapped?: readonly WrappedCall[];
  readonly wrappedDropped?: number;
  readonly contentCaptured: boolean;
  readonly idempotencyKey: string;
  readonly deadlineMs?: number | null;
  readonly caseKey?: string | null;
  readonly caseKeySource?: CaseKeySource | null;
}

/**
 * One `IngestObservation` as the ingest route will receive it.
 *
 * `deadlineMs` is what is **left** of the caller's budget at the moment of the send, not what the
 * caller asked for: the server is told the remaining budget so it can refuse before reserving rather
 * than answer late. An `OBSERVE` submission carries none — it is off the response path.
 *
 * `verifier` is `null` for an attach-mode observation: the process did not name a verifier, so the
 * tuple is recorded and nothing is verified. `null` goes on the wire rather than the key being
 * omitted, because "no verifier" is a statement and an absent field is a silence.
 */
export function observationBody(input: BodyInput): JsonObject {
  const body: JsonObject = {
    mode: input.mode,
    verifier: input.verifier,
    request: input.request,
    output: input.output,
    evidence: input.evidence ?? {},
    wrappedCalls: (input.wrapped ?? []).map((call) => call.toWire()),
    wrappedCallsDropped: input.wrappedDropped ?? 0,
    contentCaptured: input.contentCaptured,
    idempotencyKey: input.idempotencyKey,
    sdk: { ...SDK },
  };
  if (input.deadlineMs !== null && input.deadlineMs !== undefined) body.deadlineMs = input.deadlineMs;
  if (input.caseKey !== null && input.caseKey !== undefined) {
    // Both keys or neither: a source naming no key is a provenance for something that is not there,
    // and the server refuses it rather than recording it.
    body.caseKey = input.caseKey;
    body.caseKeySource = input.caseKeySource ?? "CALLER";
  }
  return body;
}

/** What one `observe` flush sends: a list of observations, nothing else. */
export function batchBody(observations: readonly JsonObject[]): JsonObject {
  return { observations: [...observations] };
}

/**
 * One submission body with every customer value in it redacted, and the report attached.
 *
 * The metadata keys are deliberately not walked: a pass that could rewrite `idempotencyKey` or
 * `caseKey` would be a pass that can break routing to protect nothing.
 */
export function redactedBody(body: JsonObject, policy: ClientRedactionPolicy): JsonObject {
  const values: JsonObject = {};
  for (const key of REDACTED_KEYS) {
    if (key in body) values[key] = body[key];
  }
  const { value, report } = redactSubmission(values, policy);
  if (value === null || typeof value !== "object" || Array.isArray(value)) return body;
  const updated: JsonObject = { ...body, ...value };
  if (report !== null) updated.clientRedaction = report as unknown as JsonValue;
  return updated;
}

/**
 * The key this submission is idempotent under: the caller's when they have one, else derived.
 *
 * A request id a caller already holds is better than any digest of a payload, because it survives a
 * retry that changed a timestamp. The derivation is the fallback for callers who have none.
 */
export function deriveIdempotencyKey(input: {
  readonly teamId: string;
  readonly verifier: string | null;
  readonly request: JsonValue;
  readonly output: JsonValue;
  readonly evidence?: JsonObject | null;
}): string {
  const seed = canonical({
    scheme: IDEMPOTENCY_SCHEME,
    teamId: input.teamId,
    verifier: input.verifier,
    request: input.request,
    output: input.output,
    evidence: input.evidence ?? {},
  });
  return `${IDEMPOTENCY_SCHEME}_${createHash("sha256").update(seed, "utf8").digest("hex")}`;
}

export { caseIdentity };
