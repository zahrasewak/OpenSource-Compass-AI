import { useState } from "react";
import { z } from "zod";
import { feedbackSchema, savedIssueSchema, savedLookupSchema, type FeedbackType } from "../shared/schemas";
import type { ErrorInfo } from "../shared/messages";
import type { IssueRef } from "../shared/url";
import type { Bridge } from "./bridge";
import { useRequest } from "./hooks";
import { Badge, Collapsible, errorText, Notice } from "./ui";

interface Props {
  bridge: Bridge;
  issue: IssueRef;
  onSettings: () => void;
}

type Saved = { id: number; already: boolean };

const QUESTIONS: { type: FeedbackType; label: string; options: { value: string; text: string }[] }[] = [
  { type: "relevance", label: "Is this a relevant recommendation?", options: [{ value: "relevant", text: "Relevant" }, { value: "not_relevant", text: "Not relevant" }] },
  {
    type: "difficulty",
    label: "How does the difficulty feel?",
    options: [{ value: "too_easy", text: "Too easy" }, { value: "appropriate", text: "Appropriate" }, { value: "too_difficult", text: "Too difficult" }],
  },
];

const needsSettings = (e: ErrorInfo | null) => e && (e.kind === "auth" || e.kind === "not_configured");

/** Save / unsave this issue. Shown right under the score because it is the one thing most people do here. */
export function SaveBar({ bridge, issue, onSettings }: Props) {
  // Saved state as the backend knows it when the page opens; `acted` takes over as soon as the user saves or removes.
  const [lookup] = useRequest(() => bridge({ type: "lookupSaved", ref: issue }, savedLookupSchema), true, `${issue.owner}/${issue.repo}#${issue.number}`);
  const [acted, setActed] = useState<{ value: Saved | null } | null>(null);
  const fromLookup: Saved | null = lookup.status === "success" && lookup.data.saved && lookup.data.saved_issue ? { id: lookup.data.saved_issue.id, already: true } : null;
  const saved: Saved | null = acted ? acted.value : fromLookup;
  const setSaved = (value: Saved | null) => setActed({ value });
  const [saveBusy, setSaveBusy] = useState(false);
  const [saveError, setSaveError] = useState<ErrorInfo | null>(null);
  const [removedNote, setRemovedNote] = useState(false);

  async function save() {
    setSaveBusy(true);
    setSaveError(null);
    const r = await bridge({ type: "save", ref: issue }, savedIssueSchema);
    setSaveBusy(false);
    if (r.ok) {
      setSaved({ id: r.data.id, already: r.data.created === false });
      setRemovedNote(false);
    } else setSaveError(r.error);
  }

  async function unsave() {
    if (!saved) return;
    setSaveBusy(true);
    setSaveError(null);
    const r = await bridge({ type: "unsave", savedId: saved.id }, z.null());
    setSaveBusy(false);
    if (r.ok) {
      setSaved(null);
      setRemovedNote(true);
    } else setSaveError(r.error);
  }

  return (
    <section aria-labelledby="compass-save-h" className="section save-bar">
      <h2 id="compass-save-h" className="visually-hidden">Save this issue</h2>
      <div className="row-actions">
        {saved ? (
          <>
            <Badge kind="ok">Saved</Badge>
            <span className="muted" role="status">{saved.already ? "Already in your saved issues." : "Saved to your issues."}</span>
            <button type="button" onClick={unsave} disabled={saveBusy}>{saveBusy ? "Removing…" : "Unsave"}</button>
          </>
        ) : (
          <button type="button" className="primary" onClick={save} disabled={saveBusy || (lookup.status === "loading" && !acted)}>{saveBusy ? "Saving…" : "Save issue"}</button>
        )}
        {lookup.status === "loading" && !acted && <span className="muted" role="status">Checking your saved issues…</span>}
      </div>
      {removedNote && !saved && <p className="muted" role="status">Removed from your saved issues.</p>}
      {lookup.status === "error" && !acted && (
        <p className="muted" role="note">Could not check whether this issue is already saved: {errorText(lookup.error)}</p>
      )}
      {saveError && (
        <Notice tone="error">
          <p>{errorText(saveError)}</p>
          {needsSettings(saveError) && <button type="button" onClick={onSettings}>Open settings</button>}
        </Notice>
      )}
    </section>
  );
}

/** Two quick questions. Tucked away until opened; answers are stored with the account, not used to retrain anything. */
export function FeedbackSection({ bridge, issue, onSettings }: Props) {
  const [answers, setAnswers] = useState<Partial<Record<FeedbackType, { value: string; duplicate: boolean }>>>({});
  const [busy, setBusy] = useState<FeedbackType | null>(null);
  const [feedbackError, setFeedbackError] = useState<ErrorInfo | null>(null);

  async function answer(type: FeedbackType, value: string) {
    setBusy(type);
    setFeedbackError(null);
    const r = await bridge({ type: "feedback", ref: issue, feedbackType: type, value }, feedbackSchema);
    setBusy(null);
    if (r.ok) setAnswers((a) => ({ ...a, [type]: { value, duplicate: r.data.duplicate } }));
    else setFeedbackError(r.error);
  }

  return (
    <Collapsible title="Was this useful?">
      <div className="feedback">
        {QUESTIONS.map((q) => {
          const done = answers[q.type];
          return (
            <fieldset key={q.type} disabled={busy === q.type}>
              <legend>{q.label}</legend>
              <div className="row-actions">
                {q.options.map((o) => (
                  <button key={o.value} type="button" aria-pressed={done?.value === o.value} onClick={() => answer(q.type, o.value)}>
                    {o.text}
                  </button>
                ))}
              </div>
              {done && <p role="status" className="muted">{done.duplicate ? "You had already given this answer." : "Thanks, your feedback was recorded."}</p>}
            </fieldset>
          );
        })}
        <p className="muted">Feedback is stored with your account to evaluate scoring methods later. It is not used to retrain anything automatically.</p>
      </div>
      {feedbackError && (
        <Notice tone="error">
          <p>{errorText(feedbackError)}</p>
          {needsSettings(feedbackError) && <button type="button" onClick={onSettings}>Open settings</button>}
        </Notice>
      )}
    </Collapsible>
  );
}
