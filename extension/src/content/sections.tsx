import { useState } from "react";
import { difficultySchema, healthSchema, issueMatchSchema, readinessSchema, type Report } from "../shared/schemas";
import { safeGithubUrl, type IssueRef } from "../shared/url";
import type { Bridge } from "./bridge";
import { ageText, humanize, LEVEL_NAMES, mlReasonText, round, statusText, words } from "./format";
import { useRequest } from "./hooks";
import { Badge, Collapsible, ErrorNotice, List, Loading, Notice, ScoreMeter } from "./ui";

interface SectionProps {
  bridge: Bridge;
  issue: IssueRef;
  onSettings: () => void;
}

const key = (r: IssueRef) => `${r.owner}/${r.repo}#${r.number}`;

// ---------------------------------------------------------------------------------------------- match

/** A plain-words reading of the number. It only restates the score; it adds no information the score does not carry. */
function verdict(score: number, insufficient: boolean): string {
  if (insufficient) return "Not enough about you or this issue to say how well it fits.";
  if (score >= 70) return "Looks like a good fit for your skills.";
  if (score >= 45) return "Some overlap with your skills.";
  return "Not a close match with your skills yet.";
}

export function MatchSection({ bridge, issue, onSettings }: SectionProps) {
  const [state, reload] = useRequest(() => bridge({ type: "match", ref: issue }, issueMatchSchema), true, key(issue));
  return (
    <section aria-labelledby="compass-match-h" className="section">
      <h2 id="compass-match-h" className="section-title">Match for you</h2>
      {state.status === "loading" && <Loading label="Scoring this issue…" />}
      {state.status === "error" && <ErrorNotice error={state.error} onRetry={reload} onSettings={onSettings} />}
      {state.status === "success" && (() => {
        const { model, match, ranking_reliability: reliability } = state.data;
        const entries = Object.entries(match.components);
        if (entries.length === 0) return <Notice>No score breakdown is available for this issue.</Notice>;
        return (
          <>
            <p className="issue-title">{match.issue.title}</p>
            <p className="muted">{match.repository.full_name} #{match.issue.number}</p>
            <div className="score-row">
              <span className="big-score" aria-label={`Match score ${round(match.final_score)} out of 100`}>{round(match.final_score)}<small>/100</small></span>
              <span className="badges">
                <Badge kind="heuristic">{model.is_ml_prediction ? "ML prediction" : `Heuristic ${model.version}`}</Badge>
                {match.insufficient_information && <Badge kind="warn">Low confidence</Badge>}
              </span>
            </div>
            <p className="verdict">{verdict(match.final_score, match.insufficient_information)}</p>
            {reliability.status !== "ok" && reliability.reasons.length > 0 && <Notice tone="warn"><List items={reliability.reasons} /></Notice>}
            {match.reasons.length > 0 && <><h3 className="mini">Why it matches</h3><List items={match.reasons.slice(0, 3)} /></>}
            {match.reasons.length === 0 && <p className="muted">No strong match signals were found.</p>}
            {match.missing_skills.length > 0 && <><h3 className="mini">You may want to learn this first</h3><List items={match.missing_skills.slice(0, 5)} /></>}
            <Collapsible title="Score breakdown">
              <p className="muted">
                {model.is_ml_prediction ? model.score_note : "Rule-based score, not an ML prediction and not a probability."} Evidence coverage {round(match.coverage * 100)}%.
              </p>
              <ul className="components" aria-label="Score breakdown">
                {entries.map(([name, c]) => (
                  <li key={name}>
                    <span className="comp-name">{humanize(name)}</span>
                    <ScoreMeter label={humanize(name)} score={c.score} />
                    <span className="comp-weight">{round(c.weight * 100)}% weight{c.status !== "known" ? ` · ${statusText(c.status).toLowerCase()}` : ""}</span>
                  </li>
                ))}
              </ul>
              {match.reasons.length > 3 && <><h4>More reasons</h4><List items={match.reasons.slice(3)} /></>}
              {match.missing_skills.length > 5 && <><h4>More skills you may be missing</h4><List items={match.missing_skills.slice(5)} /></>}
              {match.warnings.length > 0 && <><h4>Worth knowing</h4><List items={match.warnings} className="warnings" /></>}
              <h4>Evidence by component</h4>
              {entries.map(([name, c]) => (
                <div key={name}>
                  <strong>{humanize(name)}</strong>
                  {c.score === null && <p className="muted">Unknown: scored as a conservative placeholder, never as a match.</p>}
                  <List items={c.evidence} />
                </div>
              ))}
            </Collapsible>
          </>
        );
      })()}
    </section>
  );
}

