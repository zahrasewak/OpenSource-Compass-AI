/** Minimal key/value storage interface so session logic can be tested without Chrome. */
export interface StorageLike {
  get(key: string): Promise<unknown>;
  set(key: string, value: unknown): Promise<void>;
  remove(key: string): Promise<void>;
}

function area(a: chrome.storage.StorageArea): StorageLike {
  return {
    async get(key) {
      return (await a.get(key))[key];
    },
    async set(key, value) {
      await a.set({ [key]: value });
    },
    async remove(key) {
      await a.remove(key);
    },
  };
}

/**
 * Persistent on disk (survives browser restarts). Holds non-secret settings and, only if the user opted in, a refresh token.
 *
 * Chrome lets CONTENT SCRIPTS read storage.local by default (only storage.session is restricted). Our content script runs
 * beside GitHub's page and must never be able to reach a credential, so the worker restricts this area to trusted
 * extension contexts (worker, options page) at every start. Verified in Chrome 156 and Edge 156 with
 * e2e/probe-storage-access.mjs: without this call the content script CAN read the persisted refresh token.
 * This is a defence against our own content-script code being compromised or buggy; it does NOT encrypt anything, and
 * anyone who can read the browser profile on disk can still read this area.
 */
export function chromeLocal(): StorageLike {
  const local = chrome.storage.local as chrome.storage.LocalStorageArea & { setAccessLevel?: (o: { accessLevel: string }) => Promise<void> };
  try {
    void local.setAccessLevel?.({ accessLevel: "TRUSTED_CONTEXTS" })?.catch?.(() => undefined);
  } catch {
    /* very old Chrome without setAccessLevel for this area: the content script simply never imports storage code */
  }
  return area(chrome.storage.local);
}

/**
 * In memory for the browser session: cleared when the browser closes and never written to disk. Access tokens live here.
 * Chrome limits this area to trusted extension contexts by default (content scripts cannot read it); we also ask explicitly.
 */
export function chromeSessionStore(): StorageLike {
  const s = chrome.storage.session as chrome.storage.SessionStorageArea;
  try {
    void s.setAccessLevel({ accessLevel: "TRUSTED_CONTEXTS" as chrome.storage.AccessLevel }).catch(() => undefined);
  } catch {
    /* older Chrome: the default is already trusted contexts only */
  }
  return area(s);
}

export function memoryStorage(initial: Record<string, unknown> = {}): StorageLike & { data: Record<string, unknown> } {
  const data = { ...initial };
  return {
    data,
    async get(key) {
      return data[key];
    },
    async set(key, value) {
      data[key] = value;
    },
    async remove(key) {
      delete data[key];
    },
  };
}
