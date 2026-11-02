import type { ZodType } from "zod";
import { responseSchema, type AdminRequest, type ErrorInfo, type ErrorKind, type Request } from "../shared/messages";

export type Result<T> = { ok: true; data: T } | { ok: false; error: ErrorInfo };

/** Sends one message to the background worker and resolves with its raw reply. */
export type Sender = (message: Request | AdminRequest) => Promise<unknown>;

export const chromeSender: Sender = (message) =>
  new Promise((resolve, reject) => {
    try {
      chrome.runtime.sendMessage(message, (reply: unknown) => {
        const err = chrome.runtime.lastError;
        if (err) reject(new Error(err.message));
        else resolve(reply);
      });
    } catch (e) {
      reject(e);
    }
  });

const KNOWN_KINDS = new Set<ErrorKind>([
  "not_configured", "auth", "forbidden", "not_found", "invalid_request", "rate_limited", "server", "network",
  "timeout", "malformed", "invalid_config", "extension", "rejected", "unknown",
]);

const fail = (kind: ErrorKind, message: string): Result<never> => ({ ok: false, error: { kind, message } });

/**
 * Typed calls to the background worker. The reply envelope AND the data are validated here as well, so a bug or a
 * malformed reply becomes an error state instead of a broken panel. The content script never sees the access token.
 */
export function createBridge(send: Sender) {
  return async function call<T>(message: Request | AdminRequest, schema: ZodType<T>): Promise<Result<T>> {
    let raw: unknown;
    try {
      raw = await send(message);
    } catch {
      return fail("extension", "Could not reach the extension. Reload this page (or the extension) and try again.");
    }
    const envelope = responseSchema.safeParse(raw);
    if (!envelope.success) return fail("malformed", "The extension returned a reply in an unexpected format.");
    if (!envelope.data.ok) {
      const e = envelope.data.error;
      const kind = (KNOWN_KINDS.has(e.kind as ErrorKind) ? e.kind : "unknown") as ErrorKind;
      return { ok: false, error: { kind, message: e.message, status: e.status, retryAfterSeconds: e.retryAfterSeconds, details: e.details } };
    }
    const data = schema.safeParse(envelope.data.data);
    if (!data.success) return fail("malformed", "The data received was not in the expected format, so nothing is shown rather than guessing.");
    return { ok: true, data: data.data };
  };
}

export type Bridge = ReturnType<typeof createBridge>;
