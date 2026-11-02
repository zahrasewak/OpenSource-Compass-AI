import { useCallback, useEffect, useState } from "react";
import { validateBaseUrl } from "../shared/api";
import type { Bridge } from "../content/bridge";
import { errorText, formatWait } from "../content/ui";
import type { ErrorInfo } from "../shared/messages";
import { statusSchema, type Status } from "../shared/messages";
import { meSchema, type Me } from "../shared/schemas";
import { ProfileEditor } from "./ProfileEditor";
import { Discover } from "./Discover";
import { SavedList } from "./SavedList";

/** Everything the page needs from the browser, injected so it can be tested without Chrome. */
export interface OptionsDeps {
  /** Talks to the background worker. This page never sees a token; it only learns whether a session exists. */
  call: Bridge;
  /** Ask Chrome for permission to contact one origin. Resolves true if granted or already held. */
  ensureOrigin(origin: string): Promise<boolean>;
}

type Message = { tone: "ok" | "error" | "info"; text: string } | null;
type Tab = "discover" | "saved" | "profile" | "account";

import { BACKEND_PINNED, DEFAULT_BACKEND_URL } from "../shared/buildinfo";

// Origins the manifest already grants; anything else needs Chrome's optional-permission prompt (development builds only).
const DEFAULT_PERMITTED = new Set(BACKEND_PINNED ? [DEFAULT_BACKEND_URL] : ["http://localhost:8000", "http://127.0.0.1:8000"]);

