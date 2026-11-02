import { createApi } from "../shared/api";
import { SessionManager } from "../shared/session";
import { chromeLocal, chromeSessionStore } from "../shared/storage";
import { ResponseCache } from "./cache";
import { handleMessage } from "./handler";

const session = new SessionManager({ local: chromeLocal(), session: chromeSessionStore() });
const cache = new ResponseCache();
const api = createApi({
  getConfig: () => session.getConfig(),
  onUnauthorized: (usedToken) => session.onUnauthorized(usedToken),
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  void handleMessage(message, { id: sender.id, url: sender.url, tab: sender.tab ? { url: sender.tab.url } : undefined }, {
    api,
    session,
    cache,
    extensionId: chrome.runtime.id,
    openOptions: () => void chrome.runtime.openOptionsPage(),
  }).then(sendResponse);
  return true; // the response is asynchronous
});

// Clicking the toolbar icon opens the settings page (there is no popup).
chrome.action.onClicked.addListener(() => void chrome.runtime.openOptionsPage());

chrome.runtime.onInstalled.addListener((details) => {
  if (details.reason === "install") void chrome.runtime.openOptionsPage();
});
