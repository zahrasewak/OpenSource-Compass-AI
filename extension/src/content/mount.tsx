import { createRoot, type Root } from "react-dom/client";
import { Panel } from "./Panel";
import type { Bridge } from "./bridge";
import css from "./panel.css?inline";
import { issueKey, parseIssueUrl, type IssueRef } from "../shared/url";

export const HOST_ID = "compass-ai-host";

/** GitHub's sidebar markup changes; try known places and fall back to a floating panel rather than guess. */
const SIDEBAR_SELECTORS = ['[data-testid="issue-viewer-metadata-pane"]', "#partial-discussion-sidebar", ".Layout-sidebar"];

export function findSidebar(doc: Document): Element | null {
  for (const selector of SIDEBAR_SELECTORS) {
    const el = doc.querySelector(selector);
    if (el) return el;
  }
  return null;
}

export interface MountedPanel {
  host: HTMLElement;
  key: string;
  floating: boolean;
  unmount(): void;
}

export function mountPanel(doc: Document, issue: IssueRef, bridge: Bridge): MountedPanel {
  const host = doc.createElement("div");
  host.id = HOST_ID;
  host.dataset.issue = issueKey(issue);
  const sidebar = findSidebar(doc);
  const floating = sidebar === null;
  if (sidebar) sidebar.prepend(host);
  else doc.body.append(host);

  const shadow = host.attachShadow({ mode: "open" });
  const style = doc.createElement("style");
  style.textContent = css;
  const container = doc.createElement("div");
  shadow.append(style, container);

  const root: Root = createRoot(container);
  root.render(<Panel bridge={bridge} issue={issue} floating={floating} />);
  return {
    host,
    key: issueKey(issue),
    floating,
    unmount() {
      root.unmount();
      host.remove();
    },
  };
}

/**
 * Keeps exactly one panel on the page that matches the current URL. GitHub navigates without full page loads
 * (Turbo), so this runs on its navigation events, on history changes and on a cheap 1 s check as a fallback.
 * It only reads location.href; it never inspects or changes GitHub's own content except to add the panel host.
 */
export function createController(win: Window, doc: Document, bridge: Bridge) {
  let mounted: MountedPanel | null = null;

  function sync() {
    const issue = parseIssueUrl(win.location.href);
    const wanted = issue ? issueKey(issue) : null;
    // GitHub renders its sidebar after the page body on some navigations. A panel that had to float moves into the sidebar
    // once it exists (answers are cached by the background worker, so this does not repeat requests).
    const sidebarAppeared = mounted !== null && mounted.floating && findSidebar(doc) !== null;
    if (mounted && (mounted.key !== wanted || !mounted.host.isConnected || sidebarAppeared)) {
      mounted.unmount();
      mounted = null;
    }
    if (!mounted && issue) mounted = mountPanel(doc, issue, bridge);
  }

  const events = ["turbo:load", "turbo:render", "pjax:end", "popstate"];
  events.forEach((e) => win.addEventListener(e, sync));
  const timer = win.setInterval(sync, 1000);
  sync();

  return {
    sync,
    isMounted: () => mounted !== null,
    stop() {
      events.forEach((e) => win.removeEventListener(e, sync));
      win.clearInterval(timer);
      mounted?.unmount();
      mounted = null;
    },
  };
}
