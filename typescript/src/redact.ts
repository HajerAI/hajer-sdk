/**
 * Remove what must not travel, in the customer's own process, before anything is sent.
 *
 * The same rule set as the Python SDK and the service (`rules.ts`, generated from the backend's
 * committed catalog by the same generator), applied one hop earlier, so the value does not leave the
 * process at all.
 *
 * **It never throws into application code.** That is the SDK's whole contract: `verify` resolves to
 * `unavailable`, and a redaction pass that threw would turn Hajer into the reason somebody's email
 * did not send. So every failure here **degrades and records**: an unknown validator, a document
 * past one of the budgets — the pass does what it can, sets `degraded` to the sentence that says
 * why, and the wire carries it to the assessment's `limitations`. A caller who believes their
 * payload was redacted and whose pass degraded is told.
 *
 * **The walk is iterative, under four budgets.** A recursive walk over a customer's own document is
 * a stack overflow somebody else's process hits, and this runs inside their request path:
 * `REDACT_MAX_DEPTH` bounds nesting, `REDACT_MAX_NODES` the number of values visited,
 * `REDACT_MAX_BYTES` the whole document and `REDACT_MAX_STRING_CHARS` one value. Past any of them
 * the pass stops scanning and says so; it never silently returns a document it did not finish.
 *
 * **Placeholders match nothing.** `redact(redact(x))` equals `redact(x)`, asserted by the tests,
 * because the wire can carry a document the caller already passed through this pass and a second
 * pass that re-redacted its own placeholders would inflate every count.
 */

import { VALIDATORS } from "./checksums.ts";
import type { JsonObject, JsonValue } from "./json.ts";
import { matched, parsePaths, type Path } from "./jsonpath.ts";
import { CATALOG_ID, RULES, type Rule } from "./rules.ts";
import {
  REDACT_MAX_BYTES,
  REDACT_MAX_DEPTH,
  REDACT_MAX_NODES,
  REDACT_MAX_STRING_CHARS,
} from "./settings.ts";

/** What replaces a value the pass could not scan at all — *we did not read this*. */
export const UNSCANNED_PLACEHOLDER = "[redacted:UNSCANNED]";
export const UNSCANNED_CATEGORY = "UNSCANNED";

/** A rough per-node cost for the punctuation around a value: over-counts, so the budget binds early. */
const NODE_OVERHEAD_BYTES = 4;

/** One value that was changed: where it was, what class was found, and how many times. */
export interface RedactionEntry {
  readonly category: string;
  readonly path: string;
  readonly count: number;
}

/** What this process removes, and what it leaves alone. */
export interface ClientRedactionPolicy {
  readonly classesOff: ReadonlySet<string>;
  readonly extraRules: readonly Rule[];
  readonly exempt: readonly Path[];
}

export interface PolicyInput {
  /** A class this team has decided about — `ACCOUNT_LIKE` for order ids written in groups of four. */
  readonly classesOff?: readonly string[];
  /** A team's own shapes, as `[category, pattern]`. Compiled HERE, where they are written. */
  readonly extraRules?: readonly (readonly [string, string])[];
  /** Paths the pass must not touch, so a check that compares an account number can still read one. */
  readonly pathsExempt?: readonly string[];
}

/**
 * One policy with its extra patterns compiled and its exempt paths parsed, or a refusal.
 *
 * Called once, where the policy is written — the two things that can be wrong with a policy (a
 * pattern that does not compile, a path the grammar cannot read) are the caller's mistakes and
 * belong at the point they are made. Inside `verify` there is no good answer to either.
 */
export function buildPolicy(input: PolicyInput = {}): ClientRedactionPolicy {
  const extraRules = (input.extraRules ?? []).map(([category, pattern]): Rule => {
    let compiled: RegExp;
    try {
      compiled = new RegExp(pattern, "g");
    } catch (error) {
      throw new SyntaxError(
        `the extra redaction rule for ${category} is not a regular expression: ${String(error)}`,
      );
    }
    return {
      ruleId: `extra:${category}`,
      category,
      pattern: compiled,
      validator: "NONE",
      placeholder: `[redacted:${category}]`,
      phase: "EXTRA",
      defaultOn: true,
    };
  });
  return {
    classesOff: new Set(input.classesOff ?? []),
    extraRules,
    exempt: parsePaths(input.pathsExempt ?? []),
  };
}

/** Every rule this policy runs, in order: the catalog's, then the team's own. */
export function rulesOf(policy: ClientRedactionPolicy): readonly Rule[] {
  return [
    ...RULES.filter((rule) => rule.defaultOn && !policy.classesOff.has(rule.category)),
    ...policy.extraRules.filter((rule) => !policy.classesOff.has(rule.category)),
  ];
}

