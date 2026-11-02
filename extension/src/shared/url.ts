export interface IssueRef {
  owner: string;
  repo: string;
  number: number;
}

const NAME = /^[A-Za-z0-9_.-]{1,100}$/;
const MAX_ISSUE_NUMBER = 2_147_483_647; // matches the backend's accepted range

export function isValidName(value: string): boolean {
  return NAME.test(value) && value !== "." && value !== "..";
}

export function isValidIssueRef(ref: IssueRef): boolean {
  return (
    isValidName(ref.owner) &&
    isValidName(ref.repo) &&
    Number.isInteger(ref.number) &&
    ref.number >= 1 &&
    ref.number <= MAX_ISSUE_NUMBER
  );
}

/**
 * Identify a GitHub issue from a page URL. Only https://github.com/{owner}/{repo}/issues/{number} qualifies
 * (query strings, fragments and further path segments such as /issues/12#issuecomment-3 are ignored).
 * Pull requests, issue lists, /issues/new, other hosts (including look-alikes) and malformed numbers return null.
 */
export function parseIssueUrl(href: string): IssueRef | null {
  let url: URL;
  try {
    url = new URL(href);
  } catch {
    return null;
  }
  if (url.protocol !== "https:" || url.hostname !== "github.com" || url.port !== "" || url.username || url.password) {
    return null;
  }
  const parts = url.pathname.split("/");
  // ["", owner, repo, "issues", number, ...rest]
  if (parts.length < 5 || parts[0] !== "" || parts[3] !== "issues") return null;
  const [, owner, repo, , rawNumber] = parts;
  if (!/^[1-9][0-9]{0,9}$/.test(rawNumber)) return null;
  const ref = { owner, repo, number: Number(rawNumber) };
  return isValidIssueRef(ref) ? ref : null;
}

export function issueKey(ref: IssueRef): string {
  return `${ref.owner}/${ref.repo}#${ref.number}`;
}

/** Links shown in the panel come from the backend; only github.com https links are ever rendered as links. */
export function safeGithubUrl(value: string | null | undefined): string | null {
  if (!value) return null;
  try {
    const u = new URL(value);
    return u.protocol === "https:" && u.hostname === "github.com" ? u.toString() : null;
  } catch {
    return null;
  }
}
