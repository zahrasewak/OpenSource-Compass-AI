import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { createBridge } from "../src/content/bridge";
import { Options, type OptionsDeps } from "../src/options/Options";
import type { AdminRequest } from "../src/shared/messages";
import { difficulty, fail, fakeSender, matchSearch, ok, profileOut, savedIssue, searchItem, status, storedMatches } from "./fixtures";

const SECRET_CODE = "PAIRING-CODE-123";

type Handlers = Parameters<typeof fakeSender>[0];

function setup(handlers: Handlers = {}, over: Partial<OptionsDeps> = {}) {
  const sender = fakeSender({
    "auth.state": () => ok(status({ signedIn: false })),
    "auth.me": () => ok({ id: 1, github_user_id: 5, username: "octocat" }),
    "settings.save": () => ok(status({ signedIn: false })),
    // What a signed-in person lands on (Find issues) asks for these; tests that care override them.
    "matches.search": () => ok(matchSearch([])),
    "matches.stored": () => ok(storedMatches()),
    "saved.list": () => ok({ items: [], total: 0, limit: 20, offset: 0 }),
    "profile.get": () => ok(null),
    "issue.difficulty": () => ok(difficulty()),
    ...handlers,
  });
  const deps: OptionsDeps = { call: createBridge(sender), ensureOrigin: vi.fn(async () => true), ...over };
  const user = userEvent.setup();
  render(<Options deps={deps} />);
  const sent = (type: AdminRequest["type"]) => sender.mock.calls.filter(([m]) => m.type === type).map(([m]) => m);
  return { deps, user, sender, sent };
}
const field = async (label: string | RegExp) => (await screen.findByLabelText(label)) as HTMLInputElement;

describe("signed out: account tab", () => {
  it("explains pairing and keeps the code field masked", async () => {
    setup();
    const code = await field("Pairing code");
    expect(code.type).toBe("password");
    expect(screen.getByText(/issue-pairing-code/)).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Profile" })).toBeDisabled();
    expect(screen.getByRole("tab", { name: "Saved issues" })).toBeDisabled();
  });

  it("signs in with a pairing code, clears the code from the page, and unlocks the other tabs", async () => {
    let signedIn = false;
    const { user, sent } = setup({
      "auth.state": () => ok(status({ signedIn })),
      "auth.connect": () => { signedIn = true; return ok(status({ signedIn: true })); },
    });
    await user.type(await field("Pairing code"), SECRET_CODE);
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("Signed in.")).toBeInTheDocument();
    expect(sent("auth.connect")).toEqual([{ type: "auth.connect", code: SECRET_CODE, keepSignedIn: false }]);
    expect(document.body.innerHTML).not.toContain(SECRET_CODE);
    expect(await screen.findByText(/octocat/)).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Profile" })).toBeEnabled();
  });

  it("requires a code before contacting anything", async () => {
    const { user, sent } = setup();
    await user.click(await screen.findByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/Enter the pairing code/);
    expect(sent("auth.connect")).toHaveLength(0);
  });

  it("shows a rejected code without revealing details, and still clears the field", async () => {
    const { user } = setup({ "auth.connect": () => fail("auth", "The pairing code is invalid, expired or already used.", { status: 401 }) });
    await user.type(await field("Pairing code"), SECRET_CODE);
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/pairing code is invalid, expired or already used/);
    expect((await field("Pairing code")).value).toBe("");
  });

  it("says 'too many attempts' with the wait when the backend rate-limits sign-in (not 'invalid code')", async () => {
    const { user } = setup({ "auth.connect": () => fail("rate_limited", "Too many attempts. Try again later.", { status: 429, retryAfterSeconds: 600 }) });
    await user.type(await field("Pairing code"), SECRET_CODE);
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Too many sign-in attempts. Try again in about 10 min.");
    expect(screen.getByRole("alert")).not.toHaveTextContent(/GitHub|invalid/);
    expect((await field("Pairing code")).value).toBe("");
  });

  it("describes the 'keep me signed in' trade-off honestly and sends the choice", async () => {
    const { user, sent } = setup({ "auth.connect": () => ok(status({ signedIn: true, keepSignedIn: true })) });
    expect(screen.queryByText(/unencrypted/)).not.toBeInTheDocument();
    const keep = await screen.findByLabelText(/Keep me signed in/);
    expect(keep).not.toBeChecked(); // off by default
    expect(screen.getByText(/unencrypted/)).toBeInTheDocument();
    await user.click(keep);
    await user.type(await field("Pairing code"), "abc");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(sent("auth.connect")).toHaveLength(1));
    expect(sent("auth.connect")[0]).toMatchObject({ keepSignedIn: true });
  });

  it("tells the user when their session ended", async () => {
    setup({ "auth.state": () => ok(status({ signedIn: false, sessionEnded: true })) });
    expect(await screen.findByText(/Your session ended/)).toBeInTheDocument();
  });

  it("asks Chrome for permission before using a non-default backend, and does not sign in if it is refused", async () => {
    const { user, deps, sent } = setup({}, { ensureOrigin: vi.fn(async () => false) });
    const url = await field("Backend URL");
    await user.clear(url);
    await user.type(url, "https://compass.example.com");
    await user.type(await field("Pairing code"), SECRET_CODE);
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/permission to contact https:\/\/compass.example.com was not granted/);
    expect(deps.ensureOrigin).toHaveBeenCalledWith("https://compass.example.com");
    expect(sent("settings.save")).toHaveLength(0);
    expect(sent("auth.connect")).toHaveLength(0);
  });

  it("does not ask for permission for the default localhost backend", async () => {
    const { user, deps } = setup({ "auth.connect": () => ok(status({ signedIn: true })) });
    await user.type(await field("Pairing code"), "x");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await screen.findByText("Signed in.");
    expect(deps.ensureOrigin).not.toHaveBeenCalled();
  });

  it.each(["http://example.com", "https://u:p@example.com", "https://example.com/path", "not a url"])("rejects the backend URL %s locally", async (bad) => {
    const { user, sent } = setup();
    const url = await field("Backend URL");
    await user.clear(url);
    await user.type(url, bad);
    await user.type(await field("Pairing code"), "x");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(sent("settings.save")).toHaveLength(0);
    expect(sent("auth.connect")).toHaveLength(0);
  });
});

