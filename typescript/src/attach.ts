/**
 * `wrap(client)` — the model calls are recorded beside the answer.
 *
 * **The SDK never imports `openai` or `@anthropic-ai/sdk`.** Both are optional peers and neither is
 * a runtime dependency: `wrap` instruments the object it is handed, by property, so a repository
 * that has one of them gets it wrapped and a repository that has neither still installs this package
 * and still passes its tests. That is also why the two providers are recognised by the SHAPE of the
 * object rather than by `instanceof`: an `instanceof` would need the import.
 *
 * What is recorded is a **summary** by default: the model, the settings, the declared tool names,
 * message counts and roles, duration, usage, the finish reason. No message text, no tool arguments
 * and no tool results unless `HAJER_CAPTURE_CONTENT` is on — the same default and the same switch as
 * the Python SDK, because a wrapper that shipped a customer's prompts by default would be the
 * dishonest part.
 *
 * The calls accumulate in a per-scope sink rather than a module global. `Hajer.scope()` returns the
 * sink the next `verify` will drain, so two requests handled concurrently in one process cannot claim
 * each other's calls — the failure the Python SDK's context-variable sink exists to prevent, spelled
 * with an explicit object here. The one `AsyncLocalStorage` is the declared workflow of `scope({
 * workflow }, fn)`, which must follow awaited work the way Python's context variable does. One scope
 * means one call site: every call inside it carries the declaration.
 */

import { AsyncLocalStorage } from "node:async_hooks";

import { callerFrames, type CallerFrame } from "./callSite.ts";
import type { JsonObject, JsonValue } from "./json.ts";
import { captureCallSiteFromEnv, projectRootFromEnv, WORKFLOW_MAX_CHARS } from "./settings.ts";

export type Provider = "openai" | "anthropic" | "unknown";
export type Api = "chat.completions" | "messages" | "responses" | "unknown";

/** One instrumented provider call, as the wire carries it inside `wrappedCalls`. */
export class WrappedCall {
  provider: Provider = "unknown";
  api: Api = "unknown";
  startedAt = new Date(0).toISOString();
  model: string | null = null;
  requestSettings: JsonObject = {};
  declaredTools: string[] = [];
  messageCount = 0;
  messageRoles: string[] = [];
  durationMs = 0;
  usage: Record<string, number> = {};
  responseId: string | null = null;
  finishReason: string | null = null;
  error: string | null = null;
  errorType: string | null = null;
  streamed = false;
  content: JsonObject | null = null;
  limitations: string[] = [];
  /** Where the call was made, innermost first (`callSite.ts`). Locations only; empty when not captured. */
  callerFrames: CallerFrame[] = [];
  /** `host:port` of the client's `baseURL`, or null when the client names none. */
  providerHost: string | null = null;
  /** The workflow the caller declared around this call (`scope({ workflow })`), taken at call start. */
  workflowHint: string | null = null;

  /** Record one thing this call could not observe, once. */
  note(limitation: string): void {
    if (!this.limitations.includes(limitation)) this.limitations.push(limitation);
  }

  /** The camelCase shape the ingest envelope carries as one element of `wrappedCalls`. */
  toWire(): JsonObject {
    const body: JsonObject = {
      provider: this.provider,
      api: this.api,
      startedAt: this.startedAt,
      model: this.model,
      requestSettings: { ...this.requestSettings },
      declaredTools: [...this.declaredTools],
      messageCount: this.messageCount,
      messageRoles: [...this.messageRoles],
      durationMs: this.durationMs,
      usage: { ...this.usage },
      toolCalls: [],
      toolResults: [],
      responseId: this.responseId,
      finishReason: this.finishReason,
      error: this.error,
      errorType: this.errorType,
      retries: 0,
      streamed: this.streamed,
      streamComplete: null,
      streamChunks: 0,
      limitations: [...this.limitations],
    };
    if (this.content !== null) body.content = { ...this.content };
    if (this.callerFrames.length > 0) body.callerFrames = this.callerFrames.map((frame) => ({ ...frame }));
    if (this.providerHost !== null) body.providerHost = this.providerHost;
    if (this.workflowHint !== null) body.workflowHint = this.workflowHint;
    return body;
  }
}

/** One operation's calls: a mutable object every wrapped client of that scope shares. */
export class CallScope {
  readonly calls: WrappedCall[] = [];
  dropped = 0;
  readonly #maximum: number;

  constructor(maximum: number) {
    this.#maximum = maximum;
  }

