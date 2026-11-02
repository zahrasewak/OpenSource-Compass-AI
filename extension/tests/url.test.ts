import { describe, expect, it } from "vitest";
import { isValidIssueRef, parseIssueUrl, safeGithubUrl } from "../src/shared/url";

describe("parseIssueUrl", () => {
  it.each([
    ["https://github.com/pallets/flask/issues/123", { owner: "pallets", repo: "flask", number: 123 }],
    ["https://github.com/pallets/flask/issues/123/", { owner: "pallets", repo: "flask", number: 123 }],
    ["https://github.com/pallets/flask/issues/123?notification_referrer_id=x", { owner: "pallets", repo: "flask", number: 123 }],
    ["https://github.com/pallets/flask/issues/123#issuecomment-99", { owner: "pallets", repo: "flask", number: 123 }],
    ["https://github.com/o.rg-1/re_po.js/issues/1", { owner: "o.rg-1", repo: "re_po.js", number: 1 }],
    ["https://github.com/a/b/issues/2147483647", { owner: "a", repo: "b", number: 2147483647 }],
  ])("recognises %s", (url, expected) => {
    expect(parseIssueUrl(url)).toEqual(expected);
  });

  it.each([
    "https://github.com/pallets/flask/issues",          // list page
    "https://github.com/pallets/flask/issues/",
    "https://github.com/pallets/flask/issues/new",
    "https://github.com/pallets/flask/issues/new/choose",
    "https://github.com/pallets/flask/pull/123",        // pull requests are not issues
    "https://github.com/pallets/flask/issues/abc",
    "https://github.com/pallets/flask/issues/0",
    "https://github.com/pallets/flask/issues/-5",
    "https://github.com/pallets/flask/issues/007",
    "https://github.com/pallets/flask/issues/1.5",
    "https://github.com/pallets/flask/issues/2147483648",
    "https://github.com/pallets/flask/issues/99999999999999999999",
    "https://github.com/pallets/issues/123",            // missing repository
    "https://github.com/issues/123",
    "https://github.com/",
    "https://github.com/../flask/issues/1",
    "https://github.com/%2e%2e/flask/issues/1",
    "https://github.com/pallets/fl%20ask/issues/1",
    "https://github.com/pallets/flask/issues/1/../../x/issues/2",
  ])("rejects %s", (url) => {
    expect(parseIssueUrl(url)).toBeNull();
  });

  it.each([
    "http://github.com/a/b/issues/1",                   // not https
    "https://www.github.com/a/b/issues/1",
    "https://github.com.evil.example/a/b/issues/1",     // look-alike host
    "https://evil.example/github.com/a/b/issues/1",
    "https://github.com:8443/a/b/issues/1",
    "https://user:pw@github.com/a/b/issues/1",
    "https://gitlab.com/a/b/issues/1",
    "javascript:alert(1)",
    "not a url",
    "",
  ])("rejects non-GitHub or unsafe origin %s", (url) => {
    expect(parseIssueUrl(url)).toBeNull();
  });
});

describe("isValidIssueRef", () => {
  it("validates names and numbers", () => {
    expect(isValidIssueRef({ owner: "a", repo: "b", number: 1 })).toBe(true);
    for (const bad of [
      { owner: "", repo: "b", number: 1 }, { owner: "a", repo: "..", number: 1 }, { owner: "a b", repo: "b", number: 1 },
      { owner: "a", repo: "b", number: 0 }, { owner: "a", repo: "b", number: 1.5 }, { owner: "a", repo: "b", number: 2 ** 31 },
      { owner: "a".repeat(101), repo: "b", number: 1 },
    ]) expect(isValidIssueRef(bad)).toBe(false);
  });
});

describe("safeGithubUrl", () => {
  it("only passes https github.com links", () => {
    expect(safeGithubUrl("https://github.com/a/b/pull/1")).toBe("https://github.com/a/b/pull/1");
    for (const bad of ["javascript:alert(1)", "http://github.com/a", "https://evil.example/x", "data:text/html,x", "", null, undefined, "github.com/a"]) {
      expect(safeGithubUrl(bad as string)).toBeNull();
    }
  });
});
