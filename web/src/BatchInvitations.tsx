// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import React, { useRef, useState } from "react";
import { PlusIcon } from "./ComposerIcons";
import { errorMessage, invitationEmailLabel, organizationPath, request, type Invitation } from "./organization-api";

export function invitationAddresses(text: string) {
  const parts = text.split(/[\s,;]+/).map(value => value.replace(/^"|"$/g, "").trim()).filter(Boolean);
  const addresses = parts.filter(value => /^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$/.test(value)).map(value => value.toLowerCase());
  const invalid = parts.filter(value => !addresses.includes(value.toLowerCase()) && !["email", "emails"].includes(value.toLowerCase()));
  return { addresses: [...new Set(addresses)], invalid, duplicates: addresses.length - new Set(addresses).size };
}

type Result = { email: string; status: "queued" | "retryable" | "failed"; error?: string;
  invitation_id?: string; email_state?: Invitation["email_state"] };

export function invitationProgress(results: Result[], invitations: Invitation[]) {
  const live = new Map(invitations.map(invite => [invite.id, invite]));
  const totals = { waiting: 0, accepted: 0, attention: 0, notQueued: 0, joined: 0 };
  const rows = results.map(row => {
    const invite = row.invitation_id ? live.get(row.invitation_id) : undefined;
    const state = invite?.email_state ?? row.email_state ?? "queued";
    if (row.status !== "queued") {
      totals.notQueued++;
      return { ...row, label: row.status === "retryable" ? "Not queued — daily email limit reached" : `Not queued — ${row.error ?? "request failed"}` };
    }
    if (invite?.accepted_at) totals.joined++;
    if (state === "queued" || state === "sending") totals.waiting++;
    else if (state === "accepted") totals.accepted++;
    else totals.attention++;
    return { ...row, label: (invite?.accepted_at ? "Joined · " : "") + invitationEmailLabel({ email_state: state } as Invitation) };
  });
  return { rows, totals };
}

export function InvitationDeliveryProgress({ results, invitations }: { results?: Result[]; invitations: Invitation[] }) {
  const progress = invitationProgress(results ?? invitations.filter(invite => invite.email_state).map(invite => ({
    email: invite.email, status: "queued", invitation_id: invite.id,
  })), invitations);
  if (!progress.rows.length) return null;
  const { totals } = progress;
  return <div className="org-invitation-progress" role="status" aria-label="Invitation email progress">
    <strong className="org-invitation-progress-heading">{results ? "This batch" : "All invitation emails"}</strong>
    <span><strong>{totals.waiting}</strong> waiting</span><span><strong>{totals.accepted}</strong> provider-accepted</span>
    <span><strong>{totals.attention}</strong> need attention</span><span><strong>{totals.notQueued}</strong> not queued</span>
    {totals.joined > 0 && <span><strong>{totals.joined}</strong> joined</span>}
    <small>Provider acceptance does not confirm inbox delivery. Waiting emails send automatically, up to 10 per minute.</small>
    {totals.attention > 0 && <small>Review recipient status below. Uncertain or rejected emails are not automatically resent.</small>}
  </div>;
}

