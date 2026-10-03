/**
 * `HajerSettings` — every bound this SDK obeys, each one a field with an environment variable.
 *
 * Nothing in this package spells a limit as a literal anywhere but here, which is the same owner
 * decision the Python SDK's `_settings.py` records: nothing hard-coded. The defaults are the Python
 * SDK's, value for value, because two clients that timed out at different deadlines or refused at
 * different body sizes would be two products.
 *
 * `fromEnv` is the ONLY place in this package that reads `process.env`. A grep for `process.env`
 * outside this file is a bug.
 *
 * **Inert mode is the important behaviour here.** Without `HAJER_API_KEY` and `HAJER_TEAM_ID` — or
 * with `HAJER_DISABLED=1` — the client is inert: `verify` resolves to `unavailable{DISABLED}` and
 * `observe` returns a receipt that queued nothing. Neither rejects, neither opens a socket. That is
 * what lets the integration land in a repository whose test suite has no Hajer credentials and pass
 * unchanged, and it is why a missing key is not a configuration error.
 */

/** The public base URL of the hosted service. */
export const DEFAULT_BASE_URL = "https://api.hajer.ai";

/**
 * The four budgets the client-side redaction pass runs under. Constants rather than settings for
 * the Python SDK's reason: a team that could raise them from the environment could turn a 1 MB
 * document into a 100 MB one inside their own request path and blame the SDK for the latency.
 */
export const REDACT_MAX_BYTES = 1_048_576;
export const REDACT_MAX_NODES = 50_000;
export const REDACT_MAX_DEPTH = 64;
export const REDACT_MAX_STRING_CHARS = 65_536;

/**
 * The call-site bounds (`callSite.ts`, `HAJER_CAPTURE_CALL_SITE`), the same numbers as the Python
 * SDK's: at most 8 application frames per call (the service's `callerFrames` bound) and 64 stack
 * frames looked at to find them. Constants rather than settings because they bound what a record
 * discloses.
 */
export const CALL_SITE_MAX_FRAMES = 8;
export const CALL_SITE_STACK_SCANNED = 64;
/** How many distinct source files one process remembers the digest of (`callSite.fileDigest`). */
export const FILE_DIGESTS_CACHED = 4096;
/** The longest declared workflow the service keeps (`workflowHint`, 512 characters). */
export const WORKFLOW_MAX_CHARS = 512;

const TRUE = new Set(["1", "true", "t", "yes", "y", "on"]);
const FALSE = new Set(["0", "false", "f", "no", "n", "off"]);

/** Everything the SDK reads from configuration, validated once, then frozen. */
export interface HajerSettings {
  readonly apiKey: string | null;
  readonly teamId: string | null;
  readonly baseUrl: string;
  readonly deadlineMsDefault: number;
  readonly observeQueueMax: number;
  readonly observeBatchMax: number;
  readonly observeFlushDeadlineMs: number;
  readonly bodyMaxBytes: number;
  readonly captureContent: boolean;
  /**
   * `HAJER_CAPTURE_CALL_SITE`, on by default: caller frames (relative to the project root, never a
   * home directory, and for a file under the root the SHA-256 digest of its bytes while it is unchanged
   * since the process started) and the endpoint's `host:port` — never content. Off sends neither.
   */
  readonly captureCallSite: boolean;
  /** `HAJER_PROJECT_ROOT`: what frames are relative to. Null means the working directory. */
  readonly projectRoot: string | null;
  readonly wrappedCallsMax: number;
  readonly redactClient: boolean;
  readonly disabled: boolean;
  /** No key, no team, or `HAJER_DISABLED`: nothing is sent and nothing rejects. */
  readonly inert: boolean;
}

export class HajerConfigError extends Error {}

function flag(raw: string | undefined, fallback: boolean, name: string): boolean {
  if (raw === undefined || raw === "") return fallback;
  const value = raw.trim().toLowerCase();
  if (TRUE.has(value)) return true;
  if (FALSE.has(value)) return false;
  throw new HajerConfigError(`${name} is neither true nor false: ${raw}`);
}

