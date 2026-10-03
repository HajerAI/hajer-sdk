/**
 * `@hajer/sdk` — the public surface, and nothing else.
 *
 * Everything a customer's code touches is exported here. Everything else is an implementation
 * detail of this package, including `wire.ts` and `rules.ts`, which are generated and whose shapes
 * change when the backend's does.
 */

export { Hajer, AssessmentUnavailableError } from "./client.ts";
export type {
  Assessment,
  CallOptions,
  HajerOptions,
  UnavailableReason,
  VerificationStatus,
} from "./client.ts";
export { CallScope, scope, WrappedCall, wrapClient } from "./attach.ts";
export type { Api, Provider } from "./attach.ts";
export { buildPolicy, redactSubmission, UNSCANNED_PLACEHOLDER } from "./redact.ts";
export type { ClientRedactionPolicy, ClientRedactionReport, PolicyInput, RedactionEntry } from "./redact.ts";
export { CASE_KEY_SCHEME, caseIdentity, deriveCaseKey, normalisedRequest } from "./caseKey.ts";
export type { CaseKeySource } from "./caseKey.ts";
export { defaults, fromEnv, HajerConfigError, DEFAULT_BASE_URL } from "./settings.ts";
export type { HajerSettings } from "./settings.ts";
export type { ObserveReceipt, ObserveState } from "./queue.ts";
export { CATALOG_ID } from "./rules.ts";
export { IDEMPOTENCY_SCHEME, VERSION } from "./payload.ts";
export type { JsonObject, JsonValue } from "./json.ts";
export type { VerifyIn, VerifyOut, ObserveIn, ObserveOut, ObservationsOut } from "./wire.ts";

import { Hajer } from "./client.ts";

/**
 * `wrap(client)` without a `Hajer` in hand: instruments against a module-level default client.
 *
 * The form the README's five lines use. A process that builds several `Hajer` clients should use
 * `hajer.wrap(...)` instead, so each client drains its own scope.
 */
let shared: Hajer | null = null;

export function wrap<T extends object>(client: T): T {
  shared ??= new Hajer();
  return shared.wrap(client);
}

/** The client `wrap()` instruments against, so the five lines can `verify` through the same scope. */
export function sharedClient(): Hajer {
  shared ??= new Hajer();
  return shared;
}