export function Options({ deps }: { deps: OptionsDeps }) {
  const { call } = deps;
  const [status, setStatus] = useState<Status | null>(null);
  const [loadError, setLoadError] = useState<ErrorInfo | null>(null);
  // null until the first status arrives: then a signed-in person lands on Find issues, anyone else on Account. After that only clicks change it.
  const [tab, setTab] = useState<Tab | null>(null);

  const refresh = useCallback(async () => {
    const r = await call({ type: "auth.state" }, statusSchema);
    if (r.ok) {
      setStatus(r.data);
      setLoadError(null);
      setTab((cur) => cur ?? (r.data.signedIn ? "discover" : "account"));
    } else setLoadError(r.error);
    return r.ok ? r.data : null;
  }, [call]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  if (loadError) return <main><h1>OpenSource Compass AI</h1><p role="alert">{errorText(loadError)}</p></main>;
  if (!status) return <p role="status">Loading settings…</p>;

  const tabs: { id: Tab; label: string; needsSession: boolean }[] = [
    { id: "discover", label: "Find issues", needsSession: true },
    { id: "saved", label: "Saved issues", needsSession: true },
    { id: "profile", label: "Profile", needsSession: true },
    { id: "account", label: "Account", needsSession: false },
  ];
  const wanted: Tab = tab ?? "account";
  const active: Tab = wanted !== "account" && !status.signedIn ? "account" : wanted;

  return (
    <main>
      <header className="brandbar">
        <CompassMark />
        <div>
          <h1>OpenSource Compass AI</h1>
          <p className="tagline">Let's find your next contribution.</p>
        </div>
      </header>

      <div role="tablist" aria-label="Settings sections" className="tabs">
        {tabs.map((t) => (
          <button
            key={t.id} type="button" role="tab" id={`tab-${t.id}`} aria-selected={active === t.id} aria-controls={`panel-${t.id}`}
            disabled={t.needsSession && !status.signedIn} onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>

      <div role="tabpanel" id={`panel-${active}`} aria-labelledby={`tab-${active}`}>
        {active === "discover" && <Discover call={call} onProfile={() => setTab("profile")} onAccount={() => setTab("account")} />}
        {active === "saved" && <SavedList call={call} onDiscover={() => setTab("discover")} />}
        {active === "profile" && <ProfileEditor call={call} />}
        {active === "account" && <Account deps={deps} status={status} refresh={refresh} onDiscover={() => setTab("discover")} />}
      </div>

      <h2>What this extension sends</h2>
      <p className="help">
        On a GitHub issue it asks your backend about that one issue (owner, repository, issue number); nothing is read from GitHub pages
        except the URL. It only runs on github.com and only contacts the backend you enter under Account. Saving an issue, editing your
        profile or sending feedback stores a record on your backend under your account. No browsing history or other sites are accessed.
      </p>
    </main>
  );
}

/** A small compass: a ring and a needle. Decorative, so hidden from assistive technology. */
function CompassMark() {
  return (
    <svg width="32" height="32" viewBox="0 0 32 32" fill="none" aria-hidden="true">
      <circle cx="16" cy="16" r="13" stroke="currentColor" strokeWidth="2" />
      <path d="M21.5 10.5 18 18l-7.5 3.5L14 14l7.5-3.5Z" fill="currentColor" />
    </svg>
  );
}

// ---------------------------------------------------------------------------------------------- account

function Account({ deps, status, refresh, onDiscover }: { deps: OptionsDeps; status: Status; refresh: () => Promise<Status | null>; onDiscover: () => void }) {
  const { call } = deps;
  const [baseUrl, setBaseUrl] = useState(status.backendUrl);
  const [code, setCode] = useState("");
  const [keep, setKeep] = useState(status.keepSignedIn);
  const [experimental, setExperimental] = useState(status.experimentalScorer);
  const [message, setMessage] = useState<Message>(null);
  const [busy, setBusy] = useState(false);
  const [me, setMe] = useState<Me | null>(null);
  const [confirmAll, setConfirmAll] = useState(false);

  useEffect(() => {
    if (!status.signedIn) return setMe(null);
    let cancelled = false;
    void call({ type: "auth.me" }, meSchema).then((r) => {
      if (!cancelled && r.ok) setMe(r.data);
    });
    return () => {
      cancelled = true;
    };
  }, [call, status.signedIn]);

  /** Validate the origin and obtain Chrome's permission for it while the click's user activation is still valid. */
  async function allowedOrigin(): Promise<string | null> {
    let origin: string;
    try {
      origin = validateBaseUrl(baseUrl);
    } catch (e) {
      setMessage({ tone: "error", text: e instanceof Error ? e.message : "Invalid backend URL." });
      return null;
    }
    if (!DEFAULT_PERMITTED.has(origin) && !(await deps.ensureOrigin(origin))) {
      setMessage({ tone: "error", text: `Chrome permission to contact ${origin} was not granted, so the extension cannot reach that backend.` });
      return null;
    }
    return origin;
  }

  async function saveSettings(): Promise<boolean> {
    const origin = await allowedOrigin();
    if (!origin) return false;
    const r = await call({ type: "settings.save", baseUrl: origin, useExperimentalScorer: experimental }, statusSchema);
    if (!r.ok) {
      setMessage({ tone: "error", text: errorText(r.error) });
      return false;
    }
    setBaseUrl(origin);
    return true;
  }

  async function onSave() {
    setMessage(null);
    setBusy(true);
    const changedOrigin = status.signedIn && baseUrl.trim().replace(/\/$/, "") !== status.backendUrl;
    if (await saveSettings()) {
      await refresh();
      setMessage({
        tone: "ok",
        text: changedOrigin ? "Settings saved. You were signed out because the backend changed." : "Settings saved.",
      });
    }
    setBusy(false);
  }

  async function onConnect() {
    setMessage(null);
    if (!code.trim()) return setMessage({ tone: "error", text: "Enter the pairing code from your backend." });
    setBusy(true);
    if (await saveSettings()) {
      const r = await call({ type: "auth.connect", code, keepSignedIn: keep }, statusSchema);
      setCode(""); // a pairing code is single-use; never keep it in the page
      if (r.ok) {
        await refresh();
        setMessage({ tone: "ok", text: "Signed in." });
      } else {
        // "code invalid/expired/used" and "too many attempts" are about sign-in itself, so they get their own wording.
        const e = r.error;
        const text = e.kind === "auth" ? e.message
          : e.kind === "rate_limited" ? `Too many sign-in attempts. ${e.retryAfterSeconds ? `Try again in about ${formatWait(e.retryAfterSeconds)}.` : "Try again later."}`
          : errorText(e);
        setMessage({ tone: "error", text });
      }
    }
    setBusy(false);
  }

  async function onSignOut() {
    setMessage(null);
    setBusy(true);
    const r = await call({ type: "auth.signOut" }, statusSchema);
    setBusy(false);
    if (r.ok) {
      await refresh();
      setMessage({ tone: "info", text: "Signed out. This session can no longer be used, here or anywhere else." });
    } else setMessage({ tone: "error", text: errorText(r.error) });
  }

  async function onSignOutEverywhere() {
    setMessage(null);
    setConfirmAll(false);
    setBusy(true);
    const r = await call({ type: "auth.signOutAll" }, statusSchema);
    setBusy(false);
    await refresh(); // this browser is always signed out locally, whatever the server answered
    if (r.ok) setMessage({ tone: "info", text: "Signed out everywhere. Every session of your account, on every browser and device, can no longer be used." });
    else setMessage({
      tone: "error",
      text: `This browser is signed out, but the server could not confirm that your other sessions were ended (${r.error.message}). Sign in again and retry if you suspect a lost or stolen browser profile.`,
    });
  }

  return (
    <section aria-labelledby="account-h">
      <h2 id="account-h">Account</h2>

      {status.sessionEnded && !status.signedIn && (
        <p className="message info" role="status">Your session ended (it expired or was revoked). Sign in again with a new pairing code.</p>
      )}

      <form onSubmit={(e) => e.preventDefault()}>
        <label htmlFor="baseUrl">Backend URL</label>
        <input id="baseUrl" type="url" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} readOnly={BACKEND_PINNED} autoComplete="off" spellCheck={false} aria-describedby="baseUrlHelp" />
        <p id="baseUrlHelp" className="help">
          {BACKEND_PINNED
            ? "This build is pinned to this backend and cannot be pointed elsewhere."
            : "Origin only. https is required except for http://localhost during development. Changing it signs you out, because a session is only valid for the server that issued it."}
        </p>

        <label className="check">
          <input type="checkbox" checked={experimental} onChange={(e) => setExperimental(e.target.checked)} />
          Use the experimental scoring method (heuristic_v2: adds repository health and readiness; not evaluated)
        </label>

        {status.signedIn ? (
          <>
            <p data-testid="session-info">
              Signed in{me ? <> as <strong>{me.username}</strong></> : null}.{" "}
              {status.sessionExpiresAt && <>This session ends on {new Date(status.sessionExpiresAt).toLocaleString()} at the latest.</>}
            </p>
            <p><button type="button" className="quiet" onClick={onDiscover}>Find issues →</button></p>
            <div className="buttons">
              <button type="button" className="primary" onClick={onSave} disabled={busy}>Save settings</button>
              <button type="button" onClick={onSignOut} disabled={busy}>Sign out</button>
              <button type="button" onClick={() => setConfirmAll(true)} disabled={busy || confirmAll}>Sign out everywhere</button>
            </div>
            {confirmAll && (
              <div className="message info" role="group" aria-label="Confirm sign out everywhere">
                <p>
                  This ends <strong>every</strong> session of your account on all browsers and devices (use it if a browser profile may have
                  been lost or copied). You will need a new pairing code to sign in again.
                </p>
                <div className="buttons">
                  <button type="button" onClick={onSignOutEverywhere} disabled={busy}>Yes, sign out everywhere</button>
                  <button type="button" onClick={() => setConfirmAll(false)}>Cancel</button>
                </div>
              </div>
            )}
          </>
        ) : (
          <>
            <label htmlFor="code">Pairing code</label>
            <input
              id="code" type="password" value={code} onChange={(e) => setCode(e.target.value)} autoComplete="off" spellCheck={false}
              aria-describedby="codeHelp"
            />
            <p id="codeHelp" className="help">
              Ask whoever runs your backend for a one-time code:{" "}
              <code>python -m app.cli issue-pairing-code --github-id &lt;id&gt; --username &lt;name&gt;</code>. It works once and
              expires in minutes. The extension exchanges it for a short-lived session; the code itself is never stored.
            </p>

            <label className="check">
              <input type="checkbox" checked={keep} onChange={(e) => setKeep(e.target.checked)} />
              <span>
                Keep me signed in after the browser closes.
                <span className="help block">
                  Off (recommended): the session lives in memory only and you pair again after restarting the browser. On: a
                  renewal token is saved <strong>unencrypted</strong> in this browser profile, so anything that can read the
                  profile files can use it until you sign out or the server revokes it.
                </span>
              </span>
            </label>

            <div className="buttons">
              <button type="button" className="primary" onClick={onConnect} disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
              <button type="button" onClick={onSave} disabled={busy}>Save settings</button>
            </div>
          </>
        )}
      </form>

      <div aria-live="polite">
        {message && (
          <p className={`message ${message.tone}`} role={message.tone === "error" ? "alert" : "status"}>
            {message.text}
          </p>
        )}
      </div>
    </section>
  );
}
