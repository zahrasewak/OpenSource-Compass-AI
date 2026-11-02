import { chromeSender, createBridge } from "./bridge";
import { createController } from "./mount";

// Only the top frame, and only once even if the script is injected twice (e.g. after an extension reload).
declare global {
  interface Window {
    __compassAiLoaded?: boolean;
  }
}

if (window.top === window && !window.__compassAiLoaded) {
  window.__compassAiLoaded = true;
  createController(window, document, createBridge(chromeSender));
}
