import { useCallback, useEffect, useRef, useState } from "react";
import { z } from "zod";
import type { Bridge } from "../content/bridge";
import { errorText } from "../content/ui";
import type { ErrorInfo } from "../shared/messages";
import { matchSearchSchema, profileSchema, savedIssueSchema, savedListSchema, type MatchItem, type MatchSearch } from "../shared/schemas";
import { safeGithubUrl } from "../shared/url";
import { Busy, IssueRow, issueKey } from "./IssueParts";

const PAGE = 10;
const REPO = /^[A-Za-z0-9_.-]{1,100}\/[A-Za-z0-9_.-]{1,100}$/;
const COMMON_LANGUAGES = ["Python", "JavaScript", "TypeScript", "Java", "Go", "Rust", "C++", "C#", "Ruby", "PHP", "Kotlin", "Swift"];
const DIFFICULTIES = [
  { value: "", text: "From my profile" },
  { value: "1", text: "1 · Beginner" },
  { value: "2", text: "2 · Easy" },
  { value: "3", text: "3 · Intermediate" },
  { value: "4", text: "4 · Advanced" },
  { value: "5", text: "5 · Expert" },
];

interface Filters { language: string; topic: string; difficulty: string; repository: string }
const NO_FILTERS: Filters = { language: "", topic: "", difficulty: "", repository: "" };

type Load = { status: "loading" } | { status: "error"; error: ErrorInfo } | { status: "ready" };
type Flash = { tone: "ok" | "error"; text: string } | null;

interface Props {
  call: Bridge;
  onProfile: () => void;
  onAccount: () => void;
}

