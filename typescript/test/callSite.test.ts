/**
 * Where a wrapped call was made: V8 stack lines parsed into application frames, and the attach wire
 * carrying them — locations, never a value and nothing derived from the prompt, and on by default.
 */

import { createHash } from "node:crypto";
import { mkdtempSync, readFileSync, statSync, utimesSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { describe, expect, it, onTestFinished, vi } from "vitest";

import { CallScope, scope, wrapClient } from "../src/attach.ts";
import { Hajer } from "../src/client.ts";
import { defaults } from "../src/settings.ts";
import { TIMESTAMP_MARGIN_MS, fileDigest, parseStack, processStartedMs } from "../src/callSite.ts";
import type { JsonObject } from "../src/json.ts";

const CWD = "/srv/app";

/** A process start just after now's files were written, with the margin added: they predate it. */
function processStartsNow(): number {
  const settle = (): void => {
    const until = Date.now() + 20;
    while (Date.now() < until) {
      // wait out the filesystem clock
    }
  };
  settle();
  const now = Date.now();
  settle();
  return now + TIMESTAMP_MARGIN_MS;
}

describe("parseStack", () => {
  it("reads named, anonymous and async V8 frames, relative to the working directory", () => {
    const stack = [
      "Error",
      "    at evaluateAnswer (/srv/app/src/tools/evaluator.ts:12:5)",
      "    at /srv/app/src/index.ts:3:1",
      "    at async Runner.run (file:///srv/app/dist/runner.js:40:9)",
    ].join("\n");
    expect(parseStack(stack, CWD)).toEqual([
      { module: "src/tools/evaluator", qualname: "evaluateAnswer", file: "src/tools/evaluator.ts", line: 12 },
      { module: "src/index", qualname: "<anonymous>", file: "src/index.ts", line: 3 },
      { module: "dist/runner", qualname: "Runner.run", file: "dist/runner.js", line: 40 },
    ]);
  });

  it("skips node_modules, node internals and frames it cannot read", () => {
    const stack = [
      "Error",
      "    at OpenAI.create (/srv/app/node_modules/openai/core.js:88:3)",
      "    at process.processTicksAndRejections (node:internal/process/task_queues:95:5)",
      "    at <anonymous>",
      "    at chat (/srv/app/src/llm.ts:7:10)",
    ].join("\n");
    expect(parseStack(stack, CWD)).toEqual([
      { module: "src/llm", qualname: "chat", file: "src/llm.ts", line: 7 },
    ]);
  });

  it.each([
    ["/srv/app/a/b/c/d/e/f/g/h.ts", "/srv/app", "a/b/c/d/e/f/g/h.ts"],
    ["/home/alice/proj/src/llm.ts", "/", "<outside>/llm.ts"],
    ["/home/alice/bot.ts", "/tmp", "<outside>/bot.ts"],
    ["/Users/alice/code/proj/src/llm.ts", "/Users/alice/code/proj/notebooks", "<outside>/llm.ts"],
    ["/home/alice/proj/src/llm.ts", "/home", "<outside>/llm.ts"],
    ["/home/alice/proj/src/llm.ts", "/home/alice", "proj/src/llm.ts"],
    ["C:\\Users\\bob\\proj\\src\\app.ts", "C:\\Users\\bob\\proj", "src/app.ts"],
    ["C:\\Users\\bob\\proj\\src\\app.ts", "C:\\", "<outside>/app.ts"],
  ])("sends %s under root %s as %s: never absolute, never a home directory", (file, root, sent) => {
    expect(parseStack(`Error\n    at go (${file}:1:1)`, root)[0]?.file).toBe(sent);
  });
});

function openAiDouble(): { chat: { completions: { create: (body: JsonObject) => Promise<JsonObject> } } } {
  return { chat: { completions: { create: async (): Promise<JsonObject> => ({ id: "chatcmpl-1" }) } } };
}

const TERSE = { role: "system", content: "You are terse." };

describe("the attach wire", () => {
  it("carries the call site by default, and nothing derived from the system text", async () => {
    const scope = new CallScope(8);
    const client = Object.assign(openAiDouble(), { baseURL: "https://api.openai.com/v1" });
    wrapClient(client, { scope, captureContent: false, captureCallSite: true });
    await client.chat.completions.create({ model: "m", messages: [TERSE, { role: "user", content: "hi" }] });
    const wire = scope.take().calls[0]!.toWire();
    const frames = wire.callerFrames as JsonObject[];
    expect(frames.length).toBeGreaterThan(0);
    expect(frames[0]!.file).toBe("test/callSite.test.ts");
    expect(wire.systemDigest).toBeUndefined();
    expect(wire.providerHost).toBe("api.openai.com:443");
    expect(wire.content).toBeUndefined();
  });

  it.each([
    ["http://llm_proxy:8000/v1", "llm_proxy:8000"],
    ["https://bücher.example/v1", "xn--bcher-kva.example:443"],
  ])("names the endpoint %s as %s", async (baseURL, host) => {
    const scope = new CallScope(8);
    const client = Object.assign(openAiDouble(), { baseURL });
    wrapClient(client, { scope, captureContent: false, captureCallSite: true });
    await client.chat.completions.create({ model: "m", messages: [TERSE] });
    expect(scope.take().calls[0]!.toWire().providerHost).toBe(host);
  });

  it("takes an explicit null projectRoot as the working directory, never the environment", async () => {
    const saved = process.env.HAJER_PROJECT_ROOT;
    process.env.HAJER_PROJECT_ROOT = "/somewhere/else";
    try {
      const scope = new CallScope(8);
      const client = openAiDouble();
      wrapClient(client, { scope, captureContent: false, captureCallSite: true, projectRoot: null });
      await client.chat.completions.create({ model: "m", messages: [TERSE] });
      const frames = scope.take().calls[0]!.toWire().callerFrames as JsonObject[];
      expect(frames[0]!.file).toBe("test/callSite.test.ts");
    } finally {
      if (saved === undefined) delete process.env.HAJER_PROJECT_ROOT;
      else process.env.HAJER_PROJECT_ROOT = saved;
    }
  });

  it("sends no frames or host when call-site capture is off", async () => {
    const scope = new CallScope(8);
    const client = Object.assign(openAiDouble(), { baseURL: "https://api.openai.com/v1" });
    wrapClient(client, { scope, captureContent: false, captureCallSite: false });
    await client.chat.completions.create({ model: "m", messages: [TERSE] });
    const wire = scope.take().calls[0]!.toWire();
    expect(wire.callerFrames).toBeUndefined();
    expect(wire.systemDigest).toBeUndefined();
    expect(wire.providerHost).toBeUndefined();
  });

  it("honours the settings' captureCallSite on the Hajer path", async () => {
    const hajer = new Hajer({ settings: defaults({ captureCallSite: false }) });
    const client = hajer.wrap(Object.assign(openAiDouble(), { baseURL: "https://api.openai.com/v1" }));
    await client.chat.completions.create({ model: "m", messages: [TERSE] });
    const wire = hajer.scope().take().calls[0]!.toWire();
    expect(wire.callerFrames).toBeUndefined();
    expect(wire.systemDigest).toBeUndefined();
    expect(wire.providerHost).toBeUndefined();
  });
});

const OWN_DIGEST = `sha256:${createHash("sha256").update(readFileSync(new URL(import.meta.url))).digest("hex")}`;
const KEY = `source-call-${"a".repeat(64)}`;

describe("the frame's file digest", () => {
  it("is the sha256 of the frame's file bytes", async () => {
    // This file predates the (pretended) process start however recently it was saved.
    const started = processStartsNow();
    const origin = vi.spyOn(performance, "timeOrigin", "get").mockReturnValue(started);
    const uptime = vi.spyOn(process, "uptime").mockReturnValue((Date.now() - started) / 1000);
    onTestFinished(() => {
      origin.mockRestore();
      uptime.mockRestore();
    });
    const scopeOfCalls = new CallScope(8);
    const client = openAiDouble();
    wrapClient(client, { scope: scopeOfCalls, captureContent: false, captureCallSite: true });
    await client.chat.completions.create({ model: "m", messages: [TERSE] });
    const frames = scopeOfCalls.take().calls[0]!.toWire().callerFrames as JsonObject[];
    expect(frames[0]!.file).toBe("test/callSite.test.ts");
    expect(frames[0]!.fileDigest).toBe(OWN_DIGEST);
  });

  it("is sent only while the file holds the code that runs", () => {
    const dir = mkdtempSync(join(tmpdir(), "hajer-digest-"));
    const source = join(dir, "app.ts");
    writeFileSync(source, "export const a = 1;\n");
    const started = processStartsNow();
    expect(fileDigest(source, started)).toBe(`sha256:${createHash("sha256").update("export const a = 1;\n").digest("hex")}`);
    writeFileSync(source, "export const a = 2; // edited while running\n");
    expect(fileDigest(source, started)).toBeNull();
    const past = Date.now() / 1000 - 3600;
    utimesSync(source, past, past);
    expect(fileDigest(source, started)).toBeNull();
    const fresh = join(dir, "fresh.ts");
    writeFileSync(fresh, "export {};\n");
    expect(fileDigest(fresh, started)).toBeNull();
    expect(fileDigest(dir, started)).toBeNull();
  });

  it("is absent for a file replaced with its mtime preserved (rsync -a, cp -p, tar -x)", () => {
    const dir = mkdtempSync(join(tmpdir(), "hajer-digest-"));
    const kept = join(dir, "kept.ts");
    const replaced = join(dir, "replaced.ts");
    const past = Date.now() / 1000 - 3600;
    for (const path of [kept, replaced]) {
      writeFileSync(path, "export const which = 'R1 draft';\n");
      utimesSync(path, past, past);
    }
    const started = processStartsNow();
    writeFileSync(replaced, "export const which = 'R2 moderation';\n");
    utimesSync(replaced, past, past);
    const status = statSync(replaced);
    const horizon = started - TIMESTAMP_MARGIN_MS;
    expect(status.mtimeMs < horizon && horizon < status.ctimeMs).toBe(true);
    expect(fileDigest(kept, started)).toBe(`sha256:${createHash("sha256").update("export const which = 'R1 draft';\n").digest("hex")}`);
    expect(fileDigest(replaced, started)).toBeNull();
  });

  it("is compared against the process start, less a margin for coarse filesystem clocks", () => {
    const started = processStartedMs();
    expect(started).toBeLessThanOrEqual(performance.timeOrigin);
    expect(started).toBeLessThanOrEqual(Date.now() - process.uptime() * 1000 + 1);
    const dir = mkdtempSync(join(tmpdir(), "hajer-digest-"));
    const source = join(dir, "recent.ts");
    writeFileSync(source, "export {};\n");
    const changed = statSync(source).ctimeMs;
    expect(fileDigest(source, changed + TIMESTAMP_MARGIN_MS - 1)).toBeNull();
    expect(fileDigest(source, changed + TIMESTAMP_MARGIN_MS + 1)).not.toBeNull();
  });

  it("is absent for a frame outside the project root", () => {
    const path = new URL(import.meta.url).pathname;
    const [frame] = parseStack(`Error\n    at plan (${path}:6:3)`, "/nonexistent-project-root");
    expect(frame!.file).toBe("<outside>/callSite.test.ts");
    expect(frame).not.toHaveProperty("fileDigest");
  });

  it("is absent for a file that cannot be read", () => {
    const [frame] = parseStack("Error\n    at plan (/srv/app/src/agent.ts:6:3)", CWD);
    expect(frame!.file).toBe("src/agent.ts");
    expect(frame).not.toHaveProperty("fileDigest");
  });
});

describe("a declared workflow", () => {
  it("rides every wrapped call made inside the scope, awaited ones included, and nothing outside it", async () => {
    const calls = new CallScope(8);
    const client = openAiDouble();
    wrapClient(client, { scope: calls, captureContent: false, captureCallSite: false });
    await scope({ workflow: KEY }, async () => {
      await Promise.resolve();
      await client.chat.completions.create({ model: "m", messages: [TERSE] });
      await scope({}, () => client.chat.completions.create({ model: "m", messages: [TERSE] }));
    });
    await client.chat.completions.create({ model: "m", messages: [TERSE] });
    const wires = calls.take().calls.map((call) => call.toWire());
    expect(wires.map((wire) => wire.workflowHint)).toEqual([KEY, undefined, undefined]);
  });

  it("refuses a blank or over-long name when the scope opens", () => {
    expect(() => scope({ workflow: "   " }, () => 1)).toThrow(RangeError);
    expect(() => scope({ workflow: "x".repeat(513) }, () => 1)).toThrow(RangeError);
  });
});