  record(call: WrappedCall): void {
    if (this.calls.length >= this.#maximum) {
      this.dropped += 1;
      return;
    }
    this.calls.push(call);
  }

  /** Take everything recorded so far and reset, so a second `verify` does not re-send the first's. */
  take(): { readonly calls: readonly WrappedCall[]; readonly dropped: number } {
    const taken = [...this.calls];
    const dropped = this.dropped;
    this.calls.length = 0;
    this.dropped = 0;
    return { calls: taken, dropped };
  }
}

const DECLARED = new AsyncLocalStorage<string | null>();

/**
 * One operation, optionally with a declared workflow: every wrapped call made inside `fn`, awaited work
 * included, carries `workflow` as its `workflowHint`. It guides grouping; a workflow key copied from Hajer
 * (`source-call-…`) is also a declaration the service links a call without call-site frames by, when the
 * indexed source still holds the key. An inner scope without a workflow declares none: nothing is inherited.
 * A blank or over-long name is refused here, when the scope opens, never during a provider call.
 */
export function scope<T>(options: { readonly workflow?: string | null }, fn: () => T): T {
  const workflow = options.workflow?.trim() ?? null;
  if (workflow !== null && (workflow.length === 0 || workflow.length > WORKFLOW_MAX_CHARS)) {
    throw new RangeError(`A declared workflow is 1 to ${WORKFLOW_MAX_CHARS} characters`);
  }
  return DECLARED.run(workflow, fn);
}

interface WrapOptions {
  readonly scope: CallScope;
  readonly captureContent: boolean;
  /** Caller frames and the endpoint. Absent means `HAJER_CAPTURE_CALL_SITE`, on by default. */
  readonly captureCallSite?: boolean;
  /** What frames are relative to. Absent means `HAJER_PROJECT_ROOT`, else the working directory. */
  readonly projectRoot?: string | null;
}

/** `host:port` of a client's `baseURL`, the scheme's default port when it names none. */
function providerHostOf(client: object): string | null {
  const base = (client as { baseURL?: unknown }).baseURL;
  if (base === undefined || base === null) return null;
  try {
    const url = new URL(String(base));
    const port = url.port || (url.protocol === "https:" ? "443" : url.protocol === "http:" ? "80" : "");
    return url.hostname && port ? `${url.hostname}:${port}` : null;
  } catch {
    return null;
  }
}

/** Where the call was made: what call-site linkage reads to name the call site that made it. */
function recordCallSite(call: WrappedCall, root: string): void {
  call.callerFrames = callerFrames(root);
  if (call.callerFrames.length === 0) {
    call.note("No application frame was found above this call, so it carries no call site.");
  }
}

function settingsOf(body: JsonObject): JsonObject {
  const named = ["temperature", "topP", "top_p", "maxTokens", "max_tokens", "toolChoice", "tool_choice"];
  const out: JsonObject = {};
  for (const key of named) {
    if (key in body) out[key] = body[key];
  }
  return out;
}

function toolNames(body: JsonObject): string[] {
  const tools = body.tools;
  if (!Array.isArray(tools)) return [];
  return tools.map((tool) => {
    if (tool === null || typeof tool !== "object" || Array.isArray(tool)) return "unknown";
    const record = tool as JsonObject;
    const nested = record.function;
    if (nested !== null && typeof nested === "object" && !Array.isArray(nested)) {
      return String((nested as JsonObject).name ?? "unknown");
    }
    return String(record.name ?? "unknown");
  });
}

function rolesOf(body: JsonObject): string[] {
  const messages = body.messages;
  if (!Array.isArray(messages)) return [];
  return messages.map((message) =>
    message !== null && typeof message === "object" && !Array.isArray(message)
      ? String((message as JsonObject).role ?? "unknown")
      : "unknown",
  );
}

function usageOf(response: JsonValue): Record<string, number> {
  if (response === null || typeof response !== "object" || Array.isArray(response)) return {};
  const usage = (response as JsonObject).usage;
  if (usage === null || usage === undefined || typeof usage !== "object" || Array.isArray(usage)) return {};
  const out: Record<string, number> = {};
  for (const [key, value] of Object.entries(usage as JsonObject)) {
    if (typeof value === "number") out[key] = value;
  }
  return out;
}

