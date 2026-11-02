import { useEffect, useId, useState } from "react";
import type { Bridge } from "../content/bridge";
import { errorText } from "../content/ui";
import {
  EXPERIENCE_LEVELS, SKILL_CATEGORIES, TASK_TYPES, profileInputSchema, profileSchema,
  type Profile, type ProfileInput,
} from "../shared/schemas";
import { Busy } from "./IssueParts";

type Category = (typeof SKILL_CATEGORIES)[number];
interface Skill { name: string; category: Category; proficiency: number }

type Load = { status: "loading" } | { status: "error"; text: string } | { status: "ready"; profile: Profile | null };
type Notice = { tone: "ok" | "error"; text: string[] } | null;

const LANGUAGES = ["Python", "JavaScript", "TypeScript", "Java", "Go", "Rust", "C++", "C#", "Ruby", "PHP", "Kotlin", "Swift"];
const TOOLS: { name: string; category: Category }[] = [
  { name: "React", category: "framework" }, { name: "Vue", category: "framework" }, { name: "Django", category: "framework" },
  { name: "FastAPI", category: "framework" }, { name: "Flask", category: "framework" }, { name: "Node.js", category: "framework" },
  { name: "Pandas", category: "library" }, { name: "NumPy", category: "library" },
  { name: "Docker", category: "skill" }, { name: "Git", category: "skill" }, { name: "SQL", category: "skill" }, { name: "Testing", category: "skill" },
];
const INTERESTS = ["web development", "machine learning", "data science", "devops", "mobile apps", "security", "developer tools", "documentation", "games", "command-line tools"];

/** How well the person knows a skill: three plain words instead of a 0-1 number. A stored value is kept until they change it. */
const LEVELS = [{ label: "Learning", value: 0.3 }, { label: "Comfortable", value: 0.6 }, { label: "Strong", value: 0.9 }];
const levelIndex = (p: number) => (p < 0.45 ? 0 : p < 0.75 ? 1 : 2);

const humanize = (s: string) => s.replace(/_/g, " ");
const display = (name: string) => [...LANGUAGES, ...TOOLS.map((t) => t.name)].find((x) => x.toLowerCase() === name) ?? name;
const norm = (s: string) => s.trim().toLowerCase();

const isTool = (s: Skill) => s.category !== "language";

