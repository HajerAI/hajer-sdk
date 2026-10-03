/**
 * The bounded in-memory queue `observe` puts a submission on, and the receipt it hands back.
 *
 * **It is bounded and it refuses the newest.** A queue that grew without limit would be this SDK
 * turning a service outage into somebody else's out-of-memory. Past `HAJER_OBSERVE_QUEUE_MAX` the
 * newest submission is refused with a receipt that says `refused`, and the count of refusals is on
 * the queue so a caller can see it happening rather than infer it from a gap.
 *
 * **It never rejects.** `observe` returns a receipt synchronously; the send happens on a flush the
 * caller triggers or the client's `close()` performs. A transport failure marks the receipt `failed`
 * and the queue keeps going — the customer's request path has already returned by then and there is
 * nobody to raise at.
 */

import { batchBody } from "./payload.ts";
import type { JsonObject } from "./json.ts";
import type { HajerSettings } from "./settings.ts";

export type ObserveState = "queued" | "sent" | "failed" | "refused" | "disabled";

/** What `observe` hands back: which submission, and what has happened to it so far. */
export interface ObserveReceipt {
  readonly idempotencyKey: string;
  readonly state: ObserveState;
  readonly detail?: string;
}

type Send = (body: JsonObject) => Promise<void>;

interface Queued {
  readonly body: JsonObject;
  readonly receipt: { idempotencyKey: string; state: ObserveState; detail?: string };
}

export class ObserveQueue {
  readonly #pending: Queued[] = [];
  #refused = 0;
  readonly #settings: HajerSettings;
  readonly #send: Send | null;

  constructor(settings: HajerSettings, send: Send | null) {
    this.#settings = settings;
    this.#send = send;
  }

  /** Observations waiting in memory. */
  get depth(): number {
    return this.#pending.length;
  }

  /** How many submissions this queue has refused because it was full. */
  get refusedCount(): number {
    return this.#refused;
  }

  enqueue(body: JsonObject, idempotencyKey: string): ObserveReceipt {
    if (this.#send === null) {
      return { idempotencyKey, state: "disabled" };
    }
    if (this.#pending.length >= this.#settings.observeQueueMax) {
      this.#refused += 1;
      return {
        idempotencyKey,
        state: "refused",
        detail: `the observe queue holds its bound of ${this.#settings.observeQueueMax} submissions; this one was refused rather than growing it`,
      };
    }
    const receipt: Queued["receipt"] = { idempotencyKey, state: "queued" };
    this.#pending.push({ body, receipt });
    return receipt;
  }

  /** Send everything queued now, in batches of `observeBatchMax`. Never rejects. */
  async flush(): Promise<void> {
    if (this.#send === null) return;
    while (this.#pending.length > 0) {
      const batch = this.#pending.splice(0, this.#settings.observeBatchMax);
      try {
        await this.#send(batchBody(batch.map((item) => item.body)));
        for (const item of batch) item.receipt.state = "sent";
      } catch (error) {
        for (const item of batch) {
          item.receipt.state = "failed";
          item.receipt.detail = error instanceof Error ? error.message.slice(0, 512) : String(error);
        }
      }
    }
  }
}