function finishOf(response: JsonValue): string | null {
  if (response === null || typeof response !== "object" || Array.isArray(response)) return null;
  const record = response as JsonObject;
  if (typeof record.stop_reason === "string") return record.stop_reason;
  const choices = record.choices;
  if (Array.isArray(choices) && choices.length > 0) {
    const first = choices[0];
    if (first !== null && typeof first === "object" && !Array.isArray(first)) {
      const reason = (first as JsonObject).finish_reason;
      if (typeof reason === "string") return reason;
    }
  }
  return null;
}

function instrument(
  create: (body: JsonObject, ...rest: unknown[]) => Promise<unknown>,
  owner: object,
  provider: Provider,
  api: Api,
  options: WrapOptions,
  host: string | null,
): (body: JsonObject, ...rest: unknown[]) => Promise<unknown> {
  const captureCallSite = options.captureCallSite ?? captureCallSiteFromEnv();
  // `undefined` means "not given": the environment decides. An explicit `null` means the working directory.
  const root =
    options.projectRoot === undefined
      ? (projectRootFromEnv() ?? process.cwd())
      : (options.projectRoot ?? process.cwd());
  return async function wrapped(body: JsonObject, ...rest: unknown[]): Promise<unknown> {
    const call = new WrappedCall();
    call.provider = provider;
    call.api = api;
    call.workflowHint = DECLARED.getStore() ?? null;
    if (captureCallSite) {
      call.providerHost = host;
      recordCallSite(call, root);
    }
    call.startedAt = new Date().toISOString();
    call.model = typeof body?.model === "string" ? body.model : null;
    call.requestSettings = settingsOf(body ?? {});
    call.declaredTools = toolNames(body ?? {});
    call.messageRoles = rolesOf(body ?? {});
    call.messageCount = call.messageRoles.length;
    if (!options.captureContent) {
      call.note("Content capture is off: no message text, tool arguments or tool results were recorded.");
    }
    const started = Date.now();
    try {
      const response = await create.call(owner, body, ...rest);
      call.durationMs = Date.now() - started;
      call.usage = usageOf(response as JsonValue);
      call.finishReason = finishOf(response as JsonValue);
      const record = response as JsonObject | null;
      call.responseId =
        record !== null && typeof record === "object" && typeof record.id === "string" ? record.id : null;
      if (Object.keys(call.usage).length === 0) {
        call.note("The provider returned no usage, so this call's token counts are unknown rather than zero.");
      }
      return response;
    } catch (error) {
      call.durationMs = Date.now() - started;
      call.error = error instanceof Error ? error.message.slice(0, 512) : String(error).slice(0, 512);
      call.errorType = error instanceof Error ? error.constructor.name : "unknown";
      throw error;
    } finally {
      options.scope.record(call);
    }
  };
}

type Creator = { create?: unknown };

/**
 * The client, instrumented in place by property. Returns the SAME object, so a caller that already
 * holds a reference to it keeps a working one.
 *
 * Recognised by shape: `chat.completions.create` is OpenAI's chat API, `messages.create` is
 * Anthropic's, `responses.create` is OpenAI's responses API. A client with none of the three is
 * returned untouched and says so, because silently returning an uninstrumented client is how a
 * customer ends up believing they have observability they do not have.
 */
export function wrapClient<T extends object>(client: T, options: WrapOptions): T {
  const record = client as unknown as Record<string, unknown>;
  const host = providerHostOf(client);
  const chat = record.chat as { completions?: Creator } | undefined;
  const completions = chat?.completions;
  if (completions !== undefined && typeof completions.create === "function") {
    completions.create = instrument(
      completions.create as (body: JsonObject, ...rest: unknown[]) => Promise<unknown>,
      completions,
      "openai",
      "chat.completions",
      options,
      host,
    );
    return client;
  }
  const responses = record.responses as Creator | undefined;
  if (responses !== undefined && typeof responses.create === "function") {
    responses.create = instrument(
      responses.create as (body: JsonObject, ...rest: unknown[]) => Promise<unknown>,
      responses,
      "openai",
      "responses",
      options,
      host,
    );
    return client;
  }
  const messages = record.messages as Creator | undefined;
  if (messages !== undefined && typeof messages.create === "function") {
    messages.create = instrument(
      messages.create as (body: JsonObject, ...rest: unknown[]) => Promise<unknown>,
      messages,
      "anthropic",
      "messages",
      options,
      host,
    );
    return client;
  }
  throw new TypeError(
    "wrap() was handed an object with no chat.completions.create, responses.create or messages.create: " +
      "it is not an openai or @anthropic-ai/sdk client, and returning it uninstrumented would leave you " +
      "believing calls were being recorded",
  );
}
