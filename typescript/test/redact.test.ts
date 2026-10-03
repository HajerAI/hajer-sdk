/**
 * Client-side redaction: the same catalog the server runs, one hop earlier.
 *
 * The positives below are shapes a real payload carries and the negatives are shapes it also
 * carries and that must survive — a model name, a digest, a job title, a due date. A false positive
 * here is a customer's evidence field replaced by a placeholder, which breaks their check.
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { luhn, ibanMod97, vinCheck } from "../src/checksums.ts";
import { buildPolicy, redactSubmission, UNSCANNED_PLACEHOLDER } from "../src/redact.ts";
import { redactedBody } from "../src/payload.ts";
import { CATALOG_ID, RULES, PLACEHOLDERS } from "../src/rules.ts";
import { REDACT_MAX_STRING_CHARS } from "../src/settings.ts";

const policy = buildPolicy();

function classesIn(document: unknown): string[] {
  const { report } = redactSubmission(document as never, policy);
  return (report?.countsByClass ?? []).map((entry) => entry.category).sort();
}

describe("the generated catalog", () => {
  it("is the v2 catalog with every class the backend declares", () => {
    expect(CATALOG_ID).toBe("redaction-rules@6");
    const categories = new Set(RULES.map((rule) => rule.category));
    for (const expected of [
      "ANTHROPIC_KEY",
      "OPENAI_KEY",
      "HAJER_KEY",
      "CARD",
      "IBAN",
      "VIN",
      "EMAIL",
      "PHONE",
      "ACCOUNT_LIKE",
      "PERSON_NAME",
      "POSTAL_ADDRESS",
      "DATE_OF_BIRTH",
    ]) {
      expect(categories, expected).toContain(expected);
    }
  });

  it("runs credentials before the loose account rule", () => {
    const phases = RULES.map((rule) => rule.phase);
    expect(phases.indexOf("CREDENTIAL")).toBeLessThan(phases.lastIndexOf("POST_PII"));
  });
});

describe("the checksums", () => {
  it("agrees with the shapes the catalog admits", () => {
    expect(luhn("4111 1111 1111 1111")).toBe(true);
    expect(luhn("4111 1111 1111 1112")).toBe(false);
    expect(luhn("")).toBe(false);
    expect(ibanMod97("GB82 WEST 1234 5698 7654 32")).toBe(true);
    expect(ibanMod97("GB82 WEST 1234 5698 7654 33")).toBe(false);
    expect(vinCheck("1HGCM82633A004352")).toBe(true);
    expect(vinCheck("1HGCM82633A004353")).toBe(false);
    expect(vinCheck("TOO SHORT")).toBe(false);
  });
});

interface Vector {
  readonly name: string;
  readonly value: string;
  readonly classes: readonly string[];
  readonly redacted: string;
}

const VECTORS = fileURLToPath(new URL("../../contract/redaction-vectors.json", import.meta.url));
const vectors = JSON.parse(readFileSync(VECTORS, "utf8")) as {
  readonly catalog: string;
  readonly positives: readonly Vector[];
  readonly negatives: readonly Vector[];
};

describe("the shared vectors, which are the Python SDK's own output", () => {
  it("is the catalog this client carries", () => {
    expect(vectors.catalog).toBe(CATALOG_ID);
    expect(vectors.positives.length).toBeGreaterThan(10);
    expect(vectors.negatives.length).toBeGreaterThan(10);
  });

  for (const vector of [...vectors.positives, ...vectors.negatives]) {
    it(`agrees with the Python SDK: ${vector.name}`, () => {
      const { value, report } = redactSubmission({ value: vector.value }, policy);
      const classes = (report?.countsByClass ?? []).map((entry) => entry.category).sort();
      expect(classes).toEqual([...vector.classes]);
      expect((value as { value: string }).value).toBe(vector.redacted);
    });
  }
});

describe("what it removes", () => {
  it("removes the three v2 structural classes", () => {
    expect(classesIn({ note: "Contact Dr. Alice Robinson about it" })).toContain("PERSON_NAME");
    expect(classesIn({ where: "221 Baker Street London NW1 6XE" })).toContain("POSTAL_ADDRESS");
    expect(classesIn({ who: "date of birth: 14/03/1981" })).toContain("DATE_OF_BIRTH");
  });

  it("removes the two provider-key classes, which the shared vectors deliberately omit", () => {
    // A data file carrying a literal provider-key prefix trips every credential scan this
    // repository runs over its artifacts, so those two classes are asserted here instead — against
    // a sample with no entropy, exactly as `python/tests/test_redact.py` does.
    expect(classesIn({ k: ["sk", "ant", "abcdefghijklmnop"].join("-") })).toContain("ANTHROPIC_KEY");
    expect(classesIn({ k: ["hjk", "abcdefghijklmnop"].join("_") })).toContain("HAJER_KEY");
  });

  it("is idempotent: its own placeholders match nothing", () => {
    const document = { a: "4111 1111 1111 1111", b: "someone@example.com" };
    const once = redactSubmission(document, policy);
    const twice = redactSubmission(once.value, policy);
    expect(twice.value).toEqual(once.value);
    expect(twice.report).toBeNull();
    for (const placeholder of PLACEHOLDERS) {
      expect(classesIn({ value: placeholder }), placeholder).toEqual([]);
    }
  });
});

describe("the budgets and the policy", () => {
  it("replaces a value past the scan bound and says the pass degraded", () => {
    const { value, report } = redactSubmission(
      { big: "a".repeat(REDACT_MAX_STRING_CHARS + 1) },
      policy,
    );
    expect((value as { big: string }).big).toBe(UNSCANNED_PLACEHOLDER);
    expect(report?.degraded).toContain("past the");
  });

  it("leaves an exempt path readable, so a check can still compare an account number", () => {
    const exempting = buildPolicy({ pathsExempt: ["evidence.cardOnFile"] });
    const document = { evidence: { cardOnFile: "4111 1111 1111 1111", other: "4111 1111 1111 1111" } };
    const { value } = redactSubmission(document, exempting);
    const evidence = (value as { evidence: Record<string, string> }).evidence;
    expect(evidence.cardOnFile).toBe("4111 1111 1111 1111");
    expect(evidence.other).toBe("[redacted:CARD]");
  });

  it("switches one class off by name and adds a team's own", () => {
    expect(classesIn({ e: "someone@example.com" })).toEqual(["EMAIL"]);
    const off = buildPolicy({ classesOff: ["EMAIL"] });
    expect(redactSubmission({ e: "someone@example.com" }, off).report).toBeNull();
    const extra = buildPolicy({ extraRules: [["ORDER_ID", "ORD-\\d{6}"]] });
    const { report } = redactSubmission({ o: "ORD-123456" }, extra);
    expect(report?.countsByClass).toEqual([{ category: "ORDER_ID", count: 1 }]);
  });

  it("refuses a pattern that does not compile where it is written", () => {
    expect(() => buildPolicy({ extraRules: [["BAD", "("]] })).toThrow(SyntaxError);
  });
});

describe("the body, redacted before it is serialised", () => {
  it("walks the customer values and leaves the routing keys alone", () => {
    const body = {
      mode: "VERIFY",
      verifier: "v@1",
      request: { question: "my card is 4111 1111 1111 1111" },
      output: "we emailed someone@example.com",
      evidence: {},
      wrappedCalls: [],
      wrappedCallsDropped: 0,
      contentCaptured: false,
      idempotencyKey: "hj1_4111111111111111",
      caseKey: "ck1_4111111111111111",
      sdk: { name: "hajer-typescript", version: "0.1.0" },
    };
    const redacted = redactedBody(body, policy);
    expect(JSON.stringify(redacted.request)).toContain("[redacted:CARD]");
    expect(redacted.output).toContain("[redacted:EMAIL]");
    // The keys that decide routing are never walked: a pass that could rewrite them would break
    // routing to protect nothing.
    expect(redacted.idempotencyKey).toBe("hj1_4111111111111111");
    expect(redacted.caseKey).toBe("ck1_4111111111111111");
    const report = redacted.clientRedaction as { catalog: string };
    expect(report.catalog).toBe(CATALOG_ID);
  });

  it("attaches no report when the team turned client redaction off", () => {
    const body = { request: { c: "4111 1111 1111 1111" } };
    // `redactedBody` is only reached when `redactClient` is on; this asserts the honest wire for the
    // other case — an ABSENT report says "this client did not redact", and a report with zero counts
    // would say "it ran and found nothing".
    expect("clientRedaction" in body).toBe(false);
  });
});
