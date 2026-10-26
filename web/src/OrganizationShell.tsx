import React, { lazy, useCallback, useEffect, useState } from "react";
import { Brand } from "./Brand";
import { JoinInvitation } from "./InvitationJoin";
import { LazyView } from "./LazyView";
import { ForgotPassword, ResetPassword } from "./PasswordRecovery";
import { Account, ApiError, Session, aborted, clearSharedReads, errorMessage, request } from "./organization-api";

// Load the workspace only after authentication or deliberate form submission.
const loadApplication = () => import("./OrganizationApp");
const OrganizationApp = lazy(() => loadApplication().then(module => ({ default: module.OrganizationApp })));
const opening = <main className="org-auth"><p role="status">Opening reasoning…</p></main>;
type AuthMode = "login" | "join" | "forgot";

export function AuthForm({ mode, invitation, initialEmail = "", notice = "", onMode, onAuthenticated }: {
  mode: AuthMode | "setup"; invitation: string; initialEmail?: string; notice?: string;
  onMode: (mode: AuthMode, email?: string) => void; onAuthenticated: (session: Session) => Promise<void>;
}) {
  const [email, setEmail] = useState(initialEmail), [name, setName] = useState(""), [password, setPassword] = useState("");
  const [token, setToken] = useState(invitation), [organization, setOrganization] = useState("");
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  if (mode === "forgot") return <ForgotPassword initialEmail={initialEmail} onBack={email => onMode("login", email)} />;
  if (mode === "join") return <JoinInvitation key={invitation} initialToken={invitation} onMode={onMode} onAuthenticated={onAuthenticated} />;
  const submit = async (event: React.FormEvent) => {
    event.preventDefault(); setBusy(true); setError("");
    void loadApplication().catch(() => { /* The application view reports load failures. */ });
    try {
      const session = await request<Session>(mode === "login" ? "/auth/login" : "/setup", {
        method: "POST", body: mode === "login" ? { email, password }
          : { email, name, password, organization_name: organization, setup_token: token },
      });
      await onAuthenticated(session);
    } catch (cause) { setError(errorMessage(cause)); }
    finally { setPassword(""); setBusy(false); }
  };
  return <main className="org-auth"><div className="org-auth-card"><Brand />
    <h1>{mode === "setup" ? "Create your organization" : "Sign in to reasoning"}</h1>
    {notice && <p role="status">{notice}</p>}
    <form onSubmit={submit}>
      {mode === "setup" && <label>Setup credential<input name="token" type="password" required value={token} autoComplete="off"
        onChange={event => setToken(event.target.value)} /></label>}
      <label>Email<input name="email" type="email" required autoComplete="username" value={email}
        onChange={event => setEmail(event.target.value)} autoFocus /></label>
      {mode === "setup" && <label>Your name<input name="name" required maxLength={200} value={name} autoComplete="name"
        onChange={event => setName(event.target.value)} /></label>}
      <label>Password<input name="password" type="password" required minLength={mode === "login" ? undefined : 12} maxLength={1024}
        autoComplete={mode === "login" ? "current-password" : "new-password"} value={password}
        onChange={event => setPassword(event.target.value)} /></label>
      {mode === "setup" && <label>Organization name<input name="organization" required maxLength={200} value={organization}
        onChange={event => setOrganization(event.target.value)} /></label>}
      {error && <p role="alert" className="org-error">{error}</p>}
      <button className="org-primary" disabled={busy}>{busy ? "Working…" : mode === "login" ? "Sign in" : "Create organization"}</button>
    </form>
    {mode === "login" && <div className="org-help"><button onClick={() => onMode("forgot", email)}>Forgot password?</button>
      <p><button onClick={() => onMode("join")}>Have an invitation?</button></p></div>}
    {mode === "setup" && <p className="org-help">The host administrator can read the private setup-token file or complete setup with the server CLI.</p>}
    <footer>© Benjamin Antieau and Deven Manam · <a href="/licenses/Reasoning-Apache-2.0.txt">Apache-2.0</a></footer>
  </div></main>;
}

