import { useCallback, useEffect, useState } from "react";
import { z } from "zod";
import type { Bridge } from "../content/bridge";
import { errorText } from "../content/ui";
import type { ErrorInfo } from "../shared/messages";
import { savedListSchema, storedMatchListSchema, type SavedList as SavedListData } from "../shared/schemas";
import { safeGithubUrl } from "../shared/url";
import { Busy, IssueRow, issueKey } from "./IssueParts";

const PAGE = 20;

type Load = { status: "loading" } | { status: "error"; error: ErrorInfo } | { status: "ready"; data: SavedListData };
type Score = { score: number; low: boolean };

export function SavedList({ call, onDiscover }: { call: Bridge; onDiscover?: () => void }) {
  const [offset, setOffset] = useState(0);
  const [load, setLoad] = useState<Load>({ status: "loading" });
  const [removing, setRemoving] = useState<number | null>(null);
  const [flash, setFlash] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  const [scores, setScores] = useState<Map<string, Score>>(new Map());

  const fetchPage = useCallback(async (at: number) => {
    setLoad({ status: "loading" });
    const r = await call({ type: "saved.list", limit: PAGE, offset: at }, savedListSchema);
    if (!r.ok) return setLoad({ status: "error", error: r.error });
    // Removing the last item of a later page leaves it empty: step back instead of showing nothing.
    if (r.data.items.length === 0 && at > 0) return setOffset(Math.max(0, at - PAGE));
    setLoad({ status: "ready", data: r.data });
  }, [call]);

  useEffect(() => {
    void fetchPage(offset);
  }, [fetchPage, offset]);

  // A score is shown only where the backend already stored one for that issue (newest first, so the first per issue wins). No guessing.
  useEffect(() => {
    void call({ type: "matches.stored" }, storedMatchListSchema).then((r) => {
      if (!r.ok) return;
      const map = new Map<string, Score>();
      for (const m of r.data.items) {
        const key = issueKey(m.repository.owner, m.repository.name, m.issue.number);
        if (!map.has(key)) map.set(key, { score: m.final_score, low: m.insufficient_information });
      }
      setScores(map);
    });
  }, [call]);

  async function remove(id: number, title: string) {
    setRemoving(id);
    setFlash(null);
    const r = await call({ type: "saved.remove", savedId: id }, z.null());
    setRemoving(null);
    if (r.ok) {
      setFlash({ tone: "ok", text: `Removed “${title}” from your saved issues.` });
      void fetchPage(offset);
    } else setFlash({ tone: "error", text: `Could not remove “${title}”. ${errorText(r.error)}` });
  }

  return (
    <section aria-labelledby="saved-h">
      <h2 id="saved-h">Saved issues</h2>
      <div aria-live="polite">
        {flash && <p className={`message ${flash.tone}`} role={flash.tone === "error" ? "alert" : "status"}>{flash.text}</p>}
      </div>
      {load.status === "loading" && <Busy>Loading saved issues…</Busy>}
      {load.status === "error" && (
        <div className="message error" role="alert">
          <p>{errorText(load.error)}</p>
          <div className="buttons"><button type="button" onClick={() => void fetchPage(offset)}>Try again</button></div>
        </div>
      )}
      {load.status === "ready" && load.data.total === 0 && (
        <div className="empty">
          <h3>Nothing saved yet</h3>
          <p>Find an issue worth exploring. Use “Save issue” in the panel on a GitHub issue page, or “Save” in Find issues, and it will show up here.</p>
          {onDiscover && <div className="buttons"><button type="button" className="primary" onClick={onDiscover}>Find issues</button></div>}
        </div>
      )}
      {load.status === "ready" && load.data.items.length > 0 && (
        <>
          <ol className="issues" aria-label="Saved issues">
            {load.data.items.map((s) => {
              const score = scores.get(issueKey(s.owner, s.repo, s.number));
              return (
                <IssueRow
                  key={s.id} call={call} owner={s.owner} repo={s.repo} number={s.number} title={s.title}
                  href={safeGithubUrl(s.html_url)} score={score?.score ?? null} lowConfidence={score?.low}
                  reason={<>{s.state === "open" ? "Was open" : `Was ${s.state}`} when last fetched · saved {new Date(s.saved_at).toLocaleDateString(undefined, { dateStyle: "medium" })}</>}
                  saved busy={removing === s.id} onUnsave={() => void remove(s.id, s.title)}
                />
              );
            })}
          </ol>
          <div className="pager">
            <p className="help">
              Showing {load.data.offset + 1}–{load.data.offset + load.data.items.length} of {load.data.total}. Titles and states are as
              last fetched and may have changed on GitHub. A score appears only where one was computed for you before.
            </p>
            <div className="buttons" style={{ marginTop: 0 }}>
              <button type="button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>Previous</button>
              <button type="button" disabled={offset + PAGE >= load.data.total} onClick={() => setOffset(offset + PAGE)}>Next</button>
            </div>
          </div>
        </>
      )}
    </section>
  );
}
