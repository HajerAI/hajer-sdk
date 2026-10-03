/**
 * `Hajer` — the call a customer's code makes.
 *
 *     const assessment = await hajer.verify("refund-policy@1", request, output, evidence);
 *     if (assessment.status === "violated") { ... }   // the application decides; the SDK never does
 *
 * **`verify` never rejects.** A transport failure, a timeout, a 500, a body over the bound, a
 * missing key: every one of them resolves to an `Assessment` with `status: "unavailable"` and a
 * `reason` that says which, and `decidedLocally` says whether this process or the service decided
 * it. `raiseOnUnavailable: true` on the constructor is available for a caller who would rather have
 * the stack trace, and it is opt-in for one reason: the default must be the one that keeps a
 * customer's request path working when Hajer is down.
 *
 * **There is no `onUnavailable: "satisfied"`.** Continuing the application and satisfying an
 * obligation are different facts, and the second is never manufactured from the first. What to do
 * with `unavailable` — continue, hold, review, reject — is the application's policy, chosen per
 * workflow at enforcement time, and it lives in the customer's own `if`.
 *
 * **`observe` never rejects and never waits.** It returns a receipt from one bounded in-memory
 * queue; `queue.ts` has the delivery contract.
 */

import { caseIdentity } from "./caseKey.ts";
import { byteLength, canonical, type JsonObject, type JsonValue } from "./json.ts";
import { CallScope, wrapClient, type WrappedCall } from "./attach.ts";
import { deriveIdempotencyKey, observationBody, redactedBody, type IngestMode } from "./payload.ts";
import { ObserveQueue, type ObserveReceipt } from "./queue.ts";
import { buildPolicy, type ClientRedactionPolicy } from "./redact.ts";
import { fromEnv, type HajerSettings } from "./settings.ts";
import type { VerifyOut } from "./wire.ts";

const VERIFY_PATH = "/api/teams/{team_id}/verify";
const OBSERVE_PATH = "/api/teams/{team_id}/observe";
const OBSERVATIONS_PATH = "/api/teams/{team_id}/observations";

/** The inert team id: `verify` needs a path even when there is nothing to send it to. */
const NO_TEAM = "-";

export type VerificationStatus = "satisfied" | "violated" | "insufficient_evidence" | "unavailable";

/**
 * Why an assessment is `unavailable`. The first six are the server's (contract v0); the last six are
 * this client's own, decided locally, and they are deliberately distinct so a reader can tell "we
 * never asked" from "the service answered that it could not".
 */
export type UnavailableReason =
  | "SHADOW"
  | "DEADLINE"
  | "VERIFIER_UNKNOWN"
  | "JUDGE_UNAVAILABLE"
  | "BUDGET_EXHAUSTED"
  | "INTERNAL"
  | "DISABLED"
  | "LOCAL_DEADLINE"
  | "TRANSPORT"
  | "BODY_OVER_BOUND"
  | "HTTP_STATUS"
  | "MALFORMED_RESPONSE";

const LOCAL_REASONS = new Set<UnavailableReason>([
  "DISABLED",
  "LOCAL_DEADLINE",
  "TRANSPORT",
  "BODY_OVER_BOUND",
  "HTTP_STATUS",
  "MALFORMED_RESPONSE",
]);

/** What one `verify` answered. `unavailable` is never reported as `satisfied`, by anything, ever. */
export interface Assessment {
  readonly status: VerificationStatus;
  readonly reason: UnavailableReason | null;
  readonly observationId: string | null;
  readonly findings: readonly JsonObject[];
  readonly missingEvidence: readonly JsonObject[];
  readonly limitations: readonly string[];
  /** A fact about TRUST, not about the answer: a shadow verifier's assessment is computed and
   *  recorded in full and the caller is told `unavailable{SHADOW}`, because nothing about it has
   *  been qualified on this team's own data yet. */
  readonly shadow: boolean;
  /** Empty on a real answer; filled when the answer stands in for work the service does not do. */
  readonly seam: string | null;
  readonly latencyMs: number;
  /** True when this process decided, false when the service answered. */
  readonly decidedLocally: boolean;
}

export class AssessmentUnavailableError extends Error {
  readonly assessment: Assessment;

  constructor(assessment: Assessment) {
    super(`assessment unavailable: ${assessment.reason ?? "unknown"}`);
    this.assessment = assessment;
  }
}

export interface HajerOptions {
  readonly settings?: HajerSettings;
  readonly fetch?: typeof globalThis.fetch;
  readonly raiseOnUnavailable?: boolean;
  readonly policy?: ClientRedactionPolicy;
}

export interface CallOptions {
  readonly deadlineMs?: number;
  readonly idempotencyKey?: string;
  readonly caseKey?: string;
  readonly policy?: ClientRedactionPolicy;
}

