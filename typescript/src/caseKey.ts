/**
 * The case key, derived here so that it is derived *before* anything rewrites the request.
 *
 * `contract/case-key-vectors.json` holds the shared vectors this module's tests read,
 * the Python SDK's read and the platform's read — so three implementations cannot drift without a red
 * test on all three sides.
 *
 * **Why the SDK derives it at all.** The server can derive one too, and does when the wire carries
 * none — but it can only read the request *as it arrives*, which is after client-side redaction has
 * replaced a matched value with a placeholder. A key derived there moves the moment a rule fires,
 * and two attempts at one case land in two groups. Derived here, before redaction, it does not move;
 * the row says `DERIVED_CLIENT` so a reader of a repeatability receipt knows which of the two it has.
 *
 * **A caller's own key beats both.** `verify(..., { caseKey: "case-alpha" })` is `CALLER`, and an
 * application that knows its own scenario id knows more than any digest of a payload can.
 */

import { createHash } from "node:crypto";

import { canonical, type JsonObject, type JsonValue } from "./json.ts";
import { parsePaths, without } from "./jsonpath.ts";

/**
 * `ck1` names the derivation, not the key: a change to what the digest covers is `ck2`, so two SDK
 * versions can never disagree silently about whether two requests are one case.
 */
export const CASE_KEY_SCHEME = "ck1";

/** Where a key came from. `DERIVED_SERVER` is the backend's own label and never sent from here. */
export type CaseKeySource = "CALLER" | "DERIVED_CLIENT";

/** One field of the seed, length-prefixed so two fields can never run together. */
function lengthPrefixed(part: string): Buffer {
  const encoded = Buffer.from(part, "utf8");
  return Buffer.concat([Buffer.from(`${encoded.length}:`, "utf8"), encoded]);
}

/** The request as the bytes a case key is taken over: canonical JSON, volatile paths removed. */
export function normalisedRequest(request: JsonValue, volatilePaths: readonly string[] = []): string {
  const document: JsonObject =
    request !== null && typeof request === "object" && !Array.isArray(request) ? request : {};
  const { value } = without(document, parsePaths(volatilePaths));
  return canonical(value !== null && typeof value === "object" && !Array.isArray(value) ? value : {});
}

/** One case key over one request. `ck1_<sha256>` — 68 characters, inside the server's bound. */
export function deriveCaseKey(request: JsonValue, volatilePaths: readonly string[] = []): string {
  const seed = Buffer.concat([
    lengthPrefixed(CASE_KEY_SCHEME),
    lengthPrefixed(normalisedRequest(request, volatilePaths)),
  ]);
  return `${CASE_KEY_SCHEME}_${createHash("sha256").update(seed).digest("hex")}`;
}

/**
 * The caller's case key, or one derived from the request here — with the label that says which.
 *
 * Called before anything rewrites the request, which is the whole reason it is called on this side
 * at all. `volatilePaths` is what the *caller* says varies per attempt; the verifier's own
 * declaration lives in its compiled document, which this process has never seen, and that asymmetry
 * is why `CALLER` is the strongest source of the three.
 */
export function caseIdentity(
  caseKey: string | null | undefined,
  request: JsonValue,
  volatilePaths: readonly string[] = [],
): { readonly key: string; readonly source: CaseKeySource } {
  if (caseKey !== null && caseKey !== undefined) return { key: caseKey, source: "CALLER" };
  return { key: deriveCaseKey(request, volatilePaths), source: "DERIVED_CLIENT" };
}