export function Discover({ call, onProfile, onAccount }: Props) {
  const [form, setForm] = useState<Filters>(NO_FILTERS);
  const [applied, setApplied] = useState<Filters>(NO_FILTERS);
  const [formError, setFormError] = useState<string | null>(null);
  const [load, setLoad] = useState<Load>({ status: "loading" });
  const [result, setResult] = useState<MatchSearch | null>(null);
  const [items, setItems] = useState<MatchItem[]>([]);
  const [loadingMore, setLoadingMore] = useState(false);
  const [saved, setSaved] = useState<Map<string, number>>(new Map());
  const [busy, setBusy] = useState<string | null>(null);
  const [flash, setFlash] = useState<Flash>(null);
  const [languages, setLanguages] = useState<string[]>(COMMON_LANGUAGES);
  const latest = useRef(0); // a reply to an older search must not overwrite a newer one

  const search = useCallback(async (f: Filters, offset: number) => {
    const mine = ++latest.current;
    if (offset === 0) setLoad({ status: "loading" });
    else setLoadingMore(true);
    const r = await call({
      type: "matches.search", limit: PAGE, offset,
      ...(f.language.trim() ? { language: f.language.trim() } : {}),
      ...(f.topic.trim() ? { topic: f.topic.trim() } : {}),
      ...(f.difficulty ? { preferredDifficulty: Number(f.difficulty) } : {}),
      ...(f.repository.trim() ? { repository: f.repository.trim() } : {}),
    }, matchSearchSchema);
    if (mine !== latest.current) return;
    setLoadingMore(false);
    if (!r.ok) return setLoad({ status: "error", error: r.error });
    setResult(r.data);
    setItems((cur) => (offset === 0 ? r.data.items : [...cur, ...r.data.items]));
    setLoad({ status: "ready" });
  }, [call]);

  useEffect(() => {
    void search(NO_FILTERS, 0);
    // Which of these are already saved, and which languages to suggest. Both are conveniences: a failure here changes nothing else.
    void call({ type: "saved.list", limit: 100, offset: 0 }, savedListSchema).then((r) => {
      if (r.ok) setSaved(new Map(r.data.items.map((s) => [issueKey(s.owner, s.repo, s.number), s.id])));
    });
    void call({ type: "profile.get" }, profileSchema.nullable()).then((r) => {
      if (!r.ok || !r.data) return;
      const mine = r.data.skills.filter((s) => s.category === "language").map((s) => s.name);
      if (mine.length) setLanguages([...new Set([...mine, ...COMMON_LANGUAGES.map((l) => l.toLowerCase())])]);
    });
  }, [call, search]);

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setFlash(null);
    if (form.repository.trim() && !REPO.test(form.repository.trim())) {
      return setFormError("A repository looks like owner/name, for example psf/requests.");
    }
    setFormError(null);
    setApplied(form);
    void search(form, 0);
  }

  function reset() {
    setForm(NO_FILTERS);
    setApplied(NO_FILTERS);
    setFormError(null);
    setFlash(null);
    void search(NO_FILTERS, 0);
  }

  async function save(m: MatchItem) {
    const key = issueKey(m.repository.owner, m.repository.name, m.issue.number);
    setBusy(key);
    setFlash(null);
    const r = await call({ type: "issue.save", ref: { owner: m.repository.owner, repo: m.repository.name, number: m.issue.number } }, savedIssueSchema);
    setBusy(null);
    if (r.ok) {
      setSaved((cur) => new Map(cur).set(key, r.data.id));
      setFlash({ tone: "ok", text: `Saved “${m.issue.title}”. You will find it under Saved issues.` });
    } else setFlash({ tone: "error", text: `Could not save “${m.issue.title}”. ${errorText(r.error)}` });
  }

  async function unsave(m: MatchItem) {
    const key = issueKey(m.repository.owner, m.repository.name, m.issue.number);
    const id = saved.get(key);
    if (id === undefined) return;
    setBusy(key);
    setFlash(null);
    const r = await call({ type: "saved.remove", savedId: id }, z.null());
    setBusy(null);
    if (r.ok) {
      setSaved((cur) => {
        const next = new Map(cur);
        next.delete(key);
        return next;
      });
      setFlash({ tone: "ok", text: `Removed “${m.issue.title}” from your saved issues.` });
    } else setFlash({ tone: "error", text: `Could not remove “${m.issue.title}”. ${errorText(r.error)}` });
  }

  const filtered = JSON.stringify(applied) !== JSON.stringify(NO_FILTERS);
  const needsAccount = load.status === "error" && (load.error.kind === "auth" || load.error.kind === "not_configured");

  return (
    <section aria-labelledby="discover-h">
      <h2 id="discover-h">Find issues</h2>
      <p className="lede">
        Let's find your next contribution. These are issues your backend already knows about, ranked against your profile.
        Open any GitHub issue and it joins the pool.
      </p>

      <form className="filters" onSubmit={onSubmit} aria-label="Filter recommendations">
        <div>
          <label htmlFor="f-language">Language</label>
          <input id="f-language" type="text" list="f-languages" value={form.language} placeholder="Any" autoComplete="off" spellCheck={false}
            onChange={(e) => setForm({ ...form, language: e.target.value })} />
          <datalist id="f-languages">{languages.map((l) => <option key={l} value={l} />)}</datalist>
        </div>
        <div>
          <label htmlFor="f-topic">Topic</label>
          <input id="f-topic" type="text" value={form.topic} placeholder="Any, e.g. cli" autoComplete="off" spellCheck={false}
            onChange={(e) => setForm({ ...form, topic: e.target.value })} />
        </div>
        <div>
          <label htmlFor="f-difficulty">Aim for</label>
          <select id="f-difficulty" value={form.difficulty} onChange={(e) => setForm({ ...form, difficulty: e.target.value })}>
            {DIFFICULTIES.map((d) => <option key={d.value} value={d.value}>{d.text}</option>)}
          </select>
        </div>
        <details className="more-filters" open={Boolean(form.repository)} style={{ gridColumn: "1 / -1" }}>
          <summary>Look in a specific repository</summary>
          <label htmlFor="f-repo">Repository (owner/name)</label>
          <input id="f-repo" type="text" value={form.repository} placeholder="psf/requests" autoComplete="off" spellCheck={false}
            aria-describedby="f-repo-help" onChange={(e) => setForm({ ...form, repository: e.target.value })} />
          <p id="f-repo-help" className="help">
            The backend fetches this repository from GitHub first, which counts against its GitHub request limit. Leave it empty to
            search only what the backend already has.
          </p>
        </details>
        <div className="buttons">
          <button type="submit" className="primary" disabled={load.status === "loading"}>{load.status === "loading" ? "Searching…" : "Show recommendations"}</button>
          {(filtered || JSON.stringify(form) !== JSON.stringify(NO_FILTERS)) && <button type="button" onClick={reset} disabled={load.status === "loading"}>Clear filters</button>}
        </div>
      </form>
      {formError && <p className="message error" role="alert">{formError}</p>}

      <div aria-live="polite">
        {flash && <p className={`message ${flash.tone}`} role={flash.tone === "error" ? "alert" : "status"}>{flash.text}</p>}
      </div>

      {load.status === "loading" && <Busy>Looking for issues that fit you…</Busy>}

      {load.status === "error" && (
        <div className="message error" role="alert">
          <p>{errorText(load.error)}</p>
          <div className="buttons">
            {needsAccount
              ? <button type="button" className="primary" onClick={onAccount}>Go to Account</button>
              : <button type="button" onClick={() => void search(applied, 0)}>Try again</button>}
          </div>
        </div>
      )}

      {load.status === "ready" && result && (
        <>
          {result.ranking_reliability.status !== "ok" && result.ranking_reliability.reasons.length > 0 && items.length > 0 && (
            <div className="message info" role="note">
              {result.ranking_reliability.reasons.map((t) => <p key={t}>{t}</p>)}
              <div className="buttons"><button type="button" onClick={onProfile}>Add to your profile</button></div>
            </div>
          )}
          {result.upstream_errors.length > 0 && (
            <div className="message error" role="alert">
              {result.upstream_errors.map((e) => <p key={`${e.repository}${e.code}`}>Could not fetch {e.repository} from GitHub: {e.message}</p>)}
            </div>
          )}

          {items.length === 0 ? (
            <div className="empty">
              <h3>{filtered ? "No issues match those filters" : "Nothing to recommend yet"}</h3>
              {filtered ? (
                <p>Try a different language or topic, or clear the filters.</p>
              ) : (
                <>
                  <p>Your backend has no open issues to rank yet. Open an issue on GitHub (the panel will add it), or look in a repository you like above.</p>
                  {result.ranking_reliability.reasons.map((t) => <p key={t}>{t}</p>)}
                </>
              )}
              <div className="buttons">
                {filtered ? <button type="button" onClick={reset}>Clear filters</button> : <button type="button" onClick={onProfile}>Fill in your profile</button>}
              </div>
            </div>
          ) : (
            <>
              <ol className="issues" aria-label="Recommended issues">
                {items.map((m) => {
                  const key = issueKey(m.repository.owner, m.repository.name, m.issue.number);
                  return (
                    <IssueRow
                      key={key} call={call}
                      owner={m.repository.owner} repo={m.repository.name} number={m.issue.number} title={m.issue.title}
                      href={safeGithubUrl(m.issue.html_url)} score={m.final_score} lowConfidence={m.insufficient_information}
                      reason={m.insufficient_information ? "Not enough profile information to say how well this fits." : (m.reasons[0] ?? "No strong signals either way.")}
                      saved={saved.has(key)} busy={busy === key} onSave={() => void save(m)} onUnsave={() => void unsave(m)}
                      details={() => (
                        <>
                          {m.reasons.length > 1 && <><h4>More about the match</h4><ul>{m.reasons.slice(1).map((t) => <li key={t}>{t}</li>)}</ul></>}
                          {m.missing_skills.length > 0 && <><h4>You may want to learn this first</h4><ul>{m.missing_skills.map((t) => <li key={t}>{t}</li>)}</ul></>}
                          {m.warnings.length > 0 && <><h4>Worth knowing</h4><ul>{m.warnings.map((t) => <li key={t}>{t}</li>)}</ul></>}
                        </>
                      )}
                    />
                  );
                })}
              </ol>
              <div className="pager">
                <p className="help">
                  Showing {items.length} of {result.total}. Scores are rule-based ({result.model.name} {result.model.version}), not predictions and not guarantees.
                  {result.candidates_truncated && " More candidates exist than were considered."}
                </p>
                {items.length < result.total && (
                  <button type="button" disabled={loadingMore} onClick={() => void search(applied, items.length)}>{loadingMore ? "Loading…" : "Show more"}</button>
                )}
              </div>
            </>
          )}
        </>
      )}
    </section>
  );
}
