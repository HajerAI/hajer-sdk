/**
 * The five lines, the wire they produce, and the case key the whole repository shares.
 *
 * The body assertions go through `VerifyIn` — the alias `wire.ts` resolves out of the backend's own
 * guarded snapshot — so a field this SDK spells differently from the backend is a failure HERE
 * rather than a 422 in somebody's request path. The case-key assertions go through
 * `contract/case-key-vectors.json`, the same file the backend's tests and the
 * Python SDK's tests read, which is what keeps three implementations of one normaliser from
 * drifting.
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { Hajer } from "../src/client.ts";
import { deriveCaseKey, normalisedRequest } from "../src/caseKey.ts";
import { canonical, type JsonObject } from "../src/json.ts";
import { deriveIdempotencyKey, observationBody } from "../src/payload.ts";
import { defaults } from "../src/settings.ts";
import type { VerifyIn, VerifyOut } from "../src/wire.ts";

const VECTORS = fileURLToPath(
  new URL("../../contract/case-key-vectors.json", import.meta.url),
);

interface Vector {
  readonly name: string;
  readonly request: JsonObject;
  readonly volatilePaths: readonly string[];
  readonly caseKey: string;
}

const vectors = JSON.parse(readFileSync(VECTORS, "utf8")) as {
  readonly scheme: string;
  readonly vectors: readonly Vector[];
};

describe("the case key", () => {
  it("has vectors to check against", () => {
    expect(vectors.scheme).toBe("ck1");
    expect(vectors.vectors.length).toBeGreaterThan(4);
  });

  for (const vector of vectors.vectors) {
    it(`matches the shared vector: ${vector.name}`, () => {
      expect(deriveCaseKey(vector.request, vector.volatilePaths)).toBe(vector.caseKey);
    });
  }

  it("normalises key order away and leaves types alone", () => {
    expect(normalisedRequest({ b: 2, a: 1 })).toBe(normalisedRequest({ a: 1, b: 2 }));
    expect(normalisedRequest({ n: 5 })).not.toBe(normalisedRequest({ n: "5" }));
  });
});

describe("the body the five lines send", () => {
  it("is a VerifyIn", () => {
    // The assignment IS the assertion: `VerifyIn` comes from the backend's own snapshot, so this
    // line fails to compile if the body and the route disagree about a required field.
    const body: VerifyIn = {
      mode: "VERIFY",
      verifier: "refund-policy@1",
      request: { ticketId: "t-1", question: "where is my refund?" },
      output: "we have refunded you",
      evidence: { orderState: "REFUNDED" },
      idempotencyKey: "hj1_" + "0".repeat(64),
      caseKey: "ck1_" + "0".repeat(64),
      caseKeySource: "CALLER",
      contentCaptured: false,
      wrappedCalls: [],
      wrappedCallsDropped: 0,
      deadlineMs: 1500,
      sdk: { name: "hajer-typescript", version: "0.1.0" },
    };
    expect(body.mode).toBe("VERIFY");
  });

  it("carries the mode, the verifier and both case-key fields or neither", () => {
    const withKey = observationBody({
      mode: "VERIFY",
      verifier: "v@1",
      request: { a: 1 },
      output: "x",
      contentCaptured: false,
      idempotencyKey: "hj1_x",
      deadlineMs: 1500,
      caseKey: "ck1_x",
      caseKeySource: "DERIVED_CLIENT",
    });
    expect(withKey.caseKey).toBe("ck1_x");
    expect(withKey.caseKeySource).toBe("DERIVED_CLIENT");
    const without = observationBody({
      mode: "OBSERVE",
      verifier: null,
      request: { a: 1 },
      output: "x",
      contentCaptured: false,
      idempotencyKey: "hj1_x",
    });
    expect("caseKey" in without).toBe(false);
    expect("caseKeySource" in without).toBe(false);
    // An OBSERVE submission is off the response path and carries no deadline.
    expect("deadlineMs" in without).toBe(false);
    // `null` rather than an absent key: "no verifier" is a statement and an absent field is silence.
    expect(without.verifier).toBeNull();
  });

  it("derives one idempotency key per submission and a different one per payload", () => {
    const base = { teamId: "t", verifier: "v@1", request: { a: 1 }, output: "x" } as const;
    expect(deriveIdempotencyKey(base)).toBe(deriveIdempotencyKey(base));
    expect(deriveIdempotencyKey(base)).not.toBe(deriveIdempotencyKey({ ...base, output: "y" }));
    expect(deriveIdempotencyKey(base).startsWith("hj1_")).toBe(true);
  });
});

describe("what verify actually puts on the socket", () => {
  /**
   * The response this fake returns is `AssessmentOut` as the SERVICE spells it — the reason field is
   * `unavailableReason`, not `reason`. It was `reason` here for as long as nothing typed it, which
   * made a fake that agreed with a client that was wrong: the first live call came back
   * `unavailable` with a NULL reason, which is the shape of a client that cannot tell `SHADOW` from
   * `INTERNAL`. The `satisfies VerifyOut` below is what keeps the fake honest now — it is checked
   * against the backend's own snapshot, so a response the route would never send does not compile.
   */
  it("posts to the team-scoped route with the key and the idempotency header", async () => {
    const seen: { url?: string; init?: RequestInit } = {};
    const answer = {
      status: "unavailable",
      unavailableReason: "SHADOW",
      shadow: true,
      verifier: "v@1",
      observationId: "ing-0",
      checkOutcomes: [],
      findings: [],
      missingEvidence: [],
      costMicrousd: 0,
      latencyMs: 0,
      seam: "the verifier has not qualified",
      limitations: [],
    } satisfies VerifyOut;
    const client = new Hajer({
      settings: defaults({ apiKey: "secret-key", teamId: "team-9", baseUrl: "http://localhost:8140/" }),
      fetch: (async (url: string, init: RequestInit) => {
        seen.url = url;
        seen.init = init;
        return new Response(JSON.stringify(answer), { status: 200 });
      }) as unknown as typeof globalThis.fetch,
    });
    const assessment = await client.verify("v@1", { a: 1 }, "out", { b: 2 });
    expect(seen.url).toBe("http://localhost:8140/api/teams/team-9/verify");
    const headers = seen.init?.headers as Record<string, string>;
    expect(headers.authorization).toBe("Bearer secret-key");
    expect(headers["idempotency-key"]).toMatch(/^hj1_[0-9a-f]{64}$/u);
    const body = JSON.parse(String(seen.init?.body)) as JsonObject;
    expect(body.mode).toBe("VERIFY");
    expect(body.verifier).toBe("v@1");
    expect(body.caseKeySource).toBe("DERIVED_CLIENT");
    // The body is canonical JSON, which is the same serialization the case key is taken over.
    expect(String(seen.init?.body)).toBe(canonical(body));
    // `unavailable{SHADOW}` is the service's answer, not this process's.
    expect(assessment.status).toBe("unavailable");
    expect(assessment.reason).toBe("SHADOW");
    expect(assessment.shadow).toBe(true);
    expect(assessment.observationId).toBe("ing-0");
    expect(assessment.seam).toContain("qualified");
    expect(assessment.decidedLocally).toBe(false);
  });
});
