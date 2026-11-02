import { useCallback, useEffect, useRef, useState } from "react";
import type { ErrorInfo } from "../shared/messages";
import type { Result } from "./bridge";

export type RequestState<T> =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "success"; data: T }
  | { status: "error"; error: ErrorInfo };

/**
 * Runs `run` when `enabled` (and again when `resetKey` changes or `reload()` is called).
 * A stale reply from an earlier run is discarded.
 */
export function useRequest<T>(run: () => Promise<Result<T>>, enabled: boolean, resetKey: string) {
  const [state, setState] = useState<RequestState<T>>(enabled ? { status: "loading" } : { status: "idle" });
  const [nonce, setNonce] = useState(0);
  const runRef = useRef(run);
  runRef.current = run;

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    setState({ status: "loading" });
    runRef.current().then((result) => {
      if (!cancelled) setState(result.ok ? { status: "success", data: result.data } : { status: "error", error: result.error });
    });
    return () => {
      cancelled = true;
    };
  }, [enabled, resetKey, nonce]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  return [state, reload] as const;
}
