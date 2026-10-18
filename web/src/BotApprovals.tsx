// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import React, { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { aborted, ApiError, errorMessage, organizationPath, Presence, request } from "./organization-api";
import { PollBackoff, schedulePoll } from "./polling";

type BaseApproval = { id: string; bot_id: string; run_id: string; requester_name: string; topic_name: string;
  created_at: number; delivery_interrupted?: boolean };
export type BotApproval = BaseApproval & (
  { kind: "execution"; command_json: string; command_sha256: string } |
  { kind: "issue"; repository: string; title: string; body: string; document_sha256: string; approval_role?: "owner" | "requester" } |
  { kind: "compute"; image_digest: string; request_sha256: string;
    parameters: { image_id: string; language: string; program: string; resources: Record<string, number> } }
);

/**
 * Poll the signed-in owner's pending bot approvals until `signal` aborts: every three seconds while requests are
 * pending or the list changes, then slowing to fifteen seconds; hidden tabs pause and refresh when visible again.
 */
export function pollBotApprovals(organizationId: string, signal: AbortSignal,
  onRows: (rows: BotApproval[]) => void, onError: (cause: unknown) => void): void {
  const backoff = new PollBackoff(3000, 15000); let previous = "";
  const poll = async () => {
    let active = false;
    try {
      const value = await request<BotApproval[]>(organizationPath(organizationId) + "/my-bots/pending-approvals", { signal });
      if (signal.aborted) return;
      const ids = value.map(row => row.id).join(" ");
      active = value.length > 0 || ids !== previous; previous = ids;
      onRows(value);
    } catch (cause) { if (!aborted(cause)) onError(cause); }
    if (!signal.aborted) schedulePoll(poll, backoff.next(active), signal);
  };
  void poll();
}

export function useBotApprovals(organizationId: string, open: boolean, onAuthLost: () => void) {
  const [rows, setRows] = useState<BotApproval[]>([]), [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => { setRows([]); setError(""); }, [organizationId]);
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    pollBotApprovals(organizationId, controller.signal, value => { setRows(value); setError(""); }, cause => {
      if (cause instanceof ApiError && cause.status === 401) onAuthLost(); else setError(errorMessage(cause));
    });
    return () => controller.abort();
  }, [organizationId, open, revision, onAuthLost]);
  return { rows, error, refresh: () => setRevision(value => value + 1),
    remove: (id: string) => setRows(current => current.filter(row => row.id !== id)) };
}

export function BotApprovalDialog({ organizationId, bot, rows, onClose, onDecided }: {
  organizationId: string; bot: Presence["bots"][number]; rows: BotApproval[];
  onClose: () => void; onDecided: (id: string) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [busy, setBusy] = useState<string | null>(null), [error, setError] = useState("");
  useEffect(() => { dialog.current?.showModal(); }, []);
  const decide = async (row: BotApproval, allow: boolean) => {
    setBusy(row.id); setError("");
    const requestSha = row.kind === "execution" ? row.command_sha256 : row.kind === "issue" ? row.document_sha256 : row.request_sha256;
    try {
      await request(organizationPath(organizationId) + `/my-bots/${row.kind}-approvals/${row.id}`,
        { method: "POST", body: { allow, request_sha256: requestSha } });
      onDecided(row.id);
    } catch (cause) { setError(errorMessage(cause)); }
    finally { setBusy(null); }
  };
  return <dialog ref={dialog} className="org-dialog org-bot-review" aria-label={`Review requests from ${bot.name}`} onCancel={onClose} onClose={onClose}>
    <header><h2>{bot.name}</h2><button type="button" aria-label="Close approval dialog" onClick={onClose}>×</button></header>
    <p className="org-help">Your decision applies to this exact action once. Closing this window leaves requests pending.</p>
    {!bot.online && <p className="org-help">Bot offline. Unconsumed requests stay pending; execution still requires valid bot access.</p>}
    {error && <p className="org-error" role="alert">{error}</p>}
    {!rows.length && <p role="status">No pending requests.</p>}
    {rows.map(row => <article className="org-approval-request" key={row.id}>
      <header><h3>{row.kind === "execution" ? "Run on the bot computer" : row.kind === "compute" ? "Run container computation" : "Publish a GitHub issue"}</h3>
        <small>{row.requester_name} · {row.topic_name}</small></header>
      {row.delivery_interrupted && <p className="org-error" role="alert">Bot delivery interrupted before execution. This request cannot be resumed here.
        Reject it and stop the retained invocation in its topic, then restart <code>make bot-run</code> and submit a new request. No action was automatically retried.</p>}
      {row.kind === "execution" ? <><p className="org-help">Confined to this topic's workspace, without home-directory or network access. Developer tests use a pinned checkout snapshot.</p>
        <pre><code>{JSON.parse(row.command_json).map((part: string) => JSON.stringify(part)).join(" ")}</code></pre></>
        : row.kind === "compute" ? <><p>{row.parameters.image_id} · {row.parameters.language}</p><pre><code>{row.parameters.program}</code></pre>
          <details><summary>Image and resource limits</summary><code>{row.image_digest}</code><pre>{JSON.stringify(row.parameters.resources, null, 2)}</pre></details>
          <p className="org-help">Uses the organization's isolated compute pool. Provider charges may apply.</p></>
          : <><p className="org-approval-repository">{row.repository}</p><h4>{row.title}</h4>
            <div className="org-approval-body"><ReactMarkdown remarkPlugins={[remarkGfm]}>{row.body}</ReactMarkdown></div>
            <p className="org-help">This exact title and body leave Reasoning and may be public. Check for private content and secrets.</p>
            {row.approval_role === "requester" && <p className="org-help">You requested this issue. The bot owner lets you approve your own issues to this repository; it is published with the owner's GitHub account and the attribution line above.</p>}</>}
      <footer><button type="button" className="org-primary" disabled={busy !== null || row.delivery_interrupted} onClick={() => void decide(row, true)}>
        {busy === row.id ? "Saving…" : row.kind === "issue" ? "Publish once" : "Allow once"}</button>
        <button type="button" disabled={busy !== null} onClick={() => void decide(row, false)}>Reject</button></footer>
    </article>)}
  </dialog>;
}