function positive(raw: string | undefined, fallback: number, name: string): number {
  if (raw === undefined || raw === "") return fallback;
  const value = Number(raw);
  if (!Number.isInteger(value) || value <= 0) {
    throw new HajerConfigError(`${name} must be a positive whole number, not ${raw}`);
  }
  return value;
}

/** The defaults, before any environment is read. Exported so a test can build one without `process`. */
export function defaults(overrides: Partial<HajerSettings> = {}): HajerSettings {
  const base = {
    apiKey: null,
    teamId: null,
    baseUrl: DEFAULT_BASE_URL,
    deadlineMsDefault: 1_500,
    observeQueueMax: 1_000,
    observeBatchMax: 32,
    observeFlushDeadlineMs: 5_000,
    bodyMaxBytes: 65_536,
    captureContent: false,
    captureCallSite: true,
    projectRoot: null,
    wrappedCallsMax: 32,
    redactClient: true,
    disabled: false,
    ...overrides,
  };
  return Object.freeze({
    ...base,
    inert: base.disabled || !base.apiKey || !base.teamId,
  });
}

/**
 * The settings this process is configured with. The one reader of the environment in this package.
 *
 * A malformed value throws HERE, at construction, rather than degrading a call later: a team that
 * wrote `HAJER_DEADLINE_MS_DEFAULT=soon` should learn it at start-up. A MISSING key does not throw,
 * because a missing key is inert and inert is a supported mode.
 */
export function fromEnv(env: Record<string, string | undefined> = process.env): HajerSettings {
  return defaults({
    apiKey: env.HAJER_API_KEY || null,
    teamId: env.HAJER_TEAM_ID || null,
    baseUrl: env.HAJER_BASE_URL || DEFAULT_BASE_URL,
    deadlineMsDefault: positive(env.HAJER_DEADLINE_MS_DEFAULT, 1_500, "HAJER_DEADLINE_MS_DEFAULT"),
    observeQueueMax: positive(env.HAJER_OBSERVE_QUEUE_MAX, 1_000, "HAJER_OBSERVE_QUEUE_MAX"),
    observeBatchMax: positive(env.HAJER_OBSERVE_BATCH_MAX, 32, "HAJER_OBSERVE_BATCH_MAX"),
    observeFlushDeadlineMs: positive(
      env.HAJER_OBSERVE_FLUSH_DEADLINE_MS,
      5_000,
      "HAJER_OBSERVE_FLUSH_DEADLINE_MS",
    ),
    bodyMaxBytes: positive(env.HAJER_BODY_MAX_BYTES, 65_536, "HAJER_BODY_MAX_BYTES"),
    captureContent: flag(env.HAJER_CAPTURE_CONTENT, false, "HAJER_CAPTURE_CONTENT"),
    captureCallSite: captureCallSiteFromEnv(env),
    projectRoot: env.HAJER_PROJECT_ROOT || null,
    wrappedCallsMax: positive(env.HAJER_WRAPPED_CALLS_MAX, 32, "HAJER_WRAPPED_CALLS_MAX"),
    redactClient: flag(env.HAJER_REDACT_CLIENT, true, "HAJER_REDACT_CLIENT"),
    disabled: flag(env.HAJER_DISABLED, false, "HAJER_DISABLED"),
  });
}

/**
 * `HAJER_CAPTURE_CALL_SITE` on its own, for `wrapClient` called without a settings object: the switch
 * is read here, in the one reader of the environment, and nowhere else.
 */
export function captureCallSiteFromEnv(env: Record<string, string | undefined> = process.env): boolean {
  return flag(env.HAJER_CAPTURE_CALL_SITE, true, "HAJER_CAPTURE_CALL_SITE");
}

/** `HAJER_PROJECT_ROOT` on its own, for `wrapClient` called without a settings object. */
export function projectRootFromEnv(env: Record<string, string | undefined> = process.env): string | null {
  return env.HAJER_PROJECT_ROOT || null;
}