describe("signed in: account tab", () => {
  it("shows who is signed in and when the session ends, never a token", async () => {
    const { user } = setup({ "auth.state": () => ok(status({ signedIn: true })) });
    await openTab(user, "Account");
    await waitFor(() => expect(screen.getByTestId("session-info")).toHaveTextContent(/octocat/));
    expect(screen.getByTestId("session-info")).toHaveTextContent(/This session ends on/);
    expect(screen.queryByLabelText("Pairing code")).not.toBeInTheDocument();
  });

  it("signs out through the background worker", async () => {
    let signedIn = true;
    const { user, sent } = setup({
      "auth.state": () => ok(status({ signedIn })),
      "auth.signOut": () => { signedIn = false; return ok(status({ signedIn: false })); },
    });
    await openTab(user, "Account");
    await user.click(await screen.findByRole("button", { name: "Sign out" }));
    expect(await screen.findByText(/Signed out/)).toBeInTheDocument();
    expect(sent("auth.signOut")).toHaveLength(1);
    expect(await screen.findByLabelText("Pairing code")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Profile" })).toBeDisabled();
  });

  it("sign out everywhere asks for confirmation first, and cancelling sends nothing", async () => {
    const { user, sent } = setup({ "auth.state": () => ok(status({ signedIn: true })) });
    await openTab(user, "Account");
    await user.click(await screen.findByRole("button", { name: "Sign out everywhere" }));
    const group = screen.getByRole("group", { name: "Confirm sign out everywhere" });
    expect(group).toHaveTextContent(/every.*session of your account/i);
    await user.click(within(group).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("group", { name: "Confirm sign out everywhere" })).not.toBeInTheDocument();
    expect(sent("auth.signOutAll")).toHaveLength(0);
  });

  it("sign out everywhere ends all sessions and returns to the sign-in form", async () => {
    let signedIn = true;
    const { user, sent } = setup({
      "auth.state": () => ok(status({ signedIn })),
      "auth.signOutAll": () => { signedIn = false; return ok(status({ signedIn: false })); },
    });
    await openTab(user, "Account");
    await user.click(await screen.findByRole("button", { name: "Sign out everywhere" }));
    await user.click(within(screen.getByRole("group", { name: "Confirm sign out everywhere" })).getByRole("button", { name: "Yes, sign out everywhere" }));
    expect(await screen.findByText(/Signed out everywhere/)).toBeInTheDocument();
    expect(sent("auth.signOutAll")).toHaveLength(1);
    expect(await screen.findByLabelText("Pairing code")).toBeInTheDocument();
  });

  it("if the server cannot confirm, the page says this browser is signed out but other sessions may remain", async () => {
    let signedIn = true;
    const { user } = setup({
      "auth.state": () => ok(status({ signedIn })),
      "auth.signOutAll": () => { signedIn = false; return fail("network", "Could not reach the backend."); },
    });
    await openTab(user, "Account");
    await user.click(await screen.findByRole("button", { name: "Sign out everywhere" }));
    await user.click(within(screen.getByRole("group", { name: "Confirm sign out everywhere" })).getByRole("button", { name: "Yes, sign out everywhere" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/This browser is signed out, but the server could not confirm/);
    expect(alert).toHaveTextContent(/Could not reach the backend/);
    expect(await screen.findByLabelText("Pairing code")).toBeInTheDocument();
  });

  it("warns that changing the backend signs the user out", async () => {
    const { user } = setup({ "auth.state": () => ok(status({ signedIn: true })) });
    await openTab(user, "Account");
    expect(await screen.findByText(/Changing it signs you out/)).toBeInTheDocument();
  });

  it("saves the scorer choice", async () => {
    const { user, sent } = setup({ "auth.state": () => ok(status({ signedIn: true })), "settings.save": () => ok(status({ signedIn: true, experimentalScorer: true })) });
    await openTab(user, "Account");
    await user.click(await screen.findByLabelText(/experimental scoring method/));
    await user.click(screen.getByRole("button", { name: "Save settings" }));
    expect(await screen.findByText("Settings saved.")).toBeInTheDocument();
    expect(sent("settings.save")[0]).toMatchObject({ baseUrl: "http://localhost:8000", useExperimentalScorer: true });
  });
});

async function openTab(user: ReturnType<typeof userEvent.setup>, name: string) {
  await user.click(await screen.findByRole("tab", { name }));
}
const signedInState = { "auth.state": () => ok(status({ signedIn: true })) };

describe("find issues tab", () => {
  const item = (n: number, over: Record<string, unknown> = {}) =>
    searchItem({
      rank: n, final_score: 80 - n,
      issue: { number: n, title: `Issue ${n}`, html_url: `https://github.com/acme/ml-api/issues/${n}` },
      reasons: [`Reason ${n} for you.`], missing_skills: [], warnings: [], ...over,
    });

  it("is where a signed-in person lands, and shows ranked issues with score, reason and a way to open each", async () => {
    const { sent } = setup({ ...signedInState, "matches.search": () => ok(matchSearch([item(1), item(2)])) });
    expect(await screen.findByRole("heading", { name: "Find issues" })).toBeInTheDocument();
    expect(await screen.findByText("Issue 1")).toBeInTheDocument();
    expect(screen.getByLabelText("Match score 79 out of 100")).toBeInTheDocument();
    expect(screen.getByText("Reason 1 for you.")).toBeInTheDocument();
    const open = screen.getByRole("link", { name: "Open on GitHub: Issue 1" });
    expect(open).toHaveAttribute("href", "https://github.com/acme/ml-api/issues/1");
    expect(open).toHaveAttribute("rel", "noopener noreferrer");
    expect(sent("matches.search")[0]).toEqual({ type: "matches.search", limit: 10, offset: 0 }); // no filters, nothing hidden
  });

  it("sends the chosen filters, and refuses a malformed repository before asking the backend", async () => {
    const { user, sent } = setup({ ...signedInState, "matches.search": () => ok(matchSearch([item(1)])) });
    await screen.findByText("Issue 1");
    await user.type(screen.getByLabelText("Language"), "Python");
    await user.type(screen.getByLabelText("Topic"), "cli");
    await user.selectOptions(screen.getByLabelText("Aim for"), "3");
    await user.click(screen.getByRole("button", { name: "Show recommendations" }));
    await waitFor(() => expect(sent("matches.search")).toHaveLength(2));
    expect(sent("matches.search")[1]).toEqual({ type: "matches.search", limit: 10, offset: 0, language: "Python", topic: "cli", preferredDifficulty: 3 });

    await user.click(screen.getByText("Look in a specific repository"));
    await user.type(screen.getByLabelText("Repository (owner/name)"), "not a repo");
    await user.click(screen.getByRole("button", { name: "Show recommendations" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/owner\/name/);
    expect(sent("matches.search")).toHaveLength(2);
  });

  it("asks the backend to look a repository up only when one is named", async () => {
    const { user, sent } = setup({ ...signedInState, "matches.search": () => ok(matchSearch([item(1)])) });
    await screen.findByText("Issue 1");
    await user.click(screen.getByText("Look in a specific repository"));
    await user.type(screen.getByLabelText("Repository (owner/name)"), "psf/requests");
    await user.click(screen.getByRole("button", { name: "Show recommendations" }));
    await waitFor(() => expect(sent("matches.search")).toHaveLength(2));
    expect(sent("matches.search")[1]).toMatchObject({ repository: "psf/requests" });
  });

  it("shows a friendly empty state with a next step, not a blank page", async () => {
    const { user } = setup({ ...signedInState, "matches.search": () => ok(matchSearch([], { ranking_reliability: { status: "no_candidates", reasons: [] } })) });
    expect(await screen.findByText("Nothing to recommend yet")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Fill in your profile" }));
    expect(await screen.findByRole("heading", { name: "Your profile" })).toBeInTheDocument();
  });

  it("says when no issue matches the filters and offers to clear them", async () => {
    const { user, sent } = setup({ ...signedInState, "matches.search": (m: { language?: string }) => ok(matchSearch(m.language ? [] : [item(1)])) });
    await screen.findByText("Issue 1");
    await user.type(screen.getByLabelText("Language"), "Cobol");
    await user.click(screen.getByRole("button", { name: "Show recommendations" }));
    expect(await screen.findByText("No issues match those filters")).toBeInTheDocument();
    await user.click(screen.getAllByRole("button", { name: "Clear filters" })[0]);
    expect(await screen.findByText("Issue 1")).toBeInTheDocument();
    expect(sent("matches.search").at(-1)).toEqual({ type: "matches.search", limit: 10, offset: 0 });
  });

  it("marks low-confidence scores in words and points to the profile", async () => {
    const { user } = setup({
      ...signedInState,
      "matches.search": () => ok(matchSearch([item(1, { insufficient_information: true })], { ranking_reliability: { status: "insufficient", reasons: ["Add skills to your profile to get meaningful scores."] } })),
    });
    expect(await screen.findByText("Low confidence")).toBeInTheDocument();
    expect(screen.getByText(/Not enough profile information/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Add to your profile" }));
    expect(await screen.findByRole("heading", { name: "Your profile" })).toBeInTheDocument();
  });

  it("saves and unsaves from the list, with feedback each time", async () => {
    const { user, sent } = setup({
      ...signedInState, "matches.search": () => ok(matchSearch([item(1)])),
      "issue.save": () => ok(savedIssue({ id: 55, number: 1, title: "Issue 1" })), "saved.remove": () => ok(null),
    });
    await user.click(await screen.findByRole("button", { name: "Save Issue 1" }));
    expect(await screen.findByText(/Saved “Issue 1”/)).toBeInTheDocument();
    expect(sent("issue.save")).toEqual([{ type: "issue.save", ref: { owner: "acme", repo: "ml-api", number: 1 } }]);
    await user.click(await screen.findByRole("button", { name: "Unsave Issue 1" }));
    expect(await screen.findByText(/Removed “Issue 1”/)).toBeInTheDocument();
    expect(sent("saved.remove")).toEqual([{ type: "saved.remove", savedId: 55 }]);
    expect(screen.getByRole("button", { name: "Save Issue 1" })).toBeInTheDocument();
  });

  it("shows an issue as saved when it already is", async () => {
    setup({
      ...signedInState, "matches.search": () => ok(matchSearch([item(1)])),
      "saved.list": () => ok({ items: [savedIssue({ id: 9, number: 1 })], total: 1, limit: 100, offset: 0 }),
    });
    expect(await screen.findByRole("button", { name: "Unsave Issue 1" })).toBeInTheDocument();
    expect(screen.getByText("Saved")).toBeInTheDocument();
  });

  it("keeps the issue unsaved and says why when saving fails", async () => {
    const { user } = setup({ ...signedInState, "matches.search": () => ok(matchSearch([item(1)])), "issue.save": () => fail("server", "Backend is down.") });
    await user.click(await screen.findByRole("button", { name: "Save Issue 1" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/Could not save “Issue 1”.*Backend is down/);
    expect(screen.getByRole("button", { name: "Save Issue 1" })).toBeEnabled();
  });

  it("keeps the details closed until asked, then shows difficulty, reasons and what to learn first", async () => {
    const { user, sent } = setup({
      ...signedInState,
      "matches.search": () => ok(matchSearch([item(1, { reasons: ["First reason.", "Second reason."], missing_skills: ["fastapi"], warnings: ["Keyword detection may be incomplete."] })])),
    });
    await screen.findByText("Issue 1");
    expect(screen.queryByText("Second reason.")).not.toBeInTheDocument();
    expect(sent("issue.difficulty")).toHaveLength(0); // nothing is fetched for issues nobody opened
    await user.click(screen.getByRole("button", { name: /Details/ }));
    expect(await screen.findByText(/Difficulty:/)).toHaveTextContent(/easy/);
    expect(screen.getByText("Second reason.")).toBeInTheDocument();
    expect(screen.getByText("You may want to learn this first")).toBeInTheDocument();
    expect(screen.getByText("fastapi")).toBeInTheDocument();
    expect(sent("issue.difficulty")).toEqual([{ type: "issue.difficulty", ref: { owner: "acme", repo: "ml-api", number: 1 } }]);
  });

  it("does not invent a difficulty when there is no label", async () => {
    const { user } = setup({
      ...signedInState, "matches.search": () => ok(matchSearch([item(1)])),
      "issue.difficulty": () => ok(difficulty({ deterministic_signals: { difficulty_label_hint: null } })),
    });
    await user.click(await screen.findByRole("button", { name: /Details/ }));
    expect(await screen.findByText(/have not put a difficulty label/)).toBeInTheDocument();
  });

  it("pages with 'Show more' and appends", async () => {
    const all = Array.from({ length: 12 }, (_, i) => item(i + 1));
    const { user, sent } = setup({
      ...signedInState,
      "matches.search": (m: { offset: number; limit: number }) => ok(matchSearch(all.slice(m.offset, m.offset + m.limit), { total: 12, offset: m.offset })),
    });
    await screen.findByText("Issue 10");
    expect(screen.queryByText("Issue 11")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Show more" }));
    expect(await screen.findByText("Issue 11")).toBeInTheDocument();
    expect(screen.getByText("Issue 1")).toBeInTheDocument();
    expect(sent("matches.search").map((m) => (m as { offset: number }).offset)).toEqual([0, 10]);
    expect(screen.queryByRole("button", { name: "Show more" })).not.toBeInTheDocument();
  });

  it("shows an error with a retry", async () => {
    let n = 0;
    const { user } = setup({ ...signedInState, "matches.search": () => (++n === 1 ? fail("server", "Backend is down.") : ok(matchSearch([item(1)]))) });
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Backend is down.");
    await user.click(within(alert).getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Issue 1")).toBeInTheDocument();
  });

  it("goes to Account when the session has ended", async () => {
    const { user } = setup({ ...signedInState, "matches.search": () => fail("auth", "Session ended.", { status: 401 }) });
    await user.click(await screen.findByRole("button", { name: "Go to Account" }));
    expect(await screen.findByRole("heading", { name: "Account" })).toBeInTheDocument();
  });

  it("lists nothing until the backend has answered (no placeholder data)", async () => {
    setup({ ...signedInState, "matches.search": () => new Promise(() => undefined) });
    expect(await screen.findByText("Looking for issues that fit you…")).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Recommended issues" })).not.toBeInTheDocument();
  });
});

describe("profile tab", () => {
  it("shows a loading state, then the stored profile as selectable tags with a level", async () => {
    let release!: (v: unknown) => void;
    const { user } = setup({ ...signedInState, "profile.get": () => new Promise((r) => (release = r)) });
    await openTab(user, "Profile");
    expect(await screen.findByText("Loading your profile…")).toBeInTheDocument();
    release(ok(profileOut()));
    expect(await screen.findByLabelText("Python level")).toHaveValue("2"); // 0.9 reads as "Strong"
    expect(within(screen.getByRole("list", { name: "Selected: Areas you are interested in" })).getByText("machine learning")).toBeInTheDocument();
    expect(screen.getByLabelText("Difficulty you are aiming for")).toHaveValue("2");
    expect(screen.getByLabelText("Experience level")).toHaveValue("intermediate");
    expect(screen.getByLabelText("bug fixing")).toBeChecked();
  });

  it("shows completion that reflects what is saved, and says what is missing", async () => {
    const { user } = setup({ ...signedInState, "profile.get": () => ok(profileOut()) });
    await openTab(user, "Profile");
    expect(await screen.findByText("3 of 4 basics saved")).toBeInTheDocument();
    expect(screen.getByText(/Still missing: a framework or tool/)).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "3 of 4 basics saved" })).toHaveAttribute("value", "3");
  });

  it("does not show a completion indicator before any profile exists, and explains unknown scores", async () => {
    const { user } = setup({ ...signedInState, "profile.get": () => ok(null) });
    await openTab(user, "Profile");
    expect(await screen.findByText(/never treated as a perfect match/)).toBeInTheDocument();
    expect(screen.queryByText(/basics saved/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create profile" })).toBeInTheDocument();
  });

  it("creates a profile (POST) from quick-add and typed tags, with normalised, validated data", async () => {
    const { user, sent } = setup({ ...signedInState, "profile.get": () => ok(null), "profile.save": () => ok(profileOut()) });
    await openTab(user, "Profile");
    await user.click(await screen.findByRole("button", { name: "+ Python" }));
    await user.selectOptions(screen.getByLabelText("Python level"), "Strong");
    await user.type(screen.getByLabelText("Add a framework or tool"), "  FastAPI {Enter}");
    await user.type(screen.getByLabelText("Add an interest"), "ML{Enter}ml{Enter}Testing,");
    await user.click(screen.getByText("More preferences (optional)"));
    await user.click(screen.getByLabelText("testing"));
    await user.selectOptions(screen.getByLabelText("Difficulty you are aiming for"), "3");
    await user.selectOptions(screen.getByLabelText("Experience level"), "beginner");
    await user.click(screen.getByRole("button", { name: "Create profile" }));
    expect(await screen.findByText(/Profile saved/)).toBeInTheDocument();
    expect(sent("profile.save")[0]).toEqual({
      type: "profile.save", exists: false,
      profile: {
        skills: [{ name: "python", category: "language", proficiency: 0.9 }, { name: "fastapi", category: "skill", proficiency: 0.6 }],
        interests: ["ml", "testing"], contribution_goals: [], preferred_task_types: ["testing"], preferred_difficulty: 3, experience_level: "beginner",
      },
    });
    expect(await screen.findByRole("button", { name: "Save profile" })).toBeInTheDocument(); // now an update
  });

  it("does not let the Enter key in a tag box submit the whole profile", async () => {
    const { user, sent } = setup({ ...signedInState, "profile.get": () => ok(profileOut()) });
    await openTab(user, "Profile");
    await user.type(await screen.findByLabelText("Add an interest"), "rust{Enter}");
    expect(sent("profile.save")).toHaveLength(0);
    expect(screen.getByText("rust")).toBeInTheDocument();
  });

  it("updates an existing profile (PUT) and keeps a stored proficiency the person did not touch", async () => {
    const stored = profileOut({ skills: [{ name: "python", category: "language", proficiency: 0.8, source: "self_reported" }] });
    const { user, sent } = setup({ ...signedInState, "profile.get": () => ok(stored), "profile.save": () => ok(profileOut({ interests: ["rust"] })) });
    await openTab(user, "Profile");
    await user.click(await screen.findByRole("button", { name: "Remove machine learning" }));
    await user.type(screen.getByLabelText("Add an interest"), "rust{Enter}");
    await user.click(screen.getByRole("button", { name: "Save profile" }));
    expect(await screen.findByText(/Profile saved/)).toBeInTheDocument();
    expect(sent("profile.save")[0]).toMatchObject({ exists: true, profile: { interests: ["rust"], skills: [{ name: "python", proficiency: 0.8 }] } });
  });

  it("lists a skill once, however it is added", async () => {
    const { user } = setup({ ...signedInState, "profile.get": () => ok(profileOut()) });
    await openTab(user, "Profile");
    await user.type(await screen.findByLabelText("Add a language"), "PYTHON{Enter}");
    expect(screen.getAllByLabelText("Python level")).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "+ Python" })).not.toBeInTheDocument(); // already chosen, so no longer suggested
  });

  it("shows the server's validation details and keeps the user's edits", async () => {
    const { user } = setup({
      ...signedInState, "profile.get": () => ok(profileOut()),
      "profile.save": () => fail("invalid_request", "Request validation failed", { status: 422, details: ["interests: duplicate entry: 'a'"] }),
    });
    await openTab(user, "Profile");
    await user.type(await screen.findByLabelText("Add an interest"), "a{Enter}");
    await user.click(screen.getByRole("button", { name: "Save profile" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Request validation failed");
    expect(alert).toHaveTextContent("interests: duplicate entry");
    expect(screen.getByRole("button", { name: "Remove a" })).toBeInTheDocument(); // the edit is still there
  });

  it("shows a load error instead of an empty form", async () => {
    const { user } = setup({ ...signedInState, "profile.get": () => fail("network", "Could not reach the backend. Is it running and is the URL correct?") });
    await openTab(user, "Profile");
    expect(await screen.findByRole("alert")).toHaveTextContent(/Could not reach the backend/);
    expect(screen.queryByRole("button", { name: /Save profile|Create profile/ })).not.toBeInTheDocument();
  });

  it("removes a skill", async () => {
    const { user } = setup({ ...signedInState, "profile.get": () => ok(profileOut()) });
    await openTab(user, "Profile");
    await user.click(await screen.findByRole("button", { name: "Remove Python" }));
    expect(screen.queryByLabelText("Python level")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "+ Python" })).toBeInTheDocument(); // and it can be added back
  });
});

describe("saved issues tab", () => {
  const page = (items: unknown[], total = items.length, offset = 0) => ok({ items, total, limit: 20, offset });

  it("shows loading, then the list with safe links", async () => {
    let release!: (v: unknown) => void;
    const { user } = setup({ ...signedInState, "saved.list": () => new Promise((r) => (release = r)) });
    await openTab(user, "Saved issues");
    expect(await screen.findByText("Loading saved issues…")).toBeInTheDocument();
    release(page([savedIssue({ id: 1, title: "Fix parser" }), savedIssue({ id: 2, title: "Bad link", html_url: "javascript:alert(1)" })]));
    const link = await screen.findByRole("link", { name: "Open on GitHub: Fix parser" });
    expect(link).toHaveAttribute("href", "https://github.com/acme/ml-api/issues/7");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(screen.queryByRole("link", { name: /Bad link/ })).not.toBeInTheDocument(); // unsafe URLs are shown as text, not links
    expect(screen.getByText("Bad link")).toBeInTheDocument();
  });

  it("shows a score only where one was stored, and says so otherwise", async () => {
    const stored = storedMatches([{
      id: 1, generated_at: "2026-01-01T00:00:00Z", model: { id: "heuristic_v1" }, repository: { owner: "acme", name: "ml-api", full_name: "acme/ml-api" },
      issue: { number: 7, title: "Fix parser", html_url: "https://github.com/acme/ml-api/issues/7" }, final_score: 63.4, coverage: 0.5, insufficient_information: false,
    }]);
    const { user } = setup({
      ...signedInState, "matches.stored": () => ok(stored),
      "saved.list": () => page([savedIssue({ id: 1, number: 7, title: "Fix parser" }), savedIssue({ id: 2, number: 9, title: "Other" })]),
    });
    await openTab(user, "Saved issues");
    expect(await screen.findByLabelText("Match score 63 out of 100")).toBeInTheDocument();
    expect(screen.getAllByText("No score yet")).toHaveLength(1);
  });

  it("shows a friendly empty state that leads back to Find issues", async () => {
    const { user } = setup({ ...signedInState, "saved.list": () => page([]) });
    await openTab(user, "Saved issues");
    expect(await screen.findByText("Nothing saved yet")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Find issues" }));
    expect(await screen.findByRole("heading", { name: "Find issues" })).toBeInTheDocument();
  });

  it("shows an error with retry", async () => {
    let n = 0;
    const { user } = setup({ ...signedInState, "saved.list": () => (++n <= 2 ? fail("server", "Backend is down.") : page([savedIssue({ title: "Back" })])) });
    await openTab(user, "Saved issues");
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Backend is down.");
    await user.click(within(alert).getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("link", { name: "Open on GitHub: Back" })).toBeInTheDocument();
  });

  it("unsaves an entry, confirms it, and refreshes the list", async () => {
    let items = [savedIssue({ id: 1, title: "One" }), savedIssue({ id: 2, title: "Two" })];
    const { user, sent } = setup({
      ...signedInState,
      "saved.list": () => page(items),
      "saved.remove": (m: { savedId: number }) => { items = items.filter((i) => i.id !== m.savedId); return ok(null); },
    });
    await openTab(user, "Saved issues");
    await user.click(await screen.findByRole("button", { name: "Unsave One" }));
    await waitFor(() => expect(screen.queryByRole("link", { name: "Open on GitHub: One" })).not.toBeInTheDocument());
    expect(screen.getByRole("link", { name: "Open on GitHub: Two" })).toBeInTheDocument();
    expect(await screen.findByText(/Removed “One”/)).toBeInTheDocument();
    expect(sent("saved.remove")).toEqual([{ type: "saved.remove", savedId: 1 }]);
  });

  it("keeps the entry and says why when removal fails", async () => {
    const { user } = setup({ ...signedInState, "saved.list": () => page([savedIssue({ id: 1, title: "One" })]), "saved.remove": () => fail("server", "Try later.") });
    await openTab(user, "Saved issues");
    await user.click(await screen.findByRole("button", { name: "Unsave One" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/Could not remove “One”/);
    expect(screen.getByRole("link", { name: "Open on GitHub: One" })).toBeInTheDocument();
  });

  it("pages through results", async () => {
    const all = Array.from({ length: 25 }, (_, i) => savedIssue({ id: i + 1, title: `Issue ${i + 1}` }));
    const { user, sent } = setup({
      ...signedInState,
      "saved.list": (m: { offset: number; limit: number }) => ok({ items: all.slice(m.offset, m.offset + m.limit), total: 25, limit: m.limit, offset: m.offset }),
    });
    await openTab(user, "Saved issues");
    expect(await screen.findByRole("link", { name: "Open on GitHub: Issue 1" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByRole("link", { name: "Open on GitHub: Issue 21" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
    const offsets = sent("saved.list").map((m) => (m as { offset: number }).offset);
    expect(offsets.slice(-2)).toEqual([0, 20]); // (the Find issues tab also asks once, for its saved markers)
  });
});

describe("page-level behaviour", () => {
  it("shows an error if the background worker cannot be reached", async () => {
    render(<Options deps={{ call: createBridge(async () => { throw new Error("no worker"); }), ensureOrigin: async () => true }} />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/Could not reach the extension/);
  });
});
