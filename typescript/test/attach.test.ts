/**
 * `wrap(client)` — the model calls are recorded beside the answer.
 *
 * Fakes only, and that is the point: the SDK never imports `openai` or `@anthropic-ai/sdk`, so the
 * doubles below are exactly what it sees of a real client — an object with a `create` at a known
 * property. A test that needed the real package to prove `wrap` works would be a test proving the
 * opposite of what this module claims.
 */

import { describe, expect, it } from "vitest";

import { Hajer } from "../src/client.ts";
import { CallScope, wrapClient } from "../src/attach.ts";
import { defaults } from "../src/settings.ts";
import type { JsonObject } from "../src/json.ts";

function openAiDouble(response: JsonObject) {
  let seen: JsonObject | null = null;
  return {
    client: {
      chat: {
        completions: {
          create: async (body: JsonObject): Promise<JsonObject> => {
            seen = body;
            return response;
          },
        },
      },
    },
    seen: () => seen,
  };
}

function anthropicDouble(response: JsonObject) {
  return {
    messages: {
      // The body is accepted and ignored: a double whose `create` took no arguments would not be
      // the shape `wrap` instruments, and the type checker is what says so.
      create: async (_body: JsonObject): Promise<JsonObject> => response,
    },
  };
}

describe("wrap", () => {
  it("records an openai chat completion as a summary, with no content by default", async () => {
    const scope = new CallScope(32);
    const double = openAiDouble({
      id: "chatcmpl-1",
      choices: [{ finish_reason: "stop", message: { content: "hi" } }],
      usage: { prompt_tokens: 11, completion_tokens: 3 },
    });
    const wrapped = wrapClient(double.client, { scope, captureContent: false });
    await wrapped.chat.completions.create({
      model: "gpt-5",
      temperature: 0.2,
      messages: [{ role: "system", content: "s" }, { role: "user", content: "u" }],
      tools: [{ function: { name: "lookup" } }],
    });
    const { calls } = scope.take();
    expect(calls).toHaveLength(1);
    const call = calls[0].toWire();
    expect(call.provider).toBe("openai");
    expect(call.api).toBe("chat.completions");
    expect(call.model).toBe("gpt-5");
    expect(call.messageCount).toBe(2);
    expect(call.messageRoles).toEqual(["system", "user"]);
    expect(call.declaredTools).toEqual(["lookup"]);
    expect(call.usage).toEqual({ prompt_tokens: 11, completion_tokens: 3 });
    expect(call.finishReason).toBe("stop");
    expect(call.responseId).toBe("chatcmpl-1");
    // No message text and no tool arguments: shapes, names, counts, timing and usage only.
    expect(JSON.stringify(call)).not.toContain('"u"');
    expect(call.limitations).toContain(
      "Content capture is off: no message text, tool arguments or tool results were recorded.",
    );
    // The provider's own body reached the provider unchanged.
    expect(double.seen()?.model).toBe("gpt-5");
  });

  it("records an anthropic messages call", async () => {
    const scope = new CallScope(32);
    const wrapped = wrapClient(anthropicDouble({ id: "msg_1", stop_reason: "end_turn" }), {
      scope,
      captureContent: false,
    });
    await wrapped.messages.create({ model: "claude-sonnet-5", messages: [{ role: "user", content: "u" }] });
    const call = scope.take().calls[0].toWire();
    expect(call.provider).toBe("anthropic");
    expect(call.api).toBe("messages");
    expect(call.finishReason).toBe("end_turn");
  });

  it("records a failed call and re-raises it, because the application's error is the application's", async () => {
    const scope = new CallScope(32);
    const wrapped = wrapClient(
      {
        messages: {
          create: async (_body: JsonObject): Promise<never> => {
            throw new Error("529 overloaded");
          },
        },
      },
      { scope, captureContent: false },
    );
    await expect(wrapped.messages.create({})).rejects.toThrow("529 overloaded");
    const call = scope.take().calls[0].toWire();
    expect(call.error).toBe("529 overloaded");
    expect(call.errorType).toBe("Error");
  });

  it("refuses an object that is neither, rather than returning it uninstrumented", () => {
    expect(() => wrapClient({ nothing: true }, { scope: new CallScope(1), captureContent: false })).toThrow(
      TypeError,
    );
  });

  it("counts what it drops past the bound rather than growing without one", async () => {
    const scope = new CallScope(1);
    const wrapped = wrapClient(anthropicDouble({ id: "m" }), { scope, captureContent: false });
    await wrapped.messages.create({});
    await wrapped.messages.create({});
    const taken = scope.take();
    expect(taken.calls).toHaveLength(1);
    expect(taken.dropped).toBe(1);
  });
});

describe("the calls ride on the next verify", () => {
  it("attaches them to the submission and drains the scope", async () => {
    let body: JsonObject | null = null;
    const client = new Hajer({
      settings: defaults({ apiKey: "k", teamId: "t", baseUrl: "http://localhost:1" }),
      fetch: (async (_url: string, init: RequestInit) => {
        body = JSON.parse(String(init.body)) as JsonObject;
        return new Response(JSON.stringify({ status: "unavailable", reason: "SHADOW" }), { status: 200 });
      }) as unknown as typeof globalThis.fetch,
    });
    const wrapped = client.wrap(anthropicDouble({ id: "msg_1", usage: { input_tokens: 4 } }));
    await wrapped.messages.create({ model: "claude-sonnet-5", messages: [{ role: "user", content: "u" }] });
    await client.verify("v@1", { a: 1 }, "out");
    expect((body as unknown as { wrappedCalls: unknown[] }).wrappedCalls).toHaveLength(1);

    // Drained: a second verify does not re-send the first's calls.
    await client.verify("v@1", { a: 2 }, "out");
    expect((body as unknown as { wrappedCalls: unknown[] }).wrappedCalls).toHaveLength(0);
  });

  it("drains the scope even when the client is inert, so a keyless process does not leak", async () => {
    const client = new Hajer({ settings: defaults() });
    const wrapped = client.wrap(anthropicDouble({ id: "m" }));
    await wrapped.messages.create({});
    expect(client.scope().calls).toHaveLength(1);
    await client.verify("v@1", {}, "out");
    expect(client.scope().calls).toHaveLength(0);
  });
});
