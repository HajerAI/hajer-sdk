/**
 * Where in the application a wrapped call was made: the V8 stack, read into application frames.
 *
 * A frame is the application's when its file is not under `node_modules`, not a `node:` internal and
 * not one of this SDK's own files. Everything left is code somebody in this process wrote, innermost
 * first. A frame is a location — module, qualified name, file, line — and never a value, which is why
 * it travels by default with content capture off (`HAJER_CAPTURE_CALL_SITE=0` turns it off).
 *
 * The file is only ever relative to the project root (`HAJER_PROJECT_ROOT`, else the working
 * directory); a file outside it is sent as `<outside>/<basename>`. A home directory (`/home/<user>`,
 * `/Users/<user>`, `C:\Users\<user>`) is replaced before anything is compared, so no root — not even
 * `/` — can put it on the wire.
 */

import { createHash } from "node:crypto";
import { readFileSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { CALL_SITE_MAX_FRAMES, CALL_SITE_STACK_SCANNED, FILE_DIGESTS_CACHED } from "./settings.ts";

/** One application frame, as `callerFrames` carries it on the wire. */
export interface CallerFrame {
  readonly module: string;
  readonly qualname: string;
  readonly file: string;
  readonly line: number;
  /**
   * `sha256:` of the frame's file bytes, read once per file per process: a digest of the application's own
   * source, never its text. The service reads the line only in the indexed revision with these bytes.
   * Absent when the file cannot be read.
   */
  readonly fileDigest?: string;
}

// `at fn (file:line:col)`, with V8's `async ` / `new ` prefixes, and `at file:line:col`.
const NAMED = /^\s*at (?:async )?(?:new )?(.+?) \((.+):(\d+):\d+\)$/;
const BARE = /^\s*at (?:async )?(.+):(\d+):\d+$/;
// Separators normalised like every frame's, so the SDK's own files are recognised on Windows too.
const OWN = fileURLToPath(new URL(".", import.meta.url)).replace(/\\/g, "/");
/** What a file outside the project root is sent as: this marker and its basename only. */
export const OUTSIDE_ROOT = "<outside>";
const HOME = /^(?:\/home\/[^/]+|\/Users\/[^/]+|\/root|[A-Za-z]:\/Users\/[^/]+)(?=\/|$)/;
const NO_ROOT = /^(?:[A-Za-z]:)?\/?$/;
const ANCHORED = /^(?:\/|~|[A-Za-z]:)/;

/** A file as it may leave the process: relative to the root, or `<outside>/<basename>`; never a home directory. */
function located(file: string, root: string): string {
  const path = file.replace(/\\/g, "/").replace(HOME, "~");
  const base = root.replace(/\\/g, "/").replace(HOME, "~").replace(/\/+$/, "");
  if (!NO_ROOT.test(base) && path.startsWith(`${base}/`)) return path.slice(base.length + 1);
  if (!ANCHORED.test(path) && !path.split("/").includes("..")) return path.replace(/^\.\//, "");
  return `${OUTSIDE_ROOT}/${path.split("/").pop() ?? ""}`;
}

/**
 * A file whose timestamps come within this much of the process start counts as written after it: filesystem
 * timestamps can be coarse (FAT's 2 s, some network filesystems' 1 s).
 */
export const TIMESTAMP_MARGIN_MS = 2_000;

/**
 * When this process started: `performance.timeOrigin` (the main thread's is the process start), or the start
 * `process.uptime()` implies when that is earlier (a worker thread's time origin is its own start).
 */
export function processStartedMs(): number {
  return Math.min(performance.timeOrigin, Date.now() - process.uptime() * 1000);
}

interface Sight {
  readonly mtimeMs: number;
  readonly ctimeMs: number;
  readonly size: number;
}

/** Each file's (mtime, ctime, size) when first seen, and its digest then. */
const SEEN = new Map<string, Sight & { readonly digest: string | null }>();

function sameSight(a: Sight, b: Sight): boolean {
  return a.mtimeMs === b.mtimeMs && a.ctimeMs === b.ctimeMs && a.size === b.size;
}

/**
 * `sha256:` of one source file's bytes, sent only while the file provably holds the code that runs: a regular
 * file (a FIFO or a device is never opened) whose `max(mtime, ctime)` is at or before the process start less
 * `TIMESTAMP_MARGIN_MS`, with the same (mtime, ctime, size) as when first seen. The ctime matters: the kernel
 * sets it on every write, rename or `utimes`, and nothing backdates it, so a file replaced by an
 * mtime-preserving writer (`rsync -a`, `cp -p`, `tar -x`) fails the check. Otherwise null, and a frame without
 * a digest never links. `startedMs` is the process start; only a test passes another.
 */
export function fileDigest(path: string, startedMs: number = processStartedMs()): string | null {
  let status;
  try {
    status = statSync(path);
  } catch {
    return null;
  }
  if (!status.isFile() || Math.max(status.mtimeMs, status.ctimeMs) > startedMs - TIMESTAMP_MARGIN_MS) return null;
  const sight: Sight = { mtimeMs: status.mtimeMs, ctimeMs: status.ctimeMs, size: status.size };
  const seen = SEEN.get(path);
  if (seen !== undefined) return sameSight(seen, sight) ? seen.digest : null;
  // A file first seen once the cache is full gets none, so the first-sight check never lapses.
  if (SEEN.size >= FILE_DIGESTS_CACHED) return null;
  let digest: string | null;
  let after;
  try {
    digest = `sha256:${createHash("sha256").update(readFileSync(path)).digest("hex")}`;
    after = statSync(path);
  } catch {
    return null;
  }
  // Changed while it was read: these bytes are nobody's.
  if (!sameSight(after, sight)) digest = null;
  SEEN.set(path, { ...sight, digest });
  return digest;
}

/** The frame's file as a path, or null for a frame that is not the application's. */
function applicationFile(raw: string): string | null {
  if (raw.startsWith("node:")) return null;
  let file = raw.replace(/\\/g, "/");
  if (file.startsWith("file://")) {
    try {
      file = fileURLToPath(file);
    } catch {
      return null;
    }
  }
  if (!file.includes("/") || file.includes("/node_modules/") || file.startsWith(OWN)) return null;
  return file;
}

/** The application frames of one V8 stack string, innermost first, at most `CALL_SITE_MAX_FRAMES`. */
export function parseStack(stack: string, root: string): CallerFrame[] {
  const frames: CallerFrame[] = [];
  for (const text of stack.split("\n")) {
    if (frames.length >= CALL_SITE_MAX_FRAMES) break;
    const named = NAMED.exec(text);
    const bare = named === null ? BARE.exec(text) : null;
    const name = named?.[1] ?? "<anonymous>";
    const raw = named?.[2] ?? bare?.[1];
    const line = named?.[3] ?? bare?.[2];
    if (raw === undefined || line === undefined) continue;
    const file = applicationFile(raw);
    if (file === null) continue;
    const relative = located(file, root);
    // Only a file under the project root is the application's own: nothing outside it is digested.
    const digest = relative.startsWith(`${OUTSIDE_ROOT}/`) ? null : fileDigest(file);
    frames.push({
      module: relative.replace(/\.[^./]+$/, ""),
      qualname: name.replace(/ \[as [^\]]+\]$/, ""),
      file: relative,
      line: Number(line),
      ...(digest === null ? {} : { fileDigest: digest }),
    });
  }
  return frames;
}

/** This call's application frames, read off a fresh stack deep enough to reach past a framework. */
export function callerFrames(root: string): CallerFrame[] {
  const limit = Error.stackTraceLimit;
  Error.stackTraceLimit = CALL_SITE_STACK_SCANNED;
  try {
    return parseStack(new Error().stack ?? "", root);
  } finally {
    Error.stackTraceLimit = limit;
  }
}
