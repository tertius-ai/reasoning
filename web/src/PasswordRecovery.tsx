// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import { useEffect, useRef, useState } from "react";
import { Brand } from "./Brand";
import { ApiError, aborted, errorMessage, request } from "./organization-api";

export function ForgotPassword({ initialEmail = "", onBack }: { initialEmail?: string; onBack: (email: string) => void }) {
  const [email, setEmail] = useState(initialEmail), [enabled, setEnabled] = useState<boolean>();
  const [delayed, setDelayed] = useState(false);
  const [busy, setBusy] = useState(false), [error, setError] = useState(""), [message, setMessage] = useState("");
  const attempt = useRef<{ email: string; client_id: string }>();
  useEffect(() => {
    const controller = new AbortController();
    void request<{ enabled: boolean; worker_running?: boolean }>("/auth/password-reset", { signal: controller.signal })
      .then(value => { setEnabled(value.enabled); setDelayed(value.enabled && value.worker_running === false); })
      .catch(cause => { if (!aborted(cause)) setError(errorMessage(cause)); });
    return () => controller.abort();
  }, []);
  return <main className="org-auth"><div className="org-auth-card"><Brand /><h1>Reset your password</h1>
    <p>Enter your account email. We will send a private link so you can choose a new password.</p>
    {enabled === false && <p role="status">Recovery email is unavailable. Contact your server administrator for help.</p>}
    {delayed && <p role="status">Email delivery is currently delayed. Requests remain queued; contact your administrator if your link does not arrive.</p>}
    {message ? <><p role="status">{message}</p><button type="button" onClick={() => { setMessage(""); attempt.current = undefined; }}>Send another link</button></>
      : <form onSubmit={event => { event.preventDefault(); setBusy(true); setError("");
        const address = email.trim().toLowerCase();
        if (!attempt.current || attempt.current.email !== address) attempt.current = { email: address, client_id: crypto.randomUUID() };
        void request<{ message: string }>("/auth/password-reset", { method: "POST", body: attempt.current })
          .then(value => setMessage(value.message)).catch(cause => setError(errorMessage(cause))).finally(() => setBusy(false));
      }}><label>Email<input name="email" type="email" autoComplete="username" required maxLength={254} value={email}
          onChange={event => setEmail(event.target.value)} autoFocus disabled={busy} /></label>
        <button className="org-primary" disabled={busy || enabled !== true}>{busy ? "Requesting…" : "Send reset link"}</button></form>}
    {error && <p role="alert" className="org-error">{error}</p>}
    <p className="org-help">First visit? Use your invitation to create an account. A reset request does not create one.</p>
    <button type="button" onClick={() => onBack(email)}>Back to sign in</button>
  </div></main>;
}

export function ResetPassword({ token, onDone, onSignIn, onRequestNew }: {
  token: string; onDone: () => void; onSignIn: () => void; onRequestNew: () => void;
}) {
  const [password, setPassword] = useState(""), [confirmation, setConfirmation] = useState("");
  const [busy, setBusy] = useState(false), [error, setError] = useState(""), [done, setDone] = useState(false);
  return <main className="org-auth"><div className="org-auth-card"><Brand /><h1>Choose a new password</h1>
    {done ? <><p role="status">Password changed. Your existing sessions have been signed out.</p>
      <p>If you are joining an organization, reopen your invitation after signing in.</p>
      <button className="org-primary" onClick={onDone}>Sign in</button></>
      : <><p>This private link expires after 30 minutes and works once.</p><form onSubmit={event => {
        event.preventDefault(); setError("");
        if (password !== confirmation) { setError("Passwords do not match."); return; }
        setBusy(true);
        void request("/auth/password-reset/confirm", { method: "POST", body: { token, password } })
          .then(() => setDone(true)).catch(cause => setError(errorMessage(cause)))
          .finally(() => { setBusy(false); setPassword(""); setConfirmation(""); });
      }}><label>New password<input name="password" type="password" autoComplete="new-password" minLength={12} maxLength={1024}
          required value={password} disabled={busy} onChange={event => setPassword(event.target.value)} autoFocus /></label>
        <label>Confirm new password<input name="confirmation" type="password" autoComplete="new-password" minLength={12} maxLength={1024}
          required value={confirmation} disabled={busy} onChange={event => setConfirmation(event.target.value)} /></label>
        <small>At least 12 characters.</small>{error && <p role="alert" className="org-error">{error}</p>}
        <button className="org-primary" disabled={busy}>{busy ? "Changing…" : "Set new password"}</button></form>
        {error && <p>If you just submitted a new password and lost the response, try signing in with that password.</p>}
        <button type="button" disabled={busy} onClick={onSignIn}>Back to sign in</button>{" · "}
        <button type="button" disabled={busy} onClick={onRequestNew}>Request a new recovery link</button></>}
  </div></main>;
}

export function AccountSecurity({ onAuthLost, onChanged }: { onAuthLost: () => void; onChanged: (message: string) => void }) {
  const [current, setCurrent] = useState(""), [password, setPassword] = useState(""), [confirmation, setConfirmation] = useState("");
  const [busy, setBusy] = useState(false), [error, setError] = useState(""), [message, setMessage] = useState("");
  const failed = (cause: unknown) => {
    if (cause instanceof ApiError && cause.status === 401) onAuthLost();
    else setError(errorMessage(cause));
  };
  return <section className="org-account-security" aria-label="Account security"><h3>Password and sessions</h3>
    <form onSubmit={event => {
      event.preventDefault(); setError(""); setMessage("");
      if (password !== confirmation) { setError("Passwords do not match."); return; }
      setBusy(true);
      void request<{ message: string }>("/auth/password", { method: "POST", body: { current_password: current, password } })
        .then(value => onChanged(value.message)).catch(failed)
        .finally(() => { setBusy(false); setCurrent(""); setPassword(""); setConfirmation(""); });
    }}><label>Current password<input type="password" name="current-password" autoComplete="current-password" required disabled={busy}
        maxLength={1024} value={current} onChange={event => setCurrent(event.target.value)} /></label>
      <label>New password<input type="password" name="new-password" autoComplete="new-password" required disabled={busy} minLength={12}
        maxLength={1024} value={password} onChange={event => setPassword(event.target.value)} /></label>
      <label>Confirm new password<input type="password" name="confirm-new-password" autoComplete="new-password" required disabled={busy}
        minLength={12} maxLength={1024} value={confirmation} onChange={event => setConfirmation(event.target.value)} /></label>
      <button disabled={busy}>Change password and sign out</button></form>
    <p>Changing your password signs out every session, including this browser. You will sign in again with your new password.</p>
    <button disabled={busy} onClick={() => {
      if (!window.confirm("Sign out your other browsers and devices? This session stays signed in.")) return;
      setBusy(true); setError(""); setMessage("");
      void request("/auth/sessions/revoke-others", { method: "POST", body: {} })
        .then(() => setMessage("Your other sessions have been signed out.")).catch(failed).finally(() => setBusy(false));
    }}>Sign out other sessions</button>{message && <p role="status">{message}</p>}{error && <p role="alert" className="org-error">{error}</p>}
  </section>;
}