/** The report the wire carries: counts and a sentence; never a value and never a path. */
export interface ClientRedactionReport {
  readonly catalog: string;
  readonly countsByClass: readonly { readonly category: string; readonly count: number }[];
  readonly degraded?: string;
}

interface Budget {
  nodes: number;
  bytes: number;
  degraded: string | null;
}

function scanned(text: string, rules: readonly Rule[], budget: Budget): { value: string; found: Map<string, number> } {
  const found = new Map<string, number>();
  if (text.length > REDACT_MAX_STRING_CHARS) {
    budget.degraded ??= `a value of ${text.length} characters is past the ${REDACT_MAX_STRING_CHARS}-character scan bound and was replaced unscanned`;
    found.set(UNSCANNED_CATEGORY, 1);
    return { value: UNSCANNED_PLACEHOLDER, found };
  }
  let value = text;
  for (const rule of rules) {
    const validate = VALIDATORS[rule.validator];
    if (validate === undefined) {
      budget.degraded ??= `rule ${rule.ruleId} names the validator ${rule.validator}, which this client does not carry, so that rule did not run`;
      continue;
    }
    let count = 0;
    // `replace` with a function is the one pass that can consult the checksum per match: a match the
    // validator rejects is written back exactly as it was, so a nine-digit number that is not a SIN
    // survives.
    value = value.replace(new RegExp(rule.pattern.source, rule.pattern.flags), (match: string) => {
      if (!validate(match)) return match;
      count += 1;
      return rule.placeholder;
    });
    if (count > 0) found.set(rule.category, (found.get(rule.category) ?? 0) + count);
  }
  return { value, found };
}

/**
 * One submission redacted, and the report that says what was removed — or `null` when nothing was.
 *
 * The pair is always the same shape whatever happened: a caller that had to check which of two
 * results it got would be a caller with two code paths over one answer.
 */
export function redactSubmission(
  document: JsonValue,
  policy: ClientRedactionPolicy,
): { readonly value: JsonValue; readonly report: ClientRedactionReport | null } {
  const rules = rulesOf(policy);
  const budget: Budget = { nodes: 0, bytes: 0, degraded: null };
  const counts = new Map<string, number>();

  const walk = (node: JsonValue, here: (string | number)[], depth: number): JsonValue => {
    budget.nodes += 1;
    if (budget.nodes > REDACT_MAX_NODES) {
      budget.degraded ??= `the document has more than ${REDACT_MAX_NODES} values; the pass stopped scanning there`;
      return node;
    }
    if (depth > REDACT_MAX_DEPTH) {
      budget.degraded ??= `the document nests deeper than ${REDACT_MAX_DEPTH} levels; the pass stopped scanning there`;
      return node;
    }
    if (budget.bytes > REDACT_MAX_BYTES) {
      budget.degraded ??= `the document is larger than ${REDACT_MAX_BYTES} bytes; the pass stopped scanning there`;
      return node;
    }
    if (Array.isArray(node)) {
      return node.map((item, position) => walk(item, [...here, position], depth + 1));
    }
    if (node !== null && typeof node === "object") {
      const out: JsonObject = {};
      for (const [key, item] of Object.entries(node)) {
        budget.bytes += key.length + NODE_OVERHEAD_BYTES;
        out[key] = walk(item as JsonValue, [...here, key], depth + 1);
      }
      return out;
    }
    if (typeof node !== "string") {
      budget.bytes += NODE_OVERHEAD_BYTES;
      return node;
    }
    budget.bytes += node.length + NODE_OVERHEAD_BYTES;
    if (matched(here, policy.exempt)) return node;
    const { value, found } = scanned(node, rules, budget);
    for (const [category, count] of found) counts.set(category, (counts.get(category) ?? 0) + count);
    return value;
  };

  const value = walk(document, [], 0);
  if (counts.size === 0 && budget.degraded === null) return { value, report: null };
  const report: ClientRedactionReport = {
    catalog: CATALOG_ID,
    countsByClass: [...counts.entries()]
      .sort(([left], [right]) => (left < right ? -1 : left > right ? 1 : 0))
      .map(([category, count]) => ({ category, count })),
    ...(budget.degraded === null ? {} : { degraded: budget.degraded }),
  };
  return { value, report };
}

/** Every entry a caller asked about, for a test or a `hajer doctor` that wants the detail. */
export function entriesOf(report: ClientRedactionReport | null): readonly RedactionEntry[] {
  if (report === null) return [];
  return report.countsByClass.map(({ category, count }) => ({ category, path: "", count }));
}
