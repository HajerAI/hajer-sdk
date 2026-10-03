/**
 * The JSON value types, and the one canonical serialization both sides digest.
 *
 * `canonical` is the server's canonical JSON and the Python SDK's `_canonical`: keys
 * sorted, no spaces, no non-finite numbers. It is what a case key is taken over, so a second
 * spelling of it would be a second case key for one request.
 */

export type JsonPrimitive = string | number | boolean | null;
export type JsonValue = JsonPrimitive | JsonValue[] | { [key: string]: JsonValue };
export type JsonObject = { [key: string]: JsonValue };

/** Canonical JSON: keys in sorted order, no insignificant whitespace, no NaN and no Infinity. */
export function canonical(value: JsonValue): string {
  if (value === null || typeof value === "boolean" || typeof value === "string") {
    return JSON.stringify(value);
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      throw new TypeError("canonical JSON has no NaN and no Infinity");
    }
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map(canonical).join(",")}]`;
  }
  const keys = Object.keys(value).sort();
  return `{${keys.map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`).join(",")}}`;
}

/** The bytes one body costs on the wire, measured rather than estimated. */
export function byteLength(text: string): number {
  return new TextEncoder().encode(text).length;
}
