import { act } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createBridge } from "../src/content/bridge";
import { createController, findSidebar, HOST_ID } from "../src/content/mount";
import { defaultHandlers, fakeSender, ok, status } from "./fixtures";

let controller: ReturnType<typeof createController> | null = null;
const go = (path: string) => window.history.pushState({}, "", path);
const host = () => document.getElementById(HOST_ID);
const hosts = () => document.querySelectorAll(`#${HOST_ID}`);

function start(handlers = {}) {
  const sender = fakeSender({ ...defaultHandlers(), status: () => ok(status({ signedIn: false })), ...handlers });
  controller = createController(window, document, createBridge(sender));
  return sender;
}

beforeEach(() => {
  document.body.innerHTML = "";
  go("/");
});
afterEach(() => {
  act(() => controller?.stop());
  controller = null;
  vi.useRealTimers();
});

describe("page detection and mounting", () => {
  it("mounts one panel on an issue page, inside a shadow root", async () => {
    go("/acme/ml-api/issues/7");
    act(() => void start());
    expect(host()).not.toBeNull();
    expect(host()!.dataset.issue).toBe("acme/ml-api#7");
    expect(host()!.shadowRoot).not.toBeNull();
    expect(host()!.shadowRoot!.querySelector("style")!.textContent).toContain(".panel");
    await vi.waitFor(() => expect(host()!.shadowRoot!.textContent).toContain("Sign in to see scores"));
  });

  it.each(["/", "/acme/ml-api", "/acme/ml-api/issues", "/acme/ml-api/issues/new", "/acme/ml-api/pull/7", "/acme/ml-api/issues/abc", "/settings/profile"])(
    "does nothing on %s", (path) => {
      go(path);
      const sender = start();
      expect(host()).toBeNull();
      expect(sender).not.toHaveBeenCalled();
    });

  it("does not mount twice for the same issue", () => {
    go("/acme/ml-api/issues/7");
    act(() => void start());
    act(() => controller!.sync());
    act(() => controller!.sync());
    expect(hosts()).toHaveLength(1);
  });

  it("follows GitHub's in-page navigation between issues and away", async () => {
    go("/acme/ml-api/issues/7");
    act(() => void start());
    go("/acme/ml-api/issues/8");
    act(() => void window.dispatchEvent(new Event("turbo:load")));
    expect(hosts()).toHaveLength(1);
    expect(host()!.dataset.issue).toBe("acme/ml-api#8");
    go("/acme/ml-api/issues");
    act(() => void window.dispatchEvent(new Event("popstate")));
    expect(host()).toBeNull();
    go("/other/repo/issues/1");
    act(() => void window.dispatchEvent(new Event("turbo:render")));
    expect(host()!.dataset.issue).toBe("other/repo#1");
  });

  it("falls back to polling when no navigation event fires", () => {
    vi.useFakeTimers();
    go("/acme/ml-api/issues/7");
    act(() => void start());
    go("/acme/ml-api/issues/9");
    act(() => void vi.advanceTimersByTime(1100));
    expect(host()!.dataset.issue).toBe("acme/ml-api#9");
  });

  it("re-adds the panel if GitHub's page re-render removes it", () => {
    go("/acme/ml-api/issues/7");
    act(() => void start());
    host()!.remove();
    act(() => controller!.sync());
    expect(hosts()).toHaveLength(1);
  });

  it("stop() removes the panel and stops listening", () => {
    go("/acme/ml-api/issues/7");
    act(() => void start());
    act(() => controller!.stop());
    expect(host()).toBeNull();
    go("/acme/ml-api/issues/8");
    act(() => void window.dispatchEvent(new Event("turbo:load")));
    expect(host()).toBeNull();
  });

  it("touches nothing of GitHub's own content", () => {
    document.body.innerHTML = '<main id="gh"><p>GitHub content</p></main>';
    go("/acme/ml-api/issues/7");
    act(() => void start());
    expect(document.getElementById("gh")!.outerHTML).toBe('<main id="gh"><p>GitHub content</p></main>');
  });
});

describe("placement", () => {
  it("floats at the bottom-right when no sidebar is found", async () => {
    go("/acme/ml-api/issues/7");
    act(() => void start());
    expect(host()!.parentElement).toBe(document.body);
    await vi.waitFor(() => expect(host()!.shadowRoot!.querySelector(".panel.floating")).not.toBeNull());
  });

  it("moves a floating panel into the sidebar when GitHub renders the sidebar later (seen on the real site after back-navigation)", async () => {
    go("/acme/ml-api/issues/7");
    act(() => void start());
    expect(host()!.parentElement).toBe(document.body);
    await vi.waitFor(() => expect(host()!.shadowRoot!.querySelector(".panel.floating")).not.toBeNull());
    document.body.insertAdjacentHTML("beforeend", '<div id="partial-discussion-sidebar"></div>');
    act(() => controller!.sync());
    expect(hosts()).toHaveLength(1);
    expect(host()!.parentElement).toBe(document.getElementById("partial-discussion-sidebar"));
    await vi.waitFor(() => expect(host()!.shadowRoot!.querySelector(".panel")).not.toBeNull());
    expect(host()!.shadowRoot!.querySelector(".panel.floating")).toBeNull();
  });

  it.each(['<div id="partial-discussion-sidebar"></div>', '<div class="Layout-sidebar"></div>', '<div data-testid="issue-viewer-metadata-pane"></div>'])(
    "is placed at the top of the sidebar when GitHub has one: %s", async (markup) => {
      document.body.innerHTML = `<main>${markup}</main>`;
      go("/acme/ml-api/issues/7");
      act(() => void start());
      const sidebar = findSidebar(document)!;
      expect(sidebar.firstElementChild).toBe(host());
      await vi.waitFor(() => expect(host()!.shadowRoot!.querySelector(".panel")).not.toBeNull());
      expect(host()!.shadowRoot!.querySelector(".panel.floating")).toBeNull();
    });
});
