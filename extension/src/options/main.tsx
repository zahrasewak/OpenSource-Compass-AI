import { createRoot } from "react-dom/client";
import { chromeSender, createBridge } from "../content/bridge";
import { Options, type OptionsDeps } from "./Options";
import "./options.css";

const deps: OptionsDeps = {
  // The options page holds no credentials: every action goes through the background worker.
  call: createBridge(chromeSender),
  async ensureOrigin(origin) {
    const request = { origins: [`${origin}/*`] };
    return (await chrome.permissions.contains(request)) || chrome.permissions.request(request);
  },
};

createRoot(document.getElementById("root")!).render(<Options deps={deps} />);
