// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import { useEffect, useState } from "react";
import { Brand } from "./Brand";
import { Session, aborted, errorMessage, request } from "./organization-api";

type InvitationStatus = { state: "pending" | "used" | "expired" | "revoked" | "invalid"; email?: string; existing_account?: boolean };
const explanations = {
  used: "This invitation has already been used. Sign in to your account, or reset your password if you have forgotten it.",
  expired: "This invitation has expired. Ask your inviter for a fresh link.",
  revoked: "This invitation has been replaced or revoked. Use the newest invitation email, or ask your inviter for a fresh link.",
  invalid: "This invitation link is invalid. Check that you copied the complete link, or ask your inviter for a fresh one.",
};
function invitationToken(value: string) {
  try { return new URLSearchParams(new URL(value).hash.slice(1)).get("invite") ?? value.trim(); }
  catch { return value.trim(); }
}
function inspect(token: string, signal?: AbortSignal) {
  if (token.length < 16) return Promise.resolve<InvitationStatus>({ state: "invalid" });
  return request<InvitationStatus>("/invitations/inspect", { method: "POST", body: { token }, signal });
}

export function JoinInvitation({ initialToken, onMode, onAuthenticated }: {
  initialToken: string; onMode: (mode: "login" | "forgot", email?: string) => void; onAuthenticated: (session: Session) => Promise<void>;
}) {
  const [value, setValue] = useState(initialToken), [status, setStatus] = useState<InvitationStatus>();
  const [name, setName] = useState(""), [password, setPassword] = useState(""), [busy, setBusy] = useState(false), [error, setError] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const token = invitationToken(value);
  useEffect(() => {
    if (!initialToken) return;
    const controller = new AbortController(); setBusy(true);
    void inspect(initialToken, controller.signal).then(setStatus).catch(cause => { if (!aborted(cause)) setError(errorMessage(cause)); })
      .finally(() => { if (!controller.signal.aborted) setBusy(false); });
    return () => controller.abort();
  }, [initialToken]);
  const newAccount = status?.state === "pending" && !status.existing_account;
  return <main className="org-auth"><div className="org-auth-card"><Brand /><h1>Join an organization</h1>
    {status && status.state !== "pending" && <p role="status">{explanations[status.state]}</p>}
    {status?.state === "pending" && <p>Invitation for <strong>{status.email}</strong>.</p>}
    {status?.existing_account && <p role="status">You already have an account with this email address. Sign in to accept the invitation, or reset your password.</p>}
    {(!status || newAccount) && <form onSubmit={event => {
      event.preventDefault(); setError("");
      if (newAccount && password !== confirmation) { setError("Passwords do not match."); return; }
      setBusy(true);
      const work = newAccount
        ? request<Session>("/invitations/accept", { method: "POST", body: { token, name, password } }).then(onAuthenticated)
        : inspect(token).then(setStatus);
      void work.catch(async cause => {
        setError(errorMessage(cause));
        if (newAccount) {
          try { const latest = await inspect(token); setStatus(latest); if (latest.state !== "pending") setError(""); }
          catch { /* Keep the actionable original failure. */ }
        }
      }).finally(() => { setBusy(false); setPassword(""); setConfirmation(""); });
    }}>
      {!initialToken && <label>Invitation link<input name="invitation" type="password" autoComplete="off" required value={value} disabled={busy}
        onChange={event => { setValue(event.target.value); setStatus(undefined); }} placeholder="Paste the link you received" /></label>}
      {newAccount && <><label>Email<input name="email" type="email" autoComplete="username" readOnly value={status?.email ?? ""} /></label>
        <label>Your name<input name="name" autoComplete="name" required maxLength={200} value={name} disabled={busy}
        onChange={event => setName(event.target.value)} autoFocus /></label>
        <label>Choose a password<input name="password" type="password" autoComplete="new-password" required minLength={12} maxLength={1024}
          value={password} disabled={busy} onChange={event => setPassword(event.target.value)} /></label>
        <label>Confirm password<input name="confirmation" type="password" autoComplete="new-password" required minLength={12} maxLength={1024}
          value={confirmation} disabled={busy} onChange={event => setConfirmation(event.target.value)} /></label><small>At least 12 characters.</small></>}
      <button className="org-primary" disabled={busy || !token}>{busy ? "Checking…" : newAccount ? "Accept invitation" : "Check invitation"}</button>
    </form>}
    {error && <p role="alert" className="org-error">{error}</p>}
    <p className="org-help"><button onClick={() => onMode("login", status?.email)}>Sign in</button>{" · "}
      <button onClick={() => onMode("forgot", status?.email)}>Forgot password?</button></p>
  </div></main>;
}

export function InvitationBanner({ token, email, onAccepted, onDismiss }: {
  token: string; email: string; onAccepted: (session: Session) => Promise<void>; onDismiss: () => void;
}) {
  const [status, setStatus] = useState<InvitationStatus>(), [busy, setBusy] = useState(false), [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    void inspect(token, controller.signal).then(setStatus).catch(cause => { if (!aborted(cause)) setError(errorMessage(cause)); });
    return () => controller.abort();
  }, [token]);
  return <div className="org-banner">
    {!status ? "Checking invitation…" : status.state !== "pending" ? explanations[status.state]
      : status.email?.toLowerCase() !== email.toLowerCase() ? `This invitation is for ${status.email}. Sign out and sign in with that email address.`
      : <>Invitation for {status.email}. <button disabled={busy} onClick={() => {
        setBusy(true); setError("");
        void request<Session>("/invitations/accept", { method: "POST", body: { token } }).then(onAccepted)
          .catch(async cause => { setError(errorMessage(cause)); try { setStatus(await inspect(token)); } catch { /* Keep original error. */ } })
          .finally(() => setBusy(false));
      }}>Accept invitation</button></>}
    <button onClick={onDismiss}>Dismiss</button>{error && <span role="alert">{error}</span>}
  </div>;
}
