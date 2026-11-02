import { useState } from "react";
import { statusSchema } from "../shared/messages";
import type { IssueRef } from "../shared/url";
import { FeedbackSection, SaveBar } from "./Actions";
import type { Bridge } from "./bridge";
import { useRequest } from "./hooks";
import { DifficultySection, HealthSection, MatchSection, ReadinessSection } from "./sections";
import { ErrorNotice, Loading, Notice } from "./ui";
import { z } from "zod";

export interface PanelProps {
  bridge: Bridge;
  issue: IssueRef;
  floating?: boolean;
}

export function Panel({ bridge, issue, floating = false }: PanelProps) {
  const [collapsed, setCollapsed] = useState(false);
  const [status, reload] = useRequest(() => bridge({ type: "status" }, statusSchema), true, "status");
  const openSettings = () => void bridge({ type: "openOptions" }, z.null());

  return (
    <aside className={`panel${floating ? " floating" : ""}`} aria-label="OpenSource Compass AI">
      <header className="panel-header">
        <div>
          <svg className="mark" width="16" height="16" viewBox="0 0 32 32" fill="none" aria-hidden="true">
            <circle cx="16" cy="16" r="13" stroke="currentColor" strokeWidth="3" />
            <path d="M21.5 10.5 18 18l-7.5 3.5L14 14l7.5-3.5Z" fill="currentColor" />
          </svg>
          <strong className="brand">Compass</strong>
          <span className="muted"> {issue.owner}/{issue.repo} #{issue.number}</span>
        </div>
        <div className="row-actions">
          <button type="button" onClick={openSettings} aria-label="Open OpenSource Compass AI settings">Settings</button>
          <button type="button" onClick={() => setCollapsed(!collapsed)} aria-expanded={!collapsed} aria-controls="compass-panel-body">
            {collapsed ? "Show" : "Hide"}
          </button>
        </div>
      </header>

      <div id="compass-panel-body" hidden={collapsed}>
        {/* Stays mounted while hidden so collapsing does not discard loaded data or repeat requests. */}
        {(
          <>
            {status.status === "loading" && <Loading label="Checking your sign-in…" />}
            {status.status === "error" && <ErrorNotice error={status.error} onRetry={reload} />}
            {status.status === "success" && !status.data.signedIn && (
              <Notice tone="warn">
                {status.data.sessionEnded ? (
                  <p><strong>Your session ended.</strong> It expired or was revoked. Sign in again with a new pairing code in the extension settings.</p>
                ) : (
                  <p><strong>Sign in to see scores.</strong> Enter a pairing code from your OpenSource Compass backend in the extension settings (no data is requested until then).</p>
                )}
                <div className="row-actions">
                  <button type="button" onClick={openSettings}>Open settings</button>
                  <button type="button" onClick={reload}>I have signed in</button>
                </div>
              </Notice>
            )}
            {status.status === "success" && status.data.signedIn && (
              <>
                {status.data.experimentalScorer && <Notice>Using the experimental scoring method (heuristic_v2). It has not been evaluated.</Notice>}
                <MatchSection bridge={bridge} issue={issue} onSettings={openSettings} />
                <SaveBar bridge={bridge} issue={issue} onSettings={openSettings} />
                <DifficultySection bridge={bridge} issue={issue} onSettings={openSettings} />
                <HealthSection bridge={bridge} issue={issue} onSettings={openSettings} />
                <ReadinessSection bridge={bridge} issue={issue} onSettings={openSettings} />
                <FeedbackSection bridge={bridge} issue={issue} onSettings={openSettings} />
              </>
            )}
            <footer className="panel-footer muted">
              Scores are heuristics from your profile and public GitHub data, not guarantees. Backend:{" "}
              {status.status === "success" ? status.data.backendUrl : "…"}
            </footer>
          </>
        )}
      </div>
    </aside>
  );
}
