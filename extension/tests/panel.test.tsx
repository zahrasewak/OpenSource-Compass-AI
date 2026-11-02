import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { createBridge } from "../src/content/bridge";
import { Panel } from "../src/content/Panel";
import type { AdminRequest, Request } from "../src/shared/messages";
import {
  defaultHandlers, difficulty, fail, fakeSender, feedbackResult, health, ISSUE, issueMatch, lookup, ok, predicted, readiness, report, savedIssue, status,
} from "./fixtures";

type Over = Parameters<typeof fakeSender>[0];

function setup(over: Over = {}) {
  const sender = fakeSender({ ...defaultHandlers(), ...over });
  const user = userEvent.setup();
  render(<Panel bridge={createBridge(sender)} issue={ISSUE} />);
  const sent = (type: (Request | AdminRequest)["type"]) => sender.mock.calls.filter(([m]) => m.type === type).map(([m]) => m);
  return { sender, user, sent };
}
const never = () => new Promise(() => undefined);
/** The breakdown is tucked away until asked for (progressive disclosure). */
const openBreakdown = async (user: ReturnType<typeof userEvent.setup>) => user.click(await screen.findByRole("button", { name: /Score breakdown/ }));

describe("match section", () => {
  it("shows a loading state, then the score, breakdown and issue details", async () => {
    let release!: (v: unknown) => void;
    const { user } = setup({ match: () => new Promise((r) => (release = r)) });
    expect(await screen.findByText("Scoring this issue…")).toBeInTheDocument();
    await waitFor(() => expect(release).toBeTypeOf("function"));
    release(ok(issueMatch()));
    expect(await screen.findByLabelText("Match score 63 out of 100")).toBeInTheDocument();
    expect(screen.getByText("Parser crashes on load")).toBeInTheDocument();
    expect(screen.getAllByText(/acme\/ml-api #7/)).toHaveLength(2); // panel header + match section
    expect(screen.getByText("Some overlap with your skills.")).toBeInTheDocument();
    expect(screen.queryByRole("progressbar", { name: "Skill compatibility" })).not.toBeInTheDocument(); // closed until asked for
    await openBreakdown(user);
    expect(screen.getByRole("progressbar", { name: "Skill compatibility" })).toHaveAttribute("aria-valuenow", "80");
    expect(screen.getByText("35% weight")).toBeInTheDocument();
  });

  it("shows unknown components as Unknown, never as a bar or a zero", async () => {
    const { user } = setup();
    await screen.findByLabelText("Match score 63 out of 100");
    await openBreakdown(user);
    expect(screen.getByLabelText("Interest similarity: unknown")).toHaveTextContent("Unknown");
    expect(screen.queryByRole("progressbar", { name: "Interest similarity" })).not.toBeInTheDocument();
    expect(screen.getByText(/25% weight · unknown/)).toBeInTheDocument();
  });

  it("identifies the score as a heuristic, never as an ML prediction", async () => {
    const { user } = setup();
    await screen.findByLabelText("Match score 63 out of 100");
    expect(screen.getByText("Heuristic v1")).toBeInTheDocument();
    await openBreakdown(user);
    expect(screen.getByText(/Rule-based score, not an ML prediction and not a probability/)).toBeInTheDocument();
    expect(screen.queryByText("ML prediction", { exact: true })).not.toBeInTheDocument();
  });

  it("shows why it matches and what to learn first up front, and keeps warnings and evidence behind the breakdown", async () => {
    const { user } = setup();
    await screen.findByLabelText("Match score 63 out of 100");
    expect(screen.getByText("Why it matches")).toBeInTheDocument();
    expect(screen.getByText(/self-reported proficiency is 0.90/)).toBeInTheDocument();
    expect(screen.getByText("You may want to learn this first")).toBeInTheDocument();
    expect(screen.getByText("fastapi")).toBeInTheDocument();
    const toggle = screen.getByRole("button", { name: /Score breakdown/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText(/keyword detection and may be incomplete/)).not.toBeInTheDocument();
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText(/keyword detection and may be incomplete/)).toBeInTheDocument();
    expect(screen.getByText(/Unknown: scored as a conservative placeholder, never as a match/)).toBeInTheDocument();
  });

  it("says in plain words when the score is low or when there is not enough information", async () => {
    setup({ match: () => ok(issueMatch({ match: { final_score: 20 } })) });
    expect(await screen.findByText("Not a close match with your skills yet.")).toBeInTheDocument();
  });

  it("warns when the score is low-confidence or the user has no profile", async () => {
    setup({
      match: () => ok(issueMatch({
        ranking_reliability: { status: "insufficient", reasons: ["You have no profile; create one with skills and interests for personalised scoring."] },
        match: { insufficient_information: true },
      })),
    });
    expect(await screen.findByText("Low confidence")).toBeInTheDocument();
    expect(screen.getByText(/You have no profile/)).toBeInTheDocument();
  });

  it("handles missing recommendation data (no components)", async () => {
    setup({ match: () => ok(issueMatch({ match: { components: {}, reasons: [], missing_skills: [], warnings: [] } })) });
    expect(await screen.findByText("No score breakdown is available for this issue.")).toBeInTheDocument();
    expect(screen.queryByLabelText(/Match score/)).not.toBeInTheDocument();
  });

  it("lists no reasons honestly when there are none", async () => {
    setup({ match: () => ok(issueMatch({ match: { reasons: [], missing_skills: [], warnings: [] } })) });
    await screen.findByLabelText("Match score 63 out of 100");
    expect(screen.getByText("No strong match signals were found.")).toBeInTheDocument();
    expect(screen.queryByText("You may want to learn this first")).not.toBeInTheDocument();
  });
});

describe("authentication and errors", () => {
  it("asks the user to sign in and requests no data when no token is configured", async () => {
    const { user, sent } = setup({ status: () => ok(status({ signedIn: false })) });
    expect(await screen.findByText(/Sign in to see scores/)).toBeInTheDocument();
    await user.click(screen.getAllByRole("button", { name: "Open settings" })[0]);
    expect(sent("openOptions")).toHaveLength(1);
    expect(sent("match")).toHaveLength(0);
    expect(sent("difficulty")).toHaveLength(0);
  });

  it("shows an expired/invalid session with a settings button and no pointless retry", async () => {
    const { user, sent } = setup({ match: () => fail("auth", "Your session is invalid or has expired.", { status: 401 }) });
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/session has ended or is invalid/);
    expect(within(alert).queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
    await user.click(within(alert).getByRole("button", { name: "Open settings" }));
    expect(sent("openOptions")).toHaveLength(1);
  });

  it("says the session ended (not just 'not signed in') when the server ended it", async () => {
    const { sent } = setup({ status: () => ok(status({ signedIn: false, sessionEnded: true })) });
    expect(await screen.findByText(/Your session ended/)).toBeInTheDocument();
    expect(sent("match")).toHaveLength(0);
  });

  it("retries after a network error", async () => {
    let calls = 0;
    const { user, sent } = setup({ match: () => (++calls === 1 ? fail("network", "Could not reach the backend. Is it running and is the URL correct?") : ok(issueMatch())) });
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/Could not reach the backend/);
    await user.click(within(alert).getByRole("button", { name: "Try again" }));
    expect(await screen.findByLabelText("Match score 63 out of 100")).toBeInTheDocument();
    expect(sent("match")).toHaveLength(2);
  });

  it("explains rate limiting with the wait time", async () => {
    setup({ match: () => fail("rate_limited", "Rate limited.", { retryAfterSeconds: 90 }) });
    expect(await screen.findByRole("alert")).toHaveTextContent(/Try again in about 90 s/);
  });

  it("shows nothing rather than guessing when the data is malformed", async () => {
    setup({ match: () => ok({ model: {}, match: "nope" }) });
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/not in the expected format/);
    expect(screen.queryByLabelText(/Match score/)).not.toBeInTheDocument();
  });

  it("copes with a garbage reply from the background worker", async () => {
    setup({ difficulty: () => "garbage" });
    expect(await screen.findByText(/reply in an unexpected format/)).toBeInTheDocument();
    expect(await screen.findByLabelText("Match score 63 out of 100")).toBeInTheDocument(); // other sections are unaffected
  });

  it("copes with an unreachable extension background", async () => {
    setup({ match: () => { throw new Error("Extension context invalidated."); } });
    expect(await screen.findByText(/Could not reach the extension/)).toBeInTheDocument();
  });

  it("reports a failing sign-in check", async () => {
    setup({ status: () => fail("extension", "Could not reach the extension.") });
    expect(await screen.findByRole("alert")).toHaveTextContent(/Could not reach the extension/);
  });
});

