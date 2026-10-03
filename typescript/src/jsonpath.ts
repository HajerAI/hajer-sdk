/**
 * One path grammar, the same subset the Hajer platform declares, for every place that names a value.
 *
 *     path    := [ "$" ] [ "." ] step { ( "." step ) | index }
 *     step    := name
 *     index   := "[" ( digits | "*" ) "]"
 *     name    := [A-Za-z0-9_-]+
 *
 * The platform's path grammar is the authority and this is the third
 * implementation of it, held together by shared test data rather than by an import. Two things point
 * at a value from this side — the case-key normaliser's volatile paths and client redaction's exempt
 * paths — and both are declarations a customer writes, so a grammar that silently missed what they
 * meant would leave them believing a field was protected or ignored when it was not.
 *
 * A path that matches nothing is not an error; absence is the normal case. Nothing here mutates its
 * input.
 */

import type { JsonValue } from "./json.ts";

const NAME = /^[A-Za-z0-9_-]+$/u;

export class PathSyntaxError extends Error {}

/** One object key. */
export interface Name {
  readonly kind: "name";
  readonly key: string;
}

/** One array position, counted from zero. */
export interface Index {
  readonly kind: "index";
  readonly position: number;
}

/** Every position of an array. */
export interface Wildcard {
  readonly kind: "wildcard";
}

export type Step = Name | Index | Wildcard;
export type Path = readonly Step[];

function indexStep(text: string, whole: string): Step {
  const inner = text.slice(1, -1);
  if (inner === "*") return { kind: "wildcard" };
  if (!/^\d+$/u.test(inner)) {
    throw new PathSyntaxError(`${whole}: [${inner}] is neither a position nor *`);
  }
  return { kind: "index", position: Number(inner) };
}

/** One path, parsed. Raised where the path is WRITTEN, never inside `verify`. */
export function parsePath(text: string): Path {
  const trimmed = text.startsWith("$") ? text.slice(1) : text;
  const body = trimmed.startsWith(".") ? trimmed.slice(1) : trimmed;
  if (body === "") throw new PathSyntaxError(`${text}: a path names at least one step`);
  const steps: Step[] = [];
  for (const segment of body.split(".")) {
    if (segment === "") throw new PathSyntaxError(`${text}: an empty step`);
    const head = segment.match(/^[^[]*/u)?.[0] ?? "";
    if (head !== "") {
      if (!NAME.test(head)) throw new PathSyntaxError(`${text}: ${head} is not a name`);
      steps.push({ kind: "name", key: head });
    }
    const rest = segment.slice(head.length);
    const brackets = rest.match(/\[[^\]]*\]/gu) ?? [];
    if (brackets.join("") !== rest) throw new PathSyntaxError(`${text}: ${rest} is not an index`);
    for (const bracket of brackets) steps.push(indexStep(bracket, text));
  }
  return steps;
}

export function parsePaths(texts: readonly string[]): readonly Path[] {
  return texts.map(parsePath);
}

/** Whether `here` — the concrete path of a value reached in a walk — is matched by any of `paths`. */
export function matched(here: readonly (string | number)[], paths: readonly Path[]): boolean {
  return paths.some((path) => matchesOne(here, path));
}

function matchesOne(here: readonly (string | number)[], path: Path): boolean {
  if (here.length !== path.length) return false;
  return path.every((step, position) => {
    const actual = here[position];
    if (step.kind === "name") return actual === step.key;
    if (step.kind === "index") return actual === step.position;
    return typeof actual === "number";
  });
}

/**
 * `document` with every value the paths name removed, and the paths that removed one.
 *
 * Used by the case-key normaliser for volatile paths. A copy: nothing here mutates the caller's
 * document, because the caller is about to send it.
 */
export function without(
  document: JsonValue,
  paths: readonly Path[],
): { readonly value: JsonValue; readonly removed: readonly string[] } {
  if (paths.length === 0) return { value: document, removed: [] };
  const removed: string[] = [];
  const walk = (node: JsonValue, here: (string | number)[]): JsonValue => {
    if (Array.isArray(node)) {
      const kept: JsonValue[] = [];
      node.forEach((item, position) => {
        const at = [...here, position];
        if (matched(at, paths)) {
          removed.push(at.join("."));
          return;
        }
        kept.push(walk(item, at));
      });
      return kept;
    }
    if (node !== null && typeof node === "object") {
      const kept: Record<string, JsonValue> = {};
      for (const [key, item] of Object.entries(node)) {
        const at = [...here, key];
        if (matched(at, paths)) {
          removed.push(at.join("."));
          continue;
        }
        kept[key] = walk(item as JsonValue, at);
      }
      return kept;
    }
    return node;
  };
  return { value: walk(document, []), removed };
}