export function ProfileEditor({ call }: { call: Bridge }) {
  const [load, setLoad] = useState<Load>({ status: "loading" });
  const [skills, setSkills] = useState<Skill[]>([]);
  const [interests, setInterests] = useState<string[]>([]);
  const [goals, setGoals] = useState<string[]>([]);
  const [taskTypes, setTaskTypes] = useState<string[]>([]);
  const [difficulty, setDifficulty] = useState("");
  const [level, setLevel] = useState("");
  const [notice, setNotice] = useState<Notice>(null);
  const [saving, setSaving] = useState(false);

  function fill(p: Profile | null) {
    setSkills((p?.skills ?? []).map((s) => ({
      name: s.name, category: (SKILL_CATEGORIES as readonly string[]).includes(s.category) ? (s.category as Category) : "skill", proficiency: s.proficiency,
    })));
    setInterests(p?.interests ?? []);
    setGoals(p?.contribution_goals ?? []);
    setTaskTypes(p?.preferred_task_types ?? []);
    setDifficulty(p?.preferred_difficulty ? String(p.preferred_difficulty) : "");
    setLevel(p?.experience_level ?? "");
  }

  useEffect(() => {
    let cancelled = false;
    setLoad({ status: "loading" });
    void call({ type: "profile.get" }, profileSchema.nullable()).then((r) => {
      if (cancelled) return;
      if (r.ok) {
        fill(r.data);
        setLoad({ status: "ready", profile: r.data });
      } else setLoad({ status: "error", text: errorText(r.error) });
    });
    return () => {
      cancelled = true;
    };
  }, [call]);

  function build(): { ok: true; value: ProfileInput } | { ok: false; errors: string[] } {
    const candidate = {
      skills: skills.map((s) => ({ name: norm(s.name), category: s.category, proficiency: s.proficiency })),
      interests,
      contribution_goals: goals,
      preferred_task_types: taskTypes,
      preferred_difficulty: difficulty ? Number(difficulty) : null,
      experience_level: level || null,
    };
    const parsed = profileInputSchema.safeParse(candidate);
    if (parsed.success) return { ok: true, value: parsed.data };
    return { ok: false, errors: parsed.error.issues.slice(0, 4).map((i) => `${String(i.path[0] ?? "profile")}: ${i.message}`) };
  }

  async function save() {
    setNotice(null);
    const built = build();
    if (!built.ok) return setNotice({ tone: "error", text: built.errors });
    if (load.status !== "ready") return;
    setSaving(true);
    const r = await call({ type: "profile.save", profile: built.value, exists: load.profile !== null }, profileSchema);
    setSaving(false);
    if (r.ok) {
      fill(r.data);
      setLoad({ status: "ready", profile: r.data });
      setNotice({ tone: "ok", text: ["Profile saved. New match scores use it."] });
    } else {
      setNotice({ tone: "error", text: [errorText(r.error), ...(r.error.details ?? [])] });
    }
  }

  if (load.status === "loading") return <Busy>Loading your profile…</Busy>;
  if (load.status === "error") return <p className="message error" role="alert">{load.text}</p>;

  const taken = new Set(skills.map((s) => norm(s.name)));
  const addSkill = (name: string, category: Category) => {
    const n = norm(name);
    if (n && !taken.has(n)) setSkills([...skills, { name: n, category, proficiency: 0.6 }]);
  };
  const setLevelOf = (name: string, value: number) => setSkills(skills.map((s) => (s.name === name ? { ...s, proficiency: value } : s)));
  const drop = (name: string) => setSkills(skills.filter((s) => s.name !== name));
  const toggleTask = (t: string) => setTaskTypes((cur) => (cur.includes(t) ? cur.filter((x) => x !== t) : [...cur, t]));

  const saved = load.profile;
  // Completion reflects what is SAVED on the backend, not what is typed but not yet saved.
  const basics = saved
    ? [
        { done: saved.skills.some((s) => s.category === "language"), todo: "a language" },
        { done: saved.skills.some((s) => s.category !== "language"), todo: "a framework or tool" },
        { done: saved.interests.length > 0, todo: "an area of interest" },
        { done: Boolean(saved.experience_level) || saved.preferred_difficulty !== null, todo: "your experience level" },
      ]
    : [];
  const doneCount = basics.filter((b) => b.done).length;

  return (
    <section aria-labelledby="profile-h">
      <div className="profile-intro">
        <div>
          <h2 id="profile-h">Your profile</h2>
          <p className="help">Tell us what you know and like. It is only used to rank issues for you.</p>
        </div>
        {saved && (
          <div className="completion">
            <span id="completion-label">{doneCount} of {basics.length} basics saved</span>
            <progress aria-labelledby="completion-label" max={basics.length} value={doneCount} />
            {doneCount < basics.length && <span>Still missing: {basics.filter((b) => !b.done).map((b) => b.todo).join(", ")}.</span>}
          </div>
        )}
      </div>

      {!saved && (
        <p className="message info" role="status">
          You have no profile yet. Until you add one, skills and interests show as <strong>unknown</strong> in match scores; they are
          never treated as a perfect match.
        </p>
      )}

      <form noValidate onSubmit={(e) => { e.preventDefault(); void save(); }}>
        <SkillGroup
          title="Languages you use" addLabel="Add a language" skills={skills.filter((s) => !isTool(s))}
          suggestions={LANGUAGES.map((name) => ({ name, category: "language" as Category }))} addCategory="language"
          taken={taken} onAdd={addSkill} onLevel={setLevelOf} onRemove={drop}
        />
        <SkillGroup
          title="Frameworks and tools" addLabel="Add a framework or tool" skills={skills.filter(isTool)} suggestions={TOOLS} addCategory="skill"
          taken={taken} onAdd={addSkill} onLevel={setLevelOf} onRemove={drop}
        />
        <TagField title="Areas you are interested in" addLabel="Add an interest" values={interests} onChange={setInterests} suggestions={INTERESTS} max={50} />

        <details className="more">
          <summary>More preferences (optional)</summary>
          <TagField title="What you hope to get out of contributing" addLabel="Add a goal" values={goals} onChange={setGoals} suggestions={["learn a new language", "build a portfolio", "give back", "get hired"]} max={20} />
          <fieldset>
            <legend>Kinds of work you enjoy</legend>
            <div className="task-grid">
              {TASK_TYPES.map((t) => (
                <label key={t} className="check">
                  <input type="checkbox" checked={taskTypes.includes(t)} onChange={() => toggleTask(t)} /> {humanize(t)}
                </label>
              ))}
            </div>
          </fieldset>
          <label htmlFor="difficulty">Difficulty you are aiming for</label>
          <select id="difficulty" value={difficulty} onChange={(e) => setDifficulty(e.target.value)}>
            <option value="">No preference</option>
            {[1, 2, 3, 4, 5].map((n) => <option key={n} value={n}>{n}{n === 1 ? " (easiest)" : n === 5 ? " (hardest)" : ""}</option>)}
          </select>
          <label htmlFor="level">Experience level</label>
          <select id="level" value={level} onChange={(e) => setLevel(e.target.value)}>
            <option value="">Not stated</option>
            {EXPERIENCE_LEVELS.map((l) => <option key={l} value={l}>{l}</option>)}
          </select>
        </details>

        <div className="buttons">
          <button type="submit" className="primary" disabled={saving}>{saving ? "Saving…" : saved ? "Save profile" : "Create profile"}</button>
        </div>
      </form>

      <div aria-live="polite">
        {notice && (
          <div className={`message ${notice.tone}`} role={notice.tone === "error" ? "alert" : "status"}>
            {notice.text.map((t) => <p key={t}>{t}</p>)}
          </div>
        )}
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------------------------- pieces

interface SkillGroupProps {
  title: string;
  addLabel: string;
  skills: Skill[];
  suggestions: { name: string; category: Category }[];
  addCategory: Category;
  taken: Set<string>;
  onAdd(name: string, category: Category): void;
  onLevel(name: string, value: number): void;
  onRemove(name: string): void;
}

/** Selected skills as chips (each with a three-step level) plus one-click suggestions and a free-text add. */
function SkillGroup(p: SkillGroupProps) {
  const id = useId();
  const [text, setText] = useState("");
  const add = () => {
    p.onAdd(text, p.addCategory);
    setText("");
  };
  return (
    <div className="field" role="group" aria-labelledby={`${id}-t`}>
      <span className="field-label" id={`${id}-t`}>{p.title}</span>
      {p.skills.length === 0 && <p className="help">Nothing added yet. Pick from the suggestions or type your own.</p>}
      <ul className="chips" aria-label={`Selected: ${p.title}`}>
        {p.skills.map((s) => (
          <li className="chip" key={s.name}>
            <span>{display(s.name)}</span>
            <select aria-label={`${display(s.name)} level`} value={levelIndex(s.proficiency)} onChange={(e) => p.onLevel(s.name, LEVELS[Number(e.target.value)].value)}>
              {LEVELS.map((l, i) => <option key={l.label} value={i}>{l.label}</option>)}
            </select>
            <button type="button" aria-label={`Remove ${display(s.name)}`} onClick={() => p.onRemove(s.name)}>×</button>
          </li>
        ))}
      </ul>
      <div className="suggest" role="group" aria-label={`Suggestions: ${p.title}`}>
        <span className="help">Quick add:</span>
        {p.suggestions.filter((s) => !p.taken.has(s.name.toLowerCase())).map((s) => (
          <button key={s.name} type="button" onClick={() => p.onAdd(s.name, s.category)}>+ {s.name}</button>
        ))}
      </div>
      <div className="add-row">
        <input
          type="text" aria-label={p.addLabel} placeholder={p.addLabel} value={text} maxLength={100} spellCheck={false} autoComplete="off"
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" || e.key === ",") { e.preventDefault(); add(); } }}
        />
        <button type="button" onClick={add} disabled={!text.trim()}>Add</button>
      </div>
    </div>
  );
}

function TagField({ title, addLabel, values, onChange, suggestions, max }: {
  title: string; addLabel: string; values: string[]; onChange(v: string[]): void; suggestions: string[]; max: number;
}) {
  const id = useId();
  const [text, setText] = useState("");
  const add = (raw: string) => {
    const v = norm(raw);
    if (v && !values.includes(v) && values.length < max) onChange([...values, v]);
    setText("");
  };
  return (
    <div className="field" role="group" aria-labelledby={`${id}-t`}>
      <span className="field-label" id={`${id}-t`}>{title}</span>
      {values.length > 0 && (
        <ul className="chips" aria-label={`Selected: ${title}`}>
          {values.map((v) => (
            <li className="chip" key={v}>
              <span>{v}</span>
              <button type="button" aria-label={`Remove ${v}`} onClick={() => onChange(values.filter((x) => x !== v))}>×</button>
            </li>
          ))}
        </ul>
      )}
      <div className="suggest" role="group" aria-label={`Suggestions: ${title}`}>
        <span className="help">Quick add:</span>
        {suggestions.filter((s) => !values.includes(s)).map((s) => (
          <button key={s} type="button" onClick={() => add(s)}>+ {s}</button>
        ))}
      </div>
      <div className="add-row">
        <input
          type="text" aria-label={addLabel} placeholder={addLabel} value={text} maxLength={100} spellCheck={false} autoComplete="off"
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" || e.key === ",") { e.preventDefault(); add(text); } }}
        />
        <button type="button" onClick={() => add(text)} disabled={!text.trim()}>Add</button>
      </div>
    </div>
  );
}