function unavailable(reason: UnavailableReason, latencyMs: number): Assessment {
  return {
    status: "unavailable",
    reason,
    observationId: null,
    findings: [],
    missingEvidence: [],
    limitations: [],
    shadow: false,
    seam: null,
    latencyMs,
    decidedLocally: LOCAL_REASONS.has(reason),
  };
}

/**
 * The service's `AssessmentOut` as the `Assessment` a customer's `if` compares against.
 *
 * `read` is typed as `VerifyOut` — the alias `wire.ts` resolves out of the backend's own guarded
 * snapshot — so every field name below is checked against the route rather than remembered. The
 * field the service spells `unavailableReason` was read here as `reason` for exactly as long as
 * nothing typed it: one live call answered `unavailable` with a null reason, which is the shape of
 * a client that cannot tell `SHADOW` from `INTERNAL`.
 *
 * The validation that remains is the narrow kind a type cannot do: a body that is not an object, or
 * a `status` outside the four. A newer server may add fields and an older client must keep working
 * rather than throw in the middle of a customer's request.
 */
function parseAssessment(body: unknown, latencyMs: number): Assessment | null {
  if (body === null || typeof body !== "object" || Array.isArray(body)) return null;
  const record = body as JsonObject;
  const status = record.status;
  if (typeof status !== "string") return null;
  if (!["satisfied", "violated", "insufficient_evidence", "unavailable"].includes(status)) return null;
  const read = body as VerifyOut;
  return {
    status: status as VerificationStatus,
    reason: (read.unavailableReason ?? null) as UnavailableReason | null,
    observationId: typeof read.observationId === "string" ? read.observationId : null,
    findings: Array.isArray(read.findings) ? (read.findings as unknown as JsonObject[]) : [],
    missingEvidence: Array.isArray(read.missingEvidence)
      ? (read.missingEvidence as unknown as JsonObject[])
      : [],
    limitations: Array.isArray(read.limitations) ? read.limitations : [],
    shadow: read.shadow === true,
    seam: typeof read.seam === "string" ? read.seam : null,
    latencyMs,
    decidedLocally: false,
  };
}

export class Hajer {
  readonly settings: HajerSettings;
  private readonly policy: ClientRedactionPolicy;
  private readonly raiseOnUnavailable: boolean;
  private readonly http: typeof globalThis.fetch;
  private readonly queue: ObserveQueue;
  private readonly callScope: CallScope;

  constructor(options: HajerOptions = {}) {
    this.settings = options.settings ?? fromEnv();
    // Built once per client, not once per call: a policy with a broken pattern is a construction
    // error a developer sees at start-up rather than a degraded pass they read about in an assessment.
    this.policy = options.policy ?? buildPolicy();
    this.raiseOnUnavailable = options.raiseOnUnavailable ?? false;
    this.http = options.fetch ?? globalThis.fetch;
    this.callScope = new CallScope(this.settings.wrappedCallsMax);
    this.queue = new ObserveQueue(
      this.settings,
      this.settings.inert ? null : (body) => this.post(OBSERVE_PATH, body, "observe-flush"),
    );
  }

  /** True when there is no key, no team, or `HAJER_DISABLED`: nothing is sent and nothing rejects. */
  get inert(): boolean {
    return this.settings.inert;
  }

  /** The scope the next `verify` will drain — hand it to `wrap()` for one operation's calls. */
  scope(): CallScope {
    return this.callScope;
  }

  /** The client, instrumented in place. The SDK imports neither provider package. */
  wrap<T extends object>(client: T): T {
    return wrapClient(client, {
      scope: this.callScope,
      captureContent: this.settings.captureContent,
      captureCallSite: this.settings.captureCallSite,
      projectRoot: this.settings.projectRoot,
    });
  }

  /** Apply `verifier` to this payload and answer inside the deadline, or say `unavailable`. */
  async verify(
    verifier: string,
    request: JsonValue,
    output: JsonValue,
    evidence: JsonObject | null = null,
    options: CallOptions = {},
  ): Promise<Assessment> {
    const started = Date.now();
    const deadlineMs = options.deadlineMs ?? this.settings.deadlineMsDefault;
    const { body, key } = this.body("VERIFY", verifier, request, output, evidence, options, deadlineMs);
    if (this.settings.inert) return this.complete(unavailable("DISABLED", Date.now() - started));
    const raw = canonical(body);
    if (byteLength(raw) > this.settings.bodyMaxBytes) {
      return this.complete(unavailable("BODY_OVER_BOUND", Date.now() - started));
    }
    const remaining = deadlineMs - (Date.now() - started);
    if (remaining <= 0) return this.complete(unavailable("LOCAL_DEADLINE", Date.now() - started));
    let response: Response;
    try {
      response = await this.fetchWithDeadline(VERIFY_PATH, raw, key, remaining);
    } catch (error) {
      const reason: UnavailableReason =
        error instanceof Error && error.name === "AbortError" ? "LOCAL_DEADLINE" : "TRANSPORT";
      return this.complete(unavailable(reason, Date.now() - started));
    }
    if (!response.ok) return this.complete(unavailable("HTTP_STATUS", Date.now() - started));
    let parsed: Assessment | null;
    try {
      parsed = parseAssessment(await response.json(), Date.now() - started);
    } catch {
      parsed = null;
    }
    if (parsed === null) return this.complete(unavailable("MALFORMED_RESPONSE", Date.now() - started));
    return this.complete(parsed);
  }