describe("difficulty section", () => {
  it("shows the maintainer label hint and an unavailable ML estimate, both clearly identified", async () => {
    setup();
    expect(await screen.findByText(/Level 2 of 5 \(easy\) from the label/)).toBeInTheDocument();
    expect(screen.getByText(/Noisy human label, not measured/)).toBeInTheDocument();
    expect(screen.getByText("Deterministic")).toBeInTheDocument();
    expect(screen.getByText("Experimental", { selector: ".badge" })).toBeInTheDocument();
    expect(screen.getByText(/No ML estimate: no ML model is configured on this backend/)).toBeInTheDocument();
    expect(screen.getByText(/only difficulty indication/)).toBeInTheDocument();
  });

  it("labels an ML prediction experimental, uncalibrated and unverified, with its method", async () => {
    setup({ difficulty: () => ok(difficulty({ ml_prediction: predicted(), fallback: { used: false, description: "ML prediction available; the maintainer-label hint is shown separately." } })) });
    expect(await screen.findByText("easy", { selector: "strong" })).toBeInTheDocument();
    expect(screen.getByText("Experimental", { selector: ".badge" })).toBeInTheDocument();
    expect(screen.getByText(/Experimental: trained on provisional labels and not validated on real data/)).toBeInTheDocument();
    expect(screen.getByText(/uncalibrated and are not probabilities/)).toBeInTheDocument();
    expect(screen.getByText(/ml_difficulty_lr_three_class_abc_s42/)).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "easy (uncalibrated)" })).toHaveAttribute("aria-valuenow", "62");
    const section = screen.getByRole("heading", { name: "Difficulty" }).closest("section")!;
    expect(within(section).queryByText(/Heuristic/)).not.toBeInTheDocument(); // the ML estimate is never labelled a heuristic
    expect(within(section).getByText("Deterministic")).toBeInTheDocument(); // the label hint is labelled deterministic
  });

  it("says plainly when there is no label and no estimate", async () => {
    setup({ difficulty: () => ok(difficulty({ deterministic_signals: { difficulty_label_hint: null }, fallback: { used: true, description: "difficulty is unknown. No value is invented." } })) });
    expect(await screen.findByText("This issue has no difficulty-style label.")).toBeInTheDocument();
    expect(screen.getByText(/No value is invented/)).toBeInTheDocument();
  });

  it("explains unsupported input", async () => {
    setup({ difficulty: () => ok(difficulty({ ml_prediction: { status: "unsupported_input", reason: "not_enough_text", class_scores: {}, calibrated: false } })) });
    expect(await screen.findByText(/No ML estimate: this issue has too little text/)).toBeInTheDocument();
  });

  it("recovers from a difficulty error independently", async () => {
    let n = 0;
    const { user } = setup({ difficulty: () => (++n === 1 ? fail("server", "GitHub is temporarily unavailable.") : ok(difficulty())) });
    await screen.findByLabelText("Match score 63 out of 100");
    const alert = await screen.findByRole("alert");
    await user.click(within(alert).getByRole("button", { name: "Try again" }));
    expect(await screen.findByText(/Level 2 of 5/)).toBeInTheDocument();
  });
});

