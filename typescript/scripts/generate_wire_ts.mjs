#!/usr/bin/env node
//
// Generate `src/wire.ts` from the platform's OpenAPI snapshot (`contract/openapi.json`).
//
//     node scripts/generate_wire_ts.mjs --write    # rewrite src/wire.ts
//     node scripts/generate_wire_ts.mjs --check    # exit 1 on drift (`just check`)
//
// The snapshot is the one serialization of the API and nothing about the wire is hand-written: the
// Python SDK generates `hajer/_wire.py` from the same file.
//
// What is emitted is `openapi-typescript`'s own `paths`/`components` output, plus a short hand-written
// tail of ALIASES onto the operations the SDK calls — named in `SDK_OPERATIONS` below, the same ones
// `python/hajer/_paths.py` names. An alias resolves through `paths`, so an operation the platform
// renames or a body field it drops is a TypeScript error in this package rather than a 422 in somebody's
// request path.

import { createHash } from "node:crypto";
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const PACKAGE = resolve(HERE, "..");
const SNAPSHOT = resolve(PACKAGE, "..", "contract", "openapi.json");
const TARGET = resolve(PACKAGE, "src", "wire.ts");
const GENERATOR = resolve(PACKAGE, "node_modules", "openapi-typescript", "dist", "index.mjs");
const REMEDY = "run `just generate-wire` in typescript/ and commit src/wire.ts with the change";

// The four calls the SDK makes, as `[method, path]`. The same four as `hajer/_paths.py`'s
// `SDK_OPERATIONS`, and deliberately a list rather than "every operation in the snapshot": a client
// that generated types for routes it never calls would fail to compile over a change it does not
// make.
const SDK_OPERATIONS = [
  ["post", "/api/teams/{team_id}/verify", "Verify"],
  ["post", "/api/teams/{team_id}/observe", "Observe"],
  ["get", "/api/teams/{team_id}/observations", "Observations"],
  ["get", "/api/teams/{team_id}/observations/{observation_id}/assessment", "AssessmentPoll"],
];

function fail(message) {
  process.stderr.write(`generate_wire_ts: ${message}\n`);
  process.stderr.write(`generate_wire_ts: ${REMEDY}\n`);
  process.exit(1);
}

function digestOf(bytes) {
  return `sha256:${createHash("sha256").update(bytes).digest("hex")}`;
}

/**
 * The success status one operation answers with, taken from the snapshot rather than assumed.
 *
 * `POST /observe` answers 202 and `POST /verify` answers 200, so an alias that hard-coded either
 * would be wrong about the other — and wrong in the shape that fails at COMPILE time here, which is
 * how this was found. The first 2xx in the document is the operation's success response.
 */
function successStatus(operation) {
  const statuses = Object.keys(operation.responses ?? {})
    .filter((code) => /^2\d\d$/u.test(code))
    .sort();
  return statuses[0] ?? null;
}

/** Every operation the SDK calls, or a refusal naming the ones the snapshot does not declare. */
function declaredOperations(spec) {
  const missing = SDK_OPERATIONS.filter(([method, path]) => !spec.paths?.[path]?.[method]);
  const declared = [];
  for (const [method, path, alias] of SDK_OPERATIONS) {
    const operation = spec.paths?.[path]?.[method];
    if (!operation) continue;
    const status = successStatus(operation);
    if (status === null) {
      fail(`${method.toUpperCase()} ${path} declares no 2xx response, so it has no success body to type`);
    }
    declared.push([method, path, alias, status]);
  }
  return {
    missing: missing.map(([method, path]) => `${method.toUpperCase()} ${path}`),
    declared,
  };
}

/**
 * The aliases a hand-written module would otherwise spell for itself.
 *
 * Each one resolves THROUGH `paths`, never through a copied schema name: the operation is the
 * contract and the schema behind it is an implementation detail of the snapshot. `never` for an
 * operation the snapshot does not declare, with the reason on the line, so a client compiled
 * against an older platform fails where the type is used rather than where it is defined.
 */