// ---------------------------------------------------------------------------------------------- difficulty

export function DifficultySection({ bridge, issue, onSettings }: SectionProps) {
  const [state, reload] = useRequest(() => bridge({ type: "difficulty", ref: issue }, difficultySchema), true, key(issue));
  return (
    <section aria-labelledby="compass-diff-h" className="section">
      <h2 id="compass-diff-h" className="section-title">Difficulty</h2>
      {state.status === "loading" && <Loading label="Estimating difficulty…" />}
      {state.status === "error" && <ErrorNotice error={state.error} onRetry={reload} onSettings={onSettings} />}
      {state.status === "success" && (() => {
        const hint = state.data.deterministic_signals.difficulty_label_hint;
        const ml = state.data.ml_prediction;
        return (
          <>
            <div className="block">
              <h4>Maintainer label <Badge kind="info">Deterministic</Badge></h4>
              {hint ? (
                <p>Level {hint.level} of 5 ({LEVEL_NAMES[hint.level] ?? "?"}) from the label “{hint.maintainer_label}”. <span className="muted">Noisy human label, not measured.</span></p>
              ) : (
                <p className="muted">This issue has no difficulty-style label.</p>
              )}
            </div>
            <div className="block">
              <h4>ML estimate <Badge kind="experimental">Experimental</Badge></h4>
              {ml.status === "predicted" && ml.model && (
                <>
                  <p>Closest class: <strong>{ml.label}</strong></p>
                  <ul className="components" aria-label="ML class scores">
                    {Object.entries(ml.class_scores).sort((a, b) => b[1] - a[1]).map(([cls, v]) => (
                      <li key={cls}><span className="comp-name">{cls}</span><ScoreMeter label={`${cls} (uncalibrated)`} score={v * 100} /></li>
                    ))}
                  </ul>
                  <p className="muted">
                    Method: logistic regression on issue text ({ml.model.id}); labels: {words(ml.model.label_source)}; status: {words(ml.model.evaluation_status)}.
                    Scores are uncalibrated and are not probabilities.
                  </p>
                </>
              )}
              {ml.status === "unsupported_input" && <p className="muted">No ML estimate: {mlReasonText(ml.reason)}.</p>}
              {ml.status === "unavailable" && <p className="muted">No ML estimate: {mlReasonText(ml.reason)}.</p>}
              <p className="muted experimental-note">
                Experimental: trained on provisional labels and not validated on real data. Treat as a hint, not a measurement.
              </p>
            </div>
            <p className="muted">{state.data.fallback.description}</p>
          </>
        );
      })()}
    </section>
  );
}

// ---------------------------------------------------------------------------------------------- health + readiness

function ReportView({ report, scoringNote }: { report: Report; scoringNote: string }) {
  const signals = Object.entries(report.signals);
  return (
    <>
      <div className="score-row">
        <span className="big-score" aria-label={report.score === null ? "No score" : `Score ${round(report.score)} out of 100`}>
          {report.score === null ? "–" : round(report.score)}<small>/100</small>
        </span>
        <span className="badges">
          <Badge kind="info">{words(report.label)}</Badge>
          <Badge kind="heuristic">Heuristic</Badge>
          {report.coverage < 0.4 && <Badge kind="warn">Limited evidence</Badge>}
        </span>
      </div>
      <p className="muted">{scoringNote} Evidence coverage {round(report.coverage * 100)}%.</p>
      <ul className="components" aria-label={`${report.kind.replace("_", " ")} signals`}>
        {signals.map(([name, s]) => (
          <li key={name}>
            <span className="comp-name">{humanize(name)}</span>
            <ScoreMeter label={humanize(name)} score={s.score} />
            <span className="comp-weight">{s.status === "known" ? `${round(s.confidence * 100)}% confidence` : statusText(s.status)}</span>
          </li>
        ))}
      </ul>
      {report.notes.length > 0 && <List items={report.notes} className="muted-list" />}
      {report.warnings.length > 0 && (
        <Collapsible title={`Warnings (${report.warnings.length})`}>
          <List items={report.warnings} className="warnings" />
        </Collapsible>
      )}
      <Collapsible title="Signal details">
        {signals.map(([name, s]) => (
          <div key={name}>
            <strong>{humanize(name)}</strong>
            <List items={s.evidence} />
          </div>
        ))}
      </Collapsible>
    </>
  );
}

