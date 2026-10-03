/**
 * Inertness, and `verify` never rejecting — the two properties that let this package land in a
 * repository whose test suite has no Hajer credentials and pass unchanged.
 *
 * Fakes only. No socket is opened by anything in this file: the transport is a function, and the
 * assertion that an inert client opens none is that the function is never called.
 */

import { describe, expect, it } from "vitest";

import { Hajer, AssessmentUnavailableError } from "../src/client.ts";
import { defaults, fromEnv, HajerConfigError } from "../src/settings.ts";

function counting(response: () => Promise<Response>) {
  let calls = 0;
  const fetch = async (): Promise<Response> => {
    calls += 1;
    return response();
  };
  return { fetch: fetch as unknown as typeof globalThis.fetch, calls: () => calls };
}

const keyed = defaults({ apiKey: "k", teamId: "t", baseUrl: "http://localhost:1" });

describe("inert without a key", () => {
  it("is inert with no key, no team, or HAJER_DISABLED", () => {
    expect(fromEnv({}).inert).toBe(true);
    expect(fromEnv({ HAJER_API_KEY: "k" }).inert).toBe(true);
    expect(fromEnv({ HAJER_TEAM_ID: "t" }).inert).toBe(true);
    expect(fromEnv({ HAJER_API_KEY: "k", HAJER_TEAM_ID: "t" }).inert).toBe(false);
    expect(fromEnv({ HAJER_API_KEY: "k", HAJER_TEAM_ID: "t", HAJER_DISABLED: "1" }).inert).toBe(true);
  });

  it("answers unavailable{DISABLED} and opens no socket", async () => {
    const transport = counting(async () => new Response("{}", { status: 200 }));
    const client = new Hajer({ settings: defaults(), fetch: transport.fetch });
    const assessment = await client.verify("v@1", { a: 1 }, "out");
    expect(assessment.status).toBe("unavailable");
    expect(assessment.reason).toBe("DISABLED");
    expect(assessment.decidedLocally).toBe(true);
    expect(transport.calls()).toBe(0);
  });

  it("queues nothing from observe and returns a disabled receipt", async () => {
    const transport = counting(async () => new Response("{}", { status: 200 }));
    const client = new Hajer({ settings: defaults(), fetch: transport.fetch });
    const receipt = client.observe("v@1", { a: 1 }, "out");
    expect(receipt.state).toBe("disabled");
    expect(client.queueDepth()).toBe(0);
    await client.close();
    expect(transport.calls()).toBe(0);
  });

  it("refuses a malformed setting where it is written, not later in a call", () => {
    expect(() => fromEnv({ HAJER_DEADLINE_MS_DEFAULT: "soon" })).toThrow(HajerConfigError);
    expect(() => fromEnv({ HAJER_REDACT_CLIENT: "maybe" })).toThrow(HajerConfigError);
    // A MISSING key is not a configuration error: inert is a supported mode.
    expect(() => fromEnv({})).not.toThrow();
  });
});

describe("verify never rejects", () => {
  it("turns a transport failure into unavailable{TRANSPORT}", async () => {
    const client = new Hajer({
      settings: keyed,
      fetch: (async () => {
        throw new Error("ECONNREFUSED");
      }) as unknown as typeof globalThis.fetch,
    });
    const assessment = await client.verify("v@1", { a: 1 }, "out");
    expect(assessment.status).toBe("unavailable");
    expect(assessment.reason).toBe("TRANSPORT");
    expect(assessment.decidedLocally).toBe(true);
  });

  it("turns a 500 into unavailable{HTTP_STATUS}", async () => {
    const transport = counting(async () => new Response("no", { status: 500 }));
    const client = new Hajer({ settings: keyed, fetch: transport.fetch });
    const assessment = await client.verify("v@1", { a: 1 }, "out");
    expect(assessment.reason).toBe("HTTP_STATUS");
  });

  it("turns an unreadable body into unavailable{MALFORMED_RESPONSE}", async () => {
    const transport = counting(async () => new Response("not json", { status: 200 }));
    const client = new Hajer({ settings: keyed, fetch: transport.fetch });
    const assessment = await client.verify("v@1", { a: 1 }, "out");
    expect(assessment.reason).toBe("MALFORMED_RESPONSE");
  });

  it("refuses a body over the bound locally, before the socket", async () => {
    const transport = counting(async () => new Response("{}", { status: 200 }));
    const client = new Hajer({
      settings: defaults({ apiKey: "k", teamId: "t", bodyMaxBytes: 64 }),
      fetch: transport.fetch,
    });
    const assessment = await client.verify("v@1", { a: "x".repeat(500) }, "out");
    expect(assessment.reason).toBe("BODY_OVER_BOUND");
    expect(transport.calls()).toBe(0);
  });

  it("throws only when the caller opted in", async () => {
    const client = new Hajer({
      settings: defaults(),
      raiseOnUnavailable: true,
      fetch: (async () => new Response("{}", { status: 200 })) as unknown as typeof globalThis.fetch,
    });
    await expect(client.verify("v@1", {}, "out")).rejects.toBeInstanceOf(AssessmentUnavailableError);
  });
});

describe("the bounded observe queue", () => {
  it("refuses the newest past its bound rather than growing", async () => {
    const transport = counting(async () => new Response("{}", { status: 202 }));
    const client = new Hajer({
      settings: defaults({ apiKey: "k", teamId: "t", observeQueueMax: 2 }),
      fetch: transport.fetch,
    });
    expect(client.observe("v@1", { n: 1 }, "a").state).toBe("queued");
    expect(client.observe("v@1", { n: 2 }, "b").state).toBe("queued");
    const refused = client.observe("v@1", { n: 3 }, "c");
    expect(refused.state).toBe("refused");
    expect(refused.detail).toContain("bound of 2");
    expect(client.queueDepth()).toBe(2);
  });

  it("marks what it sent and keeps going when a flush fails", async () => {
    let ok = false;
    const client = new Hajer({
      settings: defaults({ apiKey: "k", teamId: "t", observeBatchMax: 1 }),
      fetch: (async () => {
        ok = !ok;
        return new Response("{}", { status: ok ? 202 : 500 });
      }) as unknown as typeof globalThis.fetch,
    });
    const first = client.observe("v@1", { n: 1 }, "a");
    const second = client.observe("v@1", { n: 2 }, "b");
    await client.flush();
    expect([first.state, second.state].sort()).toEqual(["failed", "sent"]);
    expect(client.queueDepth()).toBe(0);
  });
});