export function BatchInvitations({ organizationId, owner, mailEnabled, workerRunning = true, invitations = [], onChanged }: {
  organizationId: string; owner: boolean; mailEnabled: boolean; workerRunning?: boolean; invitations?: Invitation[]; onChanged: () => void;
}) {
  const [text, setText] = useState(""), [role, setRole] = useState("member");
  const [results, setResults] = useState<Result[]>([]), [error, setError] = useState(""), [busy, setBusy] = useState(false);
  const [filename, setFilename] = useState(""), [readingFile, setReadingFile] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const [retryAt, setRetryAt] = useState(0), [now, setNow] = useState(Date.now());
  const identity = useRef<string>();
  const submission = useRef<{ emails: string[]; existing: Result[] }>();
  React.useEffect(() => { if (!retryAt) return; const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer); }, [retryAt]);
  const { addresses, invalid, duplicates } = invitationAddresses(text);
  const retrySeconds = Math.max(0, Math.ceil((retryAt - now) / 1000));
  const locked = !!identity.current || busy || readingFile;
  const existing = new Map(invitations.filter(invite => invite.email_state && !invite.accepted_at && !invite.revoked_at &&
    invite.expires_at * 1000 > Date.now() && invite.role === role).map(invite => [invite.email.toLowerCase(), invite]));
  const existingCount = addresses.filter(email => existing.has(email)).length;
  const allQueued = results.length === addresses.length && results.length > 0 && results.every(row => row.status === "queued");
  const canSend = !busy && !readingFile && mailEnabled && !invalid.length && addresses.length > 0 && addresses.length <= 100 && !retrySeconds && !allQueued;
  const send = async () => {
    if (!canSend) return;
    setBusy(true); setError(""); identity.current ??= crypto.randomUUID();
    submission.current ??= { emails: addresses.filter(email => !existing.has(email)),
      existing: addresses.filter(email => existing.has(email)).map(email => ({ email, status: "queued", invitation_id: existing.get(email)!.id })) };
    try {
      const value = submission.current.emails.length ? await request<{ results: Result[]; retry_after_seconds: number | null }>(
        organizationPath(organizationId) + "/invitations/batch", { method: "POST", body: { emails: submission.current.emails, role, client_id: identity.current } })
        : { results: [], retry_after_seconds: null };
      setResults([...submission.current.existing, ...value.results]); setRetryAt(value.retry_after_seconds ? Date.now() + value.retry_after_seconds * 1000 : 0);
      setNow(Date.now()); onChanged();
    } catch (cause) { setError(errorMessage(cause)); } finally { setBusy(false); }
  };
  return <details className="org-batch-invitations"><summary>Batch invitations</summary>
    <div className="org-batch-invitation-body">
      {!mailEnabled && <p className="org-help">Email is not configured. Run <code>make email-setup</code> on the operator computer first.</p>}
      <label>Email addresses
        <textarea rows={4} maxLength={40000} value={text} disabled={locked}
          placeholder="alice@example.org, bob@example.org"
          onChange={event => { setText(event.target.value); setFilename(""); setError(""); }} />
        <small>Paste addresses separated by lines, commas, or spaces, or upload a one-column email CSV. Up to 100 people.</small>
      </label>
      <div className="org-batch-upload">
        <button type="button" className="upload-labelled" disabled={locked} onClick={() => fileInput.current?.click()}><PlusIcon size={13} />{readingFile ? "Reading CSV…" : "Upload CSV"}</button>
        {filename && <small title={filename}>{filename}</small>}
        <input ref={fileInput} hidden type="file" accept=".csv,text/csv,text/plain" aria-label="Upload email CSV" disabled={locked}
          onChange={event => {
            const file = event.target.files?.[0]; event.target.value = "";
            if (!file) return;
            setError("");
            if (file.size > 40000) { setError("Use a CSV smaller than 40 KB."); return; }
            setReadingFile(true);
            void file.text().then(value => { setText(value); setFilename(file.name); })
              .catch(cause => setError(errorMessage(cause))).finally(() => setReadingFile(false));
          }} />
      </div>
      <label>Invitation role<select value={role} disabled={locked} onChange={event => setRole(event.target.value)}>
        <option>member</option><option>viewer</option>{owner && <option>admin</option>}</select></label>
      <p className="org-batch-summary" role="status">{allQueued ? `Batch recorded: ${results.length} invitations. See delivery status below.` :
        `${addresses.length} recipient${addresses.length === 1 ? "" : "s"}${duplicates ? ` · ${duplicates} duplicate${duplicates === 1 ? "" : "s"} removed` : ""}`}</p>
      {!identity.current && existingCount > 0 && <p className="org-help">{existingCount} already invited; these will not be emailed again. Use “New link and email” below to deliberately resend.</p>}
      {mailEnabled && !workerRunning && <p className="org-error" role="alert">Email worker is offline. Invitations stay queued until the operator restores it.</p>}
      {invalid.length > 0 && <p className="org-error" role="alert">Invalid entries: {invalid.join(", ")}</p>}
      {addresses.length > 100 && <p className="org-error" role="alert">Use at most 100 recipients per batch.</p>}
      {addresses.length > 0 && <details className="org-batch-preview"><summary>Preview recipients</summary>
        <ul>{addresses.map(email => <li key={email}>{email} · {role}</li>)}</ul></details>}
      <div className="org-batch-actions">
        <button type="button" className="org-primary" disabled={!canSend}
          onClick={() => void send()}>{busy ? "Queueing…" : allQueued ? "Batch queued" : identity.current ? "Retry same batch" : "Send invitations"}</button>
        {retrySeconds > 0 && <small>Daily limit: retry unqueued recipients in {Math.ceil(retrySeconds / 60)} min. Already queued emails continue automatically.</small>}
        {identity.current && <button type="button" disabled={busy} onClick={() => {
          if (!allQueued && !window.confirm("Some invitations remain unresolved. Keep this batch open to retry safely. Start a different batch anyway?")) return;
          identity.current = undefined; submission.current = undefined; setText(""); setFilename(""); setResults([]); setRetryAt(0); setError("");
        }}>New batch</button>}
      </div>
      {results.length > 0 && <><InvitationDeliveryProgress results={results} invitations={invitations} />
        <details><summary>Recipient status</summary><ul className="org-batch-results">{invitationProgress(results, invitations).rows.map(row =>
          <li key={row.email}>{row.email} · {row.label}</li>)}</ul></details>
        {results.some(row => row.status !== "queued") && <p className="org-error" role="alert">Some recipients were not queued. Retry this same batch when available; it will not resend queued emails.</p>}</>}
      {error && <p className="org-error" role="alert">{error}</p>}
    </div>
  </details>;
}