/** Session check and sign-in; mounts the workspace for an authenticated account. */
export function OrganizationShell() {
  const [user, setUser] = useState<Account | null>(null), [loading, setLoading] = useState(true), [initialized, setInitialized] = useState(true);
  const [error, setError] = useState("");
  const [invitation, setInvitation] = useState(() => typeof window === "undefined" ? "" : new URLSearchParams(window.location.hash.slice(1)).get("invite") ?? "");
  const [verificationToken, setVerificationToken] = useState(() => typeof window === "undefined" ? "" : new URLSearchParams(window.location.hash.slice(1)).get("verify") ?? "");
  const [resetToken, setResetToken] = useState(() => typeof window === "undefined" ? "" : new URLSearchParams(window.location.hash.slice(1)).get("reset") ?? "");
  const [mode, setMode] = useState<AuthMode>(invitation ? "join" : "login");
  const [authEmail, setAuthEmail] = useState(""), [authNotice, setAuthNotice] = useState("");
  const [linkRevision, setLinkRevision] = useState(0);
  const [sessionOrganization, setSessionOrganization] = useState<string | null>(null);
  useEffect(() => {
    const readLinks = () => {
      const params = new URLSearchParams(window.location.hash.slice(1));
      const invite = params.get("invite"), verify = params.get("verify"), reset = params.get("reset");
      if (invite) { setInvitation(invite); setMode("join"); }
      if (verify) { setVerificationToken(verify); setMode("login"); }
      if (reset) setResetToken(reset);
      if (invite || verify || reset) {
        setLinkRevision(value => value + 1);
        window.history.replaceState(null, "", window.location.pathname + window.location.search);
      }
    };
    readLinks(); window.addEventListener("hashchange", readLinks);
    const controller = new AbortController();
    const failure = (cause: unknown) => {
      if (!controller.signal.aborted && !aborted(cause) && !(cause instanceof ApiError && cause.status === 401)) setError(errorMessage(cause));
    };
    void Promise.all([
      request<{ initialized: boolean }>("/health", { signal: controller.signal })
        .then(health => { if (!controller.signal.aborted) setInitialized(health.initialized); }).catch(failure),
      request<Account>("/auth/me", { signal: controller.signal })
        // A signed-in account opens the workspace at once; slower startup reads such as health never hold it back.
        .then(account => { if (!controller.signal.aborted && !resetToken) { setUser(account); setLoading(false); } }).catch(failure),
    ]).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => { controller.abort(); window.removeEventListener("hashchange", readLinks); };
  }, []);
  const authenticated = useCallback(async (session: Session) => {
    clearSharedReads();
    setUser(await request<Account>("/auth/me")); setInitialized(true); setMode("login"); setError("");
    if (session.organization_id) { setSessionOrganization(session.organization_id); setInvitation(""); }
  }, []);
  const signedOut = useCallback((message = "") => {
    clearSharedReads(); setUser(null); setSessionOrganization(null); setMode("login"); setAuthNotice(message);
  }, []);
  const startupError = error && <div className="org-startup-error" role="alert">{error} <button onClick={() => window.location.reload()}>Reconnect</button></div>;
  if (resetToken) return <ResetPassword key={resetToken + ":" + linkRevision} token={resetToken} onDone={() => {
    clearSharedReads(); setResetToken(""); setUser(null); setMode("login"); setAuthNotice("Password changed. Sign in with your new password.");
  }} onSignIn={() => { clearSharedReads(); setResetToken(""); setUser(null); setMode("login"); setAuthNotice(""); }}
    onRequestNew={() => { setResetToken(""); setMode("forgot"); setUser(null); }} />;
  if (loading) return opening;
  if (!user || mode === "forgot") return <><AuthForm key={initialized ? mode + ":" + linkRevision : "setup"} mode={!initialized ? "setup" : mode} invitation={invitation}
    initialEmail={authEmail} notice={authNotice} onMode={(next, email) => { setMode(next); setAuthNotice(""); if (email) setAuthEmail(email); }} onAuthenticated={authenticated} />
    {startupError}</>;
  return <LazyView label="reasoning" fallback={opening}><OrganizationApp user={user} onAccount={setUser} onAuthenticated={authenticated} onSignedOut={signedOut}
    invitation={invitation} onInvitation={setInvitation} verificationToken={verificationToken} onVerificationToken={setVerificationToken}
    sessionOrganization={sessionOrganization} />{startupError}</LazyView>;
}