function aliases(declared) {
  const byAlias = new Map(declared.map((row) => [row[2], row]));
  const lines = [];
  for (const [method, path, alias] of SDK_OPERATIONS) {
    const row = byAlias.get(alias);
    if (row === undefined) {
      lines.push(
        `/** ${method.toUpperCase()} ${path} — NOT in the snapshot this file was generated from. */`,
        `export type ${alias}In = never;`,
        `export type ${alias}Out = never;`,
        "",
      );
      continue;
    }
    const status = row[3];
    const operation = `paths[${JSON.stringify(path)}][${JSON.stringify(method)}]`;
    lines.push(
      `/** ${method.toUpperCase()} ${path} — success ${status} */`,
      `export type ${alias}Operation = ${operation};`,
      method === "post"
        ? `export type ${alias}In = ${alias}Operation["requestBody"] extends { content: { "application/json": infer B } } ? B : never;`
        : `export type ${alias}In = never;`,
      `export type ${alias}Out = ${alias}Operation["responses"][${status}] extends { content: { "application/json": infer R } } ? R : never;`,
      "",
    );
  }
  return lines.join("\n");
}

function header(digest, missing) {
  const note =
    missing.length === 0
      ? "Every operation the SDK calls is declared in it."
      : `The snapshot does not declare: ${missing.join(", ")} — those aliases are \`never\`.`;
  return `// GENERATED from the platform's OpenAPI snapshot by \`scripts/generate_wire_ts.mjs\`.
// Never hand-edited. Source: contract/openapi.json at
// ${digest}
//
// ${note}
//
// The type bodies below are \`openapi-typescript\`'s own output, at the version
// \`package.json\` exact-pins, run over the same bytes \`python/hajer/_wire.py\` is generated from.
// The aliases at the end resolve THROUGH \`paths\`, so a route the platform renames or a body field it drops is a compile error here rather
// than a 422 in somebody's request path.
//
// To regenerate: \`just generate-wire\` in typescript/.

/* eslint-disable */
`;
}

async function render() {
  let raw;
  try {
    raw = readFileSync(SNAPSHOT);
  } catch {
    fail(`the platform snapshot is not at ${SNAPSHOT}`);
  }
  const spec = JSON.parse(raw.toString("utf8"));
  if (!spec.openapi || !spec.paths) {
    fail("the snapshot declares no `openapi` or no `paths`, so there is no wire in it to generate from");
  }
  let generate;
  let astToString;
  try {
    ({ default: generate, astToString } = await import(pathToFileURL(GENERATOR).href));
  } catch {
    fail(
      "openapi-typescript is not installed; run `npm ci` in typescript/ first",
    );
  }
  // `astToString` is the generator's own printer, which is what its CLI uses too, so the output carries
  // no second opinion about how a type is spelled.
  const ast = await generate(spec, { defaultNonNullable: false });
  const { missing, declared } = declaredOperations(spec);
  return `${header(digestOf(raw), missing)}\n${astToString(ast)}\n${aliases(declared)}`;
}

const mode = process.argv[2];
if (mode !== "--write" && mode !== "--check") {
  process.stderr.write("usage: node scripts/generate_wire_ts.mjs --write|--check\n");
  process.exit(2);
}
const rendered = await render();
if (mode === "--write") {
  mkdirSync(dirname(TARGET), { recursive: true });
  writeFileSync(TARGET, rendered, "utf8");
  process.stderr.write(`generate_wire_ts: wrote ${TARGET}\n`);
} else {
  let current = "";
  try {
    current = readFileSync(TARGET, "utf8");
  } catch {
    fail("src/wire.ts is missing");
  }
  if (current !== rendered) {
    fail("src/wire.ts is stale or was hand-edited");
  }
  process.stderr.write("generate_wire_ts: OK (src/wire.ts is what the snapshot produces)\n");
}