describe("repository health and readiness (loaded on demand)", () => {
  it("does not request health until the section is opened, and works from the keyboard", async () => {
    const { user, sent } = setup();
    await screen.findByLabelText("Match score 63 out of 100");
    expect(sent("health")).toHaveLength(0);
    const toggle = screen.getByRole("button", { name: /Repository health/ });
    toggle.focus();
    await user.keyboard("{Enter}");
    expect(await screen.findByLabelText("Score 72 out of 100")).toBeInTheDocument();
    expect(sent("health")).toHaveLength(1);
    expect(within(screen.getByRole("region", { name: "Repository health" })).getByText("Heuristic", { selector: ".badge" })).toBeInTheDocument();
    expect(screen.getByText("healthy")).toBeInTheDocument();
    await user.keyboard("{Enter}"); // close, then reopen: no second request
    await user.keyboard(" ");
    expect(sent("health")).toHaveLength(1);
  });

  it("shows freshness, staleness, incomplete collection and unknown signals", async () => {
    const { user } = setup({
      health: () => ok(health({
        report: report("repository_health", { score: 41, label: "moderate", coverage: 0.3 }),
        freshness: { collected_at: "x", age_hours: 100, window_days: 90, head_sha: null, complete: false, stale: true, errors: { comments: "github_unavailable" }, refresh_error: "github_timeout" },
      })),
    });
    await user.click(await screen.findByRole("button", { name: /Repository health/ }));
    expect(await screen.findByText("Limited evidence")).toBeInTheDocument();
    expect(screen.getByText(/Data collected 4 days ago \(window 90 days\)/)).toBeInTheDocument();
    expect(screen.getByText(/This analysis is stale/)).toBeInTheDocument();
    expect(screen.getByText(/Incomplete: comments/)).toBeInTheDocument();
    expect(screen.getByText(/A refresh failed \(github_timeout\)/)).toBeInTheDocument();
    expect(screen.getByLabelText("B signal: unknown")).toBeInTheDocument();
  });

  it("shows a health error with retry", async () => {
    let n = 0;
    const { user } = setup({ health: () => (++n === 1 ? fail("server", "GitHub is temporarily unavailable") : ok(health())) });
    await user.click(await screen.findByRole("button", { name: /Repository health/ }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/temporarily unavailable/);
    await user.click(within(alert).getByRole("button", { name: "Try again" }));
    expect(await screen.findByLabelText("Score 72 out of 100")).toBeInTheDocument();
  });

  it("holds 'Try again' during a rate-limit wait instead of inviting a retry loop, then releases it", async () => {
    let n = 0;
    const { user, sent } = setup({ health: () => (++n === 1 ? fail("rate_limited", "GitHub rate limit reached", { retryAfterSeconds: 2 }) : ok(health())) });
    await user.click(await screen.findByRole("button", { name: /Repository health/ }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/Try again in about 2 s/);
    expect(within(alert).getByRole("button", { name: /Try again in/ })).toBeDisabled();
    expect(sent("health")).toHaveLength(1);
    const retry = await within(alert).findByRole("button", { name: "Try again" }, { timeout: 4000 });
    expect(retry).toBeEnabled();
    await user.click(retry);
    expect(await screen.findByLabelText("Score 72 out of 100")).toBeInTheDocument();
    expect(sent("health")).toHaveLength(2);
  }, 10_000);

  it("shows readiness with linked PRs as safe links and notes that nothing was executed", async () => {
    const { user } = setup({
      readiness: () => ok(readiness({
        linked_pull_requests: { pull_requests: [
          { number: 50, state: "open", html_url: "https://github.com/acme/ml-api/pull/50" },
          { number: 51, state: "closed_unmerged", html_url: "javascript:alert(1)" },
        ] },
        upstream_errors: { linked_pull_requests: "github_unavailable" },
      })),
    });
    await user.click(await screen.findByRole("button", { name: /Contribution readiness/ }));
    expect(await screen.findByText("Detected files were not executed.")).toBeInTheDocument();
    const link = screen.getByRole("link", { name: "#50" });
    expect(link).toHaveAttribute("href", "https://github.com/acme/ml-api/pull/50");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(screen.queryByRole("link", { name: "#51" })).not.toBeInTheDocument(); // unsafe URL is text, not a link
    expect(screen.getByText(/#51/)).toBeInTheDocument();
    expect(screen.getByText(/Some checks failed: linked pull requests \(github_unavailable\)/)).toBeInTheDocument();
  });

  it("says linked PRs were not checked rather than implying there are none", async () => {
    const { user } = setup({ readiness: () => ok(readiness({ linked_pull_requests: null })) });
    await user.click(await screen.findByRole("button", { name: /Contribution readiness/ }));
    expect(await screen.findByText("Linked pull requests have not been checked.")).toBeInTheDocument();
  });
});

const openFeedback = async (user: ReturnType<typeof userEvent.setup>) => user.click(await screen.findByRole("button", { name: /Was this useful\?/ }));

describe("actions", () => {
  it("saves an issue and allows removing it", async () => {
    const { user, sent } = setup();
    await user.click(await screen.findByRole("button", { name: "Save issue" }));
    expect(await screen.findByText("Saved to your issues.")).toBeInTheDocument();
    expect(sent("save")).toEqual([{ type: "save", ref: ISSUE }]);
    await user.click(screen.getByRole("button", { name: "Unsave" }));
    expect(sent("unsave")).toEqual([{ type: "unsave", savedId: 11 }]);
    expect(await screen.findByRole("button", { name: "Save issue" })).toBeInTheDocument();
    expect(screen.getByText("Removed from your saved issues.")).toBeInTheDocument();
  });

  it("reports an already-saved issue", async () => {
    const { user } = setup({ save: () => ok(savedIssue({ created: false })) });
    await user.click(await screen.findByRole("button", { name: "Save issue" }));
    expect(await screen.findByText("Already in your saved issues.")).toBeInTheDocument();
  });

  it("prevents double submission while saving", async () => {
    let release!: (v: unknown) => void;
    const { user, sent } = setup({ save: () => new Promise((r) => (release = r)) });
    const button = await screen.findByRole("button", { name: "Save issue" });
    await user.click(button);
    expect(await screen.findByRole("button", { name: "Saving…" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Saving…" }));
    release(ok(savedIssue()));
    await screen.findByText("Saved to your issues.");
    expect(sent("save")).toHaveLength(1);
  });

  it("shows save failures, with a settings shortcut for authentication problems", async () => {
    const { user, sent } = setup({ save: () => fail("auth", "Your session is invalid or has expired.") });
    await user.click(await screen.findByRole("button", { name: "Save issue" }));
    const alerts = await screen.findAllByRole("alert");
    expect(alerts.some((a) => /session has ended or is invalid/.test(a.textContent ?? ""))).toBe(true);
    const settings = alerts.flatMap((a) => within(a).queryAllByRole("button", { name: "Open settings" }))[0];
    await user.click(settings);
    expect(sent("openOptions").length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Save issue" })).toBeEnabled(); // can try again
  });

  it("submits relevance and difficulty feedback and confirms it", async () => {
    const { user, sent } = setup();
    await openFeedback(user);
    await user.click(await screen.findByRole("button", { name: "Relevant" }));
    await user.click(screen.getByRole("button", { name: "Too easy" }));
    expect(sent("feedback")).toEqual([
      { type: "feedback", ref: ISSUE, feedbackType: "relevance", value: "relevant" },
      { type: "feedback", ref: ISSUE, feedbackType: "difficulty", value: "too_easy" },
    ]);
    expect(screen.getByRole("button", { name: "Relevant" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getAllByText("Thanks, your feedback was recorded.")).toHaveLength(2);
  });

  it("tells the user when the same answer was already recorded", async () => {
    const { user } = setup({ feedback: () => ok(feedbackResult({ duplicate: true })) });
    await openFeedback(user);
    await user.click(await screen.findByRole("button", { name: "Not relevant" }));
    expect(await screen.findByText("You had already given this answer.")).toBeInTheDocument();
  });

  it("shows feedback errors", async () => {
    const { user } = setup({ feedback: () => fail("server", "The backend is temporarily unavailable.") });
    await openFeedback(user);
    await user.click(await screen.findByRole("button", { name: "Appropriate" }));
    expect(await screen.findByText("The backend is temporarily unavailable.")).toBeInTheDocument();
    expect(screen.queryByText(/Thanks/)).not.toBeInTheDocument();
  });

  it("offers only actions that work: no placeholder for unbuilt features, and the explanation is the real one", async () => {
    const { sender } = setup();
    await screen.findByLabelText("Match score 63 out of 100");
    for (const name of ["Explain this issue", "Find similar issues", "View learning path"]) {
      expect(screen.queryByText(name)).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name })).not.toBeInTheDocument();
    }
    expect(screen.queryByText(/Not available yet/)).not.toBeInTheDocument();
    expect(sender.mock.calls.every(([m]) => !/explain|similar|learning/i.test(m.type))).toBe(true); // and no such request exists
    // What is offered instead is built from real data: reasons, and skills to learn first.
    expect(screen.getByText("Why it matches")).toBeInTheDocument();
    expect(screen.getByText("You may want to learn this first")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save issue" })).toBeInTheDocument();
  });
});

describe("panel chrome", () => {
  it("is a labelled landmark whose controls have accessible names", async () => {
    const { user } = setup();
    await screen.findByLabelText("Match score 63 out of 100");
    await openBreakdown(user);
    expect(screen.getByRole("complementary", { name: "OpenSource Compass AI" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open OpenSource Compass AI settings" })).toBeInTheDocument();
    expect(screen.getByRole("list", { name: "Score breakdown" })).toBeInTheDocument();
    for (const bar of screen.getAllByRole("progressbar")) {
      expect(bar).toHaveAccessibleName();
      expect(bar).toHaveAttribute("aria-valuemin", "0");
      expect(bar).toHaveAttribute("aria-valuemax", "100");
    }
  });

  it("collapses without discarding loaded data or repeating requests", async () => {
    const { user, sent } = setup();
    await screen.findByLabelText("Match score 63 out of 100");
    await user.click(screen.getByRole("button", { name: "Hide" }));
    expect(screen.getByRole("button", { name: "Show" })).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByLabelText("Match score 63 out of 100")).not.toBeVisible();
    await user.click(screen.getByRole("button", { name: "Show" }));
    expect(screen.getByLabelText("Match score 63 out of 100")).toBeVisible();
    expect(sent("match")).toHaveLength(1);
  });

  it("states when the experimental scoring method is active", async () => {
    setup({ status: () => ok(status({ experimentalScorer: true })) });
    expect(await screen.findByText(/experimental scoring method \(heuristic_v2\)/)).toBeInTheDocument();
  });

  it("never sends anything to the background before the sign-in check passes", async () => {
    const { sent } = setup({ status: never as never });
    await screen.findByText("Checking your sign-in…");
    await waitFor(() => expect(sent("status")).toHaveLength(1));
    expect(sent("match")).toHaveLength(0);
  });
});

describe("saved indicator and save workflow", () => {
  it("shows 'checking' while the saved state loads, and does not let a save race the lookup", async () => {
    let release!: (v: unknown) => void;
    const { user, sent } = setup({ lookupSaved: () => new Promise((r) => (release = r)) });
    expect(await screen.findByText("Checking your saved issues…")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save issue" })).toBeDisabled();
    release(ok(lookup(false)));
    await waitFor(() => expect(screen.getByRole("button", { name: "Save issue" })).toBeEnabled());
    expect(screen.queryByText("Checking your saved issues…")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save issue" }));
    expect(await screen.findByText("Saved to your issues.")).toBeInTheDocument();
    expect(sent("save")).toHaveLength(1);
    expect(sent("lookupSaved")).toHaveLength(1);
  });

  it("shows an issue as already saved when the page opens, with a way to remove it", async () => {
    const { user, sent } = setup({ lookupSaved: () => ok(lookup(true)) });
    expect(await screen.findByText("Already in your saved issues.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save issue" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Unsave" }));
    expect(sent("unsave")).toEqual([{ type: "unsave", savedId: 11 }]);
    expect(await screen.findByRole("button", { name: "Save issue" })).toBeEnabled();
    expect(screen.queryByText("Already in your saved issues.")).not.toBeInTheDocument();
  });

  it("an action by the user wins over a late lookup result", async () => {
    let release!: (v: unknown) => void;
    const { user } = setup({ lookupSaved: () => new Promise((r) => (release = r)), save: () => ok(savedIssue({ created: true })) });
    await screen.findByText("Checking your saved issues…");
    release(ok(lookup(true)));
    await user.click(await screen.findByRole("button", { name: "Unsave" }));
    expect(await screen.findByRole("button", { name: "Save issue" })).toBeInTheDocument();
  });

  it("still allows saving when the saved-state lookup fails, and says the check failed", async () => {
    const { user } = setup({ lookupSaved: () => fail("network", "Could not reach the backend."), save: () => ok(savedIssue()) });
    expect(await screen.findByText(/Could not check whether this issue is already saved/)).toBeInTheDocument();
    await user.click(await screen.findByRole("button", { name: "Save issue" }));
    expect(await screen.findByText("Saved to your issues.")).toBeInTheDocument();
    expect(screen.queryByText(/Could not check whether/)).not.toBeInTheDocument();
  });

  it("reports saving an issue that was already saved elsewhere without claiming a new save", async () => {
    const { user } = setup({ save: () => ok(savedIssue({ created: false })) });
    await user.click(await screen.findByRole("button", { name: "Save issue" }));
    expect(await screen.findByText("Already in your saved issues.")).toBeInTheDocument();
  });

  it("shows a failed removal and keeps the saved state", async () => {
    const { user } = setup({ lookupSaved: () => ok(lookup(true)), unsave: () => fail("server", "Try later.") });
    await user.click(await screen.findByRole("button", { name: "Unsave" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Try later.");
    expect(screen.getByText("Already in your saved issues.")).toBeInTheDocument();
  });
});
