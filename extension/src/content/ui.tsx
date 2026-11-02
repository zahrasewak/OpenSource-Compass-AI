import { useEffect, useId, useState, type ReactNode } from "react";
import type { ErrorInfo } from "../shared/messages";
import { round } from "./format";

/** Status is always conveyed with words, never by color alone. */
export function Badge({ kind, children }: { kind: "heuristic" | "experimental" | "warn" | "info" | "ok"; children: ReactNode }) {
  return <span className={`badge badge-${kind}`}>{children}</span>;
}

export function ScoreMeter({ label, score }: { label: string; score: number | null }) {
  if (score === null) {
    return <span className="unknown" aria-label={`${label}: unknown`}>Unknown</span>;
  }
  const value = Math.max(0, Math.min(100, round(score)));
  return (
    <span className="meter-wrap">
      <span className="meter" role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={value}>
        <span className="meter-fill" style={{ width: `${value}%` }} />
      </span>
      <span className="meter-num">{value}</span>
    </span>
  );
}

export function Loading({ label }: { label: string }) {
  return (
    <p className="loading" role="status" aria-live="polite">
      <span className="spinner" aria-hidden="true" /> {label}
    </p>
  );
}

export function Notice({ tone = "info", children }: { tone?: "info" | "warn" | "error"; children: ReactNode }) {
  return (
    <div className={`notice notice-${tone}`} role={tone === "error" ? "alert" : "note"}>
      {children}
    </div>
  );
}

export function formatWait(seconds: number): string {
  const s = Math.max(1, Math.ceil(seconds));
  if (s <= 120) return `${s} s`;
  const m = Math.ceil(s / 60);
  return m <= 90 ? `${m} min` : `${Math.ceil(m / 60)} h`;
}

/** Seconds left until `seconds` from mount have passed; re-renders once a second. Used to hold "Try again" during a rate limit. */
function useCountdown(seconds: number | undefined): number {
  const [left, setLeft] = useState(seconds ? Math.ceil(seconds) : 0);
  useEffect(() => {
    if (!seconds) return;
    const end = Date.now() + seconds * 1000;
    setLeft(Math.ceil(seconds));
    const id = setInterval(() => {
      const remaining = Math.ceil((end - Date.now()) / 1000);
      setLeft(Math.max(0, remaining));
      if (remaining <= 0) clearInterval(id);
    }, 1000);
    return () => clearInterval(id);
  }, [seconds]);
  return left;
}

export function errorText(error: ErrorInfo): string {
  switch (error.kind) {
    case "auth": return "Your session has ended or is invalid. Sign in again with a new pairing code in the extension settings.";
    case "not_configured": return "You are not signed in. Open the extension settings and enter a pairing code.";
    case "rate_limited":
      return `Rate limited by the backend or GitHub.${error.retryAfterSeconds ? ` Try again in about ${formatWait(error.retryAfterSeconds)}.` : " Try again later."}`;
    case "malformed": return `${error.message} Nothing is shown rather than guessing.`;
    case "network": return `${error.message}`;
    case "invalid_config": return `${error.message} Check the extension settings.`;
    default: return error.message;
  }
}

export function ErrorNotice({ error, onRetry, onSettings }: { error: ErrorInfo; onRetry?: () => void; onSettings?: () => void }) {
  const waitLeft = useCountdown(error.kind === "rate_limited" ? error.retryAfterSeconds : undefined);
  const needsSettings = error.kind === "auth" || error.kind === "not_configured" || error.kind === "invalid_config";
  return (
    <Notice tone="error">
      <p>{errorText(error)}</p>
      <div className="row-actions">
        {needsSettings && onSettings && <button type="button" onClick={onSettings}>Open settings</button>}
        {!needsSettings && onRetry && (
          <button type="button" onClick={onRetry} disabled={waitLeft > 0}>
            {waitLeft > 0 ? `Try again in ${formatWait(waitLeft)}` : "Try again"}
          </button>
        )}
      </div>
    </Notice>
  );
}

/** Accordion. A real <button> with aria-expanded, so Enter and Space work and screen readers announce the state. */
export function Collapsible({
  title,
  badge,
  defaultOpen = false,
  onOpenChange,
  children,
}: {
  title: string;
  badge?: ReactNode;
  defaultOpen?: boolean;
  onOpenChange?: (open: boolean) => void;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const id = useId();
  const toggle = () => {
    setOpen(!open);
    onOpenChange?.(!open);
  };
  return (
    <section className="collapsible">
      <h3 className="collapsible-title">
        <button type="button" className="collapsible-button" aria-expanded={open} aria-controls={`${id}-body`} onClick={toggle}>
          <span aria-hidden="true" className="chevron">{open ? "▾" : "▸"}</span> {title}
        </button>
        {badge}
      </h3>
      <div id={`${id}-body`} role="region" aria-label={title} hidden={!open}>
        {open && children}
      </div>
    </section>
  );
}

export function List({ items, className }: { items: string[]; className?: string }) {
  if (items.length === 0) return null;
  return (
    <ul className={className}>
      {items.map((t, i) => (
        <li key={i}>{t}</li>
      ))}
    </ul>
  );
}
