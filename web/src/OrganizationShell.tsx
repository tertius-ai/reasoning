import React, { lazy, useCallback, useEffect, useState } from "react";
import { LazyView } from "./LazyView";
import { Account, ApiError, Session, aborted, clearSharedReads, errorMessage, request } from "./organization-api";

// The signed-in application (conversations, Markdown/KaTeX, research and administration views) loads only after
// the session check succeeds, so the sign-in page needs just this shell.
const loadApplication = () => import("./OrganizationApp");
const OrganizationApp = lazy(() => loadApplication().then(module => ({ default: module.OrganizationApp })));

const opening = <main className="org-auth"><p role="status">Opening Reasoning…</p></main>;

export function AuthForm({ mode, invitation, onMode, onAuthenticated }: {
  mode: "login" | "join" | "setup"; invitation: string; onMode: (mode: "login" | "join") => void;
  onAuthenticated: (session: Session) => Promise<void>;
}) {
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [token, setToken] = useState(invitation);
  const [organization, setOrganization] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const title = mode === "setup" ? "Create your organization" : mode === "join" ? "Join an organization" : "Sign in to Reasoning";
  const submit = async (event: React.FormEvent) => {
    event.preventDefault(); setBusy(true); setError("");
    // Submitting is deliberate intent: fetch the application while the credentials are checked.
    void loadApplication().catch(() => { /* The application view reports load failures. */ });
    try {
      const session = await request<Session>(mode === "login" ? "/auth/login" : mode === "join" ? "/invitations/accept" : "/setup", {
        method: "POST", body: mode === "login" ? { email, password } : mode === "join" ? { token, name, password }
          : { email, name, password, organization_name: organization, setup_token: token },
      });
      await onAuthenticated(session);
    } catch (cause) { setError(errorMessage(cause)); }
    finally { setPassword(""); setBusy(false); }
  };
  return <main className="org-auth"><div className="org-auth-card">
    <div className="org-wordmark"><img src="/reasoning.svg" alt="" />Reasoning</div>
    <h1>{title}</h1>
    <form onSubmit={submit}>
      {mode !== "login" && <label>{mode === "setup" ? "Setup credential" : "Invitation token"}<input name="token" type="password"
        required value={token} autoComplete="off" onChange={(event) => setToken(event.target.value)} /></label>}
      {mode !== "join" && <label>Email<input name="email" type="email" required autoComplete="username" value={email}
        onChange={(event) => setEmail(event.target.value)} autoFocus /></label>}
      {mode !== "login" && <label>Your name<input name="name" required maxLength={200} value={name} autoComplete="name"
        onChange={(event) => setName(event.target.value)} /></label>}
      <label>Password<input name="password" type="password" required minLength={mode === "login" ? undefined : 12} maxLength={1024}
        autoComplete={mode === "login" ? "current-password" : "new-password"} value={password}
        onChange={(event) => setPassword(event.target.value)} /></label>
      {mode === "setup" && <label>Organization name<input name="organization" required maxLength={200} value={organization}
        onChange={(event) => setOrganization(event.target.value)} /></label>}
      {error && <p role="alert" className="org-error">{error}</p>}
      <button className="org-primary" disabled={busy}>{busy ? "Working…" : mode === "login" ? "Sign in" : mode === "join" ? "Accept invitation" : "Create organization"}</button>
    </form>
    {mode === "join" && <p className="org-help">Your invitation determines your email address. Already registered? <button onClick={() => onMode("login")}>Sign in first</button>.</p>}
    {mode === "login" && <div className="org-help"><button onClick={() => onMode("join")}>Have an invitation?</button><p>For account recovery, contact your server administrator.</p></div>}
    {mode === "setup" && <p className="org-help">The host administrator can read the private setup-token file or complete setup with the server CLI.</p>}
    <footer>© Benjamin Antieau and Deven Manam · <a href="/licenses/Reasoning-Apache-2.0.txt">Apache-2.0</a></footer>
  </div></main>;
}

/** Session check and sign-in; mounts the signed-in application for an authenticated account. */
export function OrganizationShell() {
  const [user, setUser] = useState<Account | null>(null);
  const [loading, setLoading] = useState(true);
  const [initialized, setInitialized] = useState(true);
  const [error, setError] = useState("");
  const [invitation, setInvitation] = useState(() => typeof window === "undefined" ? "" : new URLSearchParams(window.location.hash.slice(1)).get("invite") ?? "");
  const [verificationToken, setVerificationToken] = useState(() => typeof window === "undefined" ? "" : new URLSearchParams(window.location.hash.slice(1)).get("verify") ?? "");
  const [mode, setMode] = useState<"login" | "join">(invitation ? "join" : "login");
  const [sessionOrganization, setSessionOrganization] = useState<string | null>(null);

  useEffect(() => {
    if (invitation || verificationToken) window.history.replaceState(null, "", window.location.pathname + window.location.search);
    const controller = new AbortController();
    const failure = (cause: unknown) => {
      if (!controller.signal.aborted && !aborted(cause) && !(cause instanceof ApiError && cause.status === 401)) setError(errorMessage(cause));
    };
    void Promise.all([
      request<{ initialized: boolean }>("/health", { signal: controller.signal })
        .then(health => { if (!controller.signal.aborted) setInitialized(health.initialized); }).catch(failure),
      request<Account>("/auth/me", { signal: controller.signal })
        .then(account => { if (!controller.signal.aborted) { setUser(account); setLoading(false); } }).catch(failure),
    ]).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, []);

  const authenticated = useCallback(async (session: Session) => {
    clearSharedReads();
    setUser(await request<Account>("/auth/me")); setInitialized(true); setError("");
    if (session.organization_id) { setSessionOrganization(session.organization_id); setInvitation(""); }
  }, []);
  // Unmounting the application discards drafts, retained attempts and every other in-memory workspace state.
  const signedOut = useCallback(() => { clearSharedReads(); setUser(null); setSessionOrganization(null); }, []);
  const startupError = error && <div className="org-startup-error" role="alert">{error} <button onClick={() => window.location.reload()}>Reconnect</button></div>;

  if (loading) return opening;
  if (!user) return <><AuthForm key={initialized ? mode : "setup"} mode={!initialized ? "setup" : mode} invitation={invitation} onMode={setMode} onAuthenticated={authenticated} />
    {startupError}</>;
  return <LazyView label="Reasoning" fallback={opening}><OrganizationApp user={user} onAccount={setUser} onAuthenticated={authenticated} onSignedOut={signedOut}
    invitation={invitation} onInvitation={setInvitation} verificationToken={verificationToken} onVerificationToken={setVerificationToken}
    sessionOrganization={sessionOrganization} />{startupError}</LazyView>;
}