export function HealthSection({ bridge, issue, onSettings }: SectionProps) {
  const [open, setOpen] = useOpen();
  const [state, reload] = useRequest(() => bridge({ type: "health", ref: issue }, healthSchema), open, key(issue));
  return (
    <Collapsible title="Repository health" badge={<Badge kind="heuristic">Heuristic</Badge>} onOpenChange={setOpen}>
      {state.status === "loading" && <Loading label="Analysing repository activity (can take a few seconds the first time)…" />}
      {state.status === "error" && <ErrorNotice error={state.error} onRetry={reload} onSettings={onSettings} />}
      {state.status === "success" && (
        <>
          <ReportView report={state.data.report} scoringNote={state.data.scoring.note} />
          <p className="muted">
            Data collected {ageText(state.data.freshness.age_hours)} (window {state.data.freshness.window_days ?? "?"} days).
            {state.data.freshness.stale && " This analysis is stale."}
            {state.data.freshness.complete === false && ` Incomplete: ${Object.keys(state.data.freshness.errors).join(", ")}.`}
            {state.data.freshness.refresh_error && ` A refresh failed (${state.data.freshness.refresh_error}); older data is shown.`}
          </p>
        </>
      )}
    </Collapsible>
  );
}

export function ReadinessSection({ bridge, issue, onSettings }: SectionProps) {
  const [open, setOpen] = useOpen();
  const [state, reload] = useRequest(() => bridge({ type: "readiness", ref: issue }, readinessSchema), open, key(issue));
  return (
    <Collapsible title="Contribution readiness" badge={<Badge kind="heuristic">Heuristic</Badge>} onOpenChange={setOpen}>
      {state.status === "loading" && <Loading label="Checking contribution docs, tests and linked PRs…" />}
      {state.status === "error" && <ErrorNotice error={state.error} onRetry={reload} onSettings={onSettings} />}
      {state.status === "success" && (
        <>
          <ReportView report={state.data.report} scoringNote={state.data.scoring.note} />
          {state.data.linked_pull_requests === null ? (
            <p className="muted">Linked pull requests have not been checked.</p>
          ) : state.data.linked_pull_requests.pull_requests.length === 0 ? (
            <p className="muted">No pull requests reference this issue (mentions only; may miss manual links).</p>
          ) : (
            <>
              <h4>Pull requests referencing this issue</h4>
              <ul>
                {state.data.linked_pull_requests.pull_requests.map((pr) => {
                  const href = safeGithubUrl(pr.html_url ?? null);
                  return (
                    <li key={pr.number}>
                      {href ? <a href={href} target="_blank" rel="noopener noreferrer">#{pr.number}</a> : `#${pr.number}`} {words(pr.state)}
                    </li>
                  );
                })}
              </ul>
            </>
          )}
          {Object.keys(state.data.upstream_errors).length > 0 && (
            <Notice tone="warn">Some checks failed: {Object.entries(state.data.upstream_errors).map(([k, v]) => `${words(k)} (${v})`).join(", ")}.</Notice>
          )}
        </>
      )}
    </Collapsible>
  );
}

function useOpen() {
  const [open, setOpen] = useState(false);
  // Once opened the data stays loaded; closing the section does not cancel or repeat the request.
  return [open, (o: boolean) => setOpen((prev) => prev || o)] as const;
}
