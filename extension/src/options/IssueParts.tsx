import { useState, type ReactNode } from "react";
import type { Bridge } from "../content/bridge";
import { LEVEL_NAMES, round } from "../content/format";
import { useRequest } from "../content/hooks";
import { Badge, errorText } from "../content/ui";
import { difficultySchema } from "../shared/schemas";
import type { IssueRef } from "../shared/url";

export const issueKey = (owner: string, repo: string, number: number) => `${owner}/${repo}#${number}`.toLowerCase();

/** Spinner + words. Same markup the panel uses, styled by options.css here. */
export function Busy({ children }: { children: ReactNode }) {
  return (
    <p className="loading" role="status">
      <span className="spinner" aria-hidden="true" /> {children}
    </p>
  );
}

/** Difficulty for one issue, loaded only when its details are opened. The maintainer label is the only non-experimental signal. */
export function DifficultyLine({ call, issue }: { call: Bridge; issue: IssueRef }) {
  const [state, reload] = useRequest(() => call({ type: "issue.difficulty", ref: issue }, difficultySchema), true, issueKey(issue.owner, issue.repo, issue.number));
  if (state.status === "loading") return <p className="help" role="status">Checking difficulty…</p>;
  if (state.status === "error") {
    return (
      <p className="help">
        Could not check the difficulty: {errorText(state.error)}{" "}
        <button type="button" className="quiet" onClick={reload}>Try again</button>
      </p>
    );
  }
  if (state.status !== "success") return null;
  const hint = state.data.deterministic_signals.difficulty_label_hint;
  const ml = state.data.ml_prediction;
  return (
    <>
      <p>
        {hint
          ? <>Difficulty: <strong>{LEVEL_NAMES[hint.level] ?? `level ${hint.level}`}</strong> (level {hint.level} of 5), going by the label “{hint.maintainer_label}”.</>
          : <>Difficulty: the maintainers have not put a difficulty label on this issue, so there is nothing to show yet.</>}
      </p>
      {ml.status === "predicted" && ml.label && (
        <p className="help"><Badge kind="experimental">Experimental</Badge> A text-based guess says “{ml.label}”. It has not been validated on real data.</p>
      )}
    </>
  );
}

export interface IssueRowProps {
  owner: string;
  repo: string;
  number: number;
  title: string;
  href: string | null;
  /** 0-100, or null when no score has been computed for this issue. */
  score: number | null;
  lowConfidence?: boolean;
  reason: ReactNode;
  /** Extra detail, shown only when the user opens "Details". */
  details?: (issue: IssueRef) => ReactNode;
  call: Bridge;
  saved: boolean;
  busy: boolean;
  onSave?: () => void;
  onUnsave: () => void;
}

export function IssueRow(p: IssueRowProps) {
  const [open, setOpen] = useState(false);
  const issue: IssueRef = { owner: p.owner, repo: p.repo, number: p.number };
  return (
    <li className="issue">
      <div className="issue-main">
        <p className="repo">{p.owner}/{p.repo} #{p.number}</p>
        <h3 className="issue-title">{p.title}</h3>
        <p className="reason">{p.reason}</p>
        <div className="badges">
          {p.lowConfidence && <Badge kind="warn">Low confidence</Badge>}
          {p.saved && <Badge kind="ok">Saved</Badge>}
        </div>
        <div className="details">
          <button type="button" className="quiet" aria-expanded={open} onClick={() => setOpen(!open)}>
            {open ? "Hide details" : "Details"}<span className="visually-hidden"> for {p.title}</span>
          </button>
          {open && (
            <div role="group" aria-label={`Details for ${p.title}`}>
              <DifficultyLine call={p.call} issue={issue} />
              {p.details?.(issue)}
            </div>
          )}
        </div>
      </div>
      <div className="issue-side">
        {p.score === null
          ? <span className="score none">No score yet</span>
          : <span className="score" aria-label={`Match score ${round(p.score)} out of 100`}>{round(p.score)}<small>/100</small></span>}
        {p.saved ? (
          <button type="button" disabled={p.busy} onClick={p.onUnsave} aria-label={`Unsave ${p.title}`}>{p.busy ? "Removing…" : "Unsave"}</button>
        ) : (
          p.onSave && <button type="button" className="primary" disabled={p.busy} onClick={p.onSave} aria-label={`Save ${p.title}`}>{p.busy ? "Saving…" : "Save"}</button>
        )}
        {p.href && <a className="button" href={p.href} target="_blank" rel="noopener noreferrer" aria-label={`Open on GitHub: ${p.title}`}>Open on GitHub</a>}
      </div>
    </li>
  );
}