  /** Record this payload for verification off the response path. Returns immediately. */
  observe(
    verifier: string,
    request: JsonValue,
    output: JsonValue,
    evidence: JsonObject | null = null,
    options: CallOptions = {},
  ): ObserveReceipt {
    const { body, key } = this.body("OBSERVE", verifier, request, output, evidence, options, null);
    return this.queue.enqueue(body, key);
  }

  /** Observations waiting in memory. */
  queueDepth(): number {
    return this.queue.depth;
  }

  /** Send everything queued now. Never rejects. */
  async flush(): Promise<void> {
    await this.queue.flush();
  }

  /** What `tail` reads: this team's stored observations, oldest first. */
  async observations(options: { since?: string; limit?: number } = {}): Promise<readonly JsonObject[]> {
    if (this.settings.inert) return [];
    const query = new URLSearchParams();
    if (options.since !== undefined) query.set("since", options.since);
    if (options.limit !== undefined) query.set("limit", String(options.limit));
    const suffix = query.size > 0 ? `?${query.toString()}` : "";
    const response = await this.http(this.url(OBSERVATIONS_PATH) + suffix, {
      method: "GET",
      headers: this.headers(),
    });
    if (!response.ok) return [];
    const body: unknown = await response.json();
    return Array.isArray(body) ? (body as JsonObject[]) : [];
  }

  /** Flush what is queued and stop. */
  async close(): Promise<void> {
    await this.flush();
  }

  // ── internals ───────────────────────────────────────────────────────────────────────────────

  private complete(assessment: Assessment): Assessment {
    if (assessment.status === "unavailable" && this.raiseOnUnavailable) {
      throw new AssessmentUnavailableError(assessment);
    }
    return assessment;
  }

  private team(): string {
    return this.settings.teamId ?? NO_TEAM;
  }

  private url(template: string): string {
    return this.settings.baseUrl.replace(/\/+$/u, "") + template.replace("{team_id}", this.team());
  }

  private headers(): Record<string, string> {
    return {
      "content-type": "application/json",
      authorization: `Bearer ${this.settings.apiKey ?? ""}`,
    };
  }

  private body(
    mode: IngestMode,
    verifier: string,
    request: JsonValue,
    output: JsonValue,
    evidence: JsonObject | null,
    options: CallOptions,
    deadlineMs: number | null,
  ): { readonly body: JsonObject; readonly key: string } {
    // Taken unconditionally, even when the client is inert: a process with no key would otherwise
    // accumulate records forever, and a customer's test run would grow a slow leak the SDK put there.
    const taken: { calls: readonly WrappedCall[]; dropped: number } = this.callScope.take();
    const key =
      options.idempotencyKey ??
      deriveIdempotencyKey({ teamId: this.team(), verifier, request, output, evidence });
    // Derived here and now, before any later pass rewrites the request: a key derived after redaction
    // moves whenever a rule fires.
    const identity = caseIdentity(options.caseKey ?? null, request);
    const body = observationBody({
      mode,
      verifier,
      request,
      output,
      evidence,
      wrapped: taken.calls,
      wrappedDropped: taken.dropped,
      contentCaptured: this.settings.captureContent,
      idempotencyKey: key,
      deadlineMs,
      caseKey: identity.key,
      caseKeySource: identity.source,
    });
    if (!this.settings.redactClient) return { body, key };
    return { body: redactedBody(body, options.policy ?? this.policy), key };
  }

  private async fetchWithDeadline(
    template: string,
    raw: string,
    idempotencyKey: string,
    timeoutMs: number,
  ): Promise<Response> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      return await this.http(this.url(template), {
        method: "POST",
        headers: { ...this.headers(), "idempotency-key": idempotencyKey },
        body: raw,
        signal: controller.signal,
      });
    } finally {
      clearTimeout(timer);
    }
  }

  private async post(template: string, body: JsonObject, idempotencyKey: string): Promise<void> {
    const response = await this.fetchWithDeadline(
      template,
      canonical(body),
      idempotencyKey,
      this.settings.observeFlushDeadlineMs,
    );
    if (!response.ok) throw new Error(`the ingest route answered ${response.status}`);
  }
}
