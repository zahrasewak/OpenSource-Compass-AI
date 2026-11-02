import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const manifest = JSON.parse(readFileSync(resolve(__dirname, "../manifest.json"), "utf8"));

describe("manifest", () => {
  it("is a valid Manifest V3 definition", () => {
    expect(manifest.manifest_version).toBe(3);
    expect(manifest.name).toBeTruthy();
    expect(manifest.version).toMatch(/^\d+(\.\d+){0,3}$/);
    expect(manifest.background.service_worker).toBe("background.js");
    expect(manifest.options_ui.page).toBe("options.html");
    expect(Object.keys(manifest.icons).sort()).toEqual(["128", "16", "32", "48"]);
  });

  it("requests the minimum permissions", () => {
    expect(manifest.permissions).toEqual(["storage"]);
    for (const risky of ["tabs", "history", "cookies", "webRequest", "webNavigation", "scripting", "activeTab", "downloads", "clipboardRead", "identity", "nativeMessaging"]) {
      expect(manifest.permissions).not.toContain(risky);
    }
    expect(manifest.host_permissions).toEqual(["http://localhost:8000/*", "http://127.0.0.1:8000/*"]);
    expect(manifest.host_permissions.join()).not.toMatch(/<all_urls>|\*:\/\/\*\//);
  });

  it("runs only on github.com, and only as a content script there", () => {
    expect(manifest.content_scripts).toHaveLength(1);
    expect(manifest.content_scripts[0].matches).toEqual(["https://github.com/*"]);
    expect(manifest.content_scripts[0].js).toEqual(["content.js"]);
    expect(manifest.content_scripts[0].all_frames).not.toBe(true);
    expect(JSON.stringify(manifest)).not.toContain("<all_urls>");
  });

  it("limits optional host access to https origins requested one at a time at runtime", () => {
    expect(manifest.optional_host_permissions).toEqual(["https://*/*"]);
  });

  it("has a strict content security policy for extension pages", () => {
    const csp = manifest.content_security_policy.extension_pages;
    expect(csp).toContain("script-src 'self'");
    expect(csp).not.toMatch(/unsafe-eval|unsafe-inline|https?:\/\//);
  });

  it("exposes nothing to web pages and embeds no secrets", () => {
    expect(manifest.web_accessible_resources).toBeUndefined();
    expect(manifest.externally_connectable).toBeUndefined();
    expect(JSON.stringify(manifest)).not.toMatch(/token|secret|api[_-]?key|ghp_/i);
  });
});
