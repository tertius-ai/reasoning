// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import React, { useEffect, useState } from "react";
import { aborted, BotMetadata, errorMessage, Member, organizationPath, request } from "./organization-api";

export function BotDeveloperAccess({ organizationId, bot, onSaved }: { organizationId: string; bot: BotMetadata; onSaved: () => void }) {
  const [enabled, setEnabled] = useState(bot.developer?.enabled ?? false), [repositories, setRepositories] = useState(bot.developer?.repositories.join("\n") ?? "");
  const [requesters, setRequesters] = useState<string[]>(bot.developer?.requesters ?? []), [members, setMembers] = useState<Member[]>([]);
  const [busy, setBusy] = useState(false), [error, setError] = useState(""), [saved, setSaved] = useState(false);
  const root = organizationPath(organizationId);
  useEffect(() => { const controller = new AbortController();
    void request<Member[]>(root + "/members", { signal: controller.signal }).then(setMembers).catch(cause => { if (!aborted(cause)) setError(errorMessage(cause)); });
    return () => controller.abort();
  }, [root]);
  return <details className="org-bot-developer"><summary>⬡ Developer tools · {bot.developer?.enabled ? "enabled" : "off"}</summary>
    <p className="org-help">Read sanitized server diagnostics and tracked source; request offline tests or GitHub issues. Every execution and issue needs your approval. No SSH, deploy, or unrestricted shell access.</p>
    <label><input type="checkbox" checked={enabled} onChange={event => setEnabled(event.target.checked)} /> Enable developer tools for {bot.name}</label>
    {enabled && <>
    <label>Allowed GitHub repositories<textarea rows={2} value={repositories} placeholder="owner/repository, one per line" onChange={event => setRepositories(event.target.value)} /></label>
    <p className="org-help">On the bot computer: <code>make dev-bot-config DEV_REPOSITORY=owner/repository</code>, then restart <code>make bot-run</code>. Pair with local execution enabled for source reads and tests; GitHub issues use its local <code>gh</code> login.</p>
    <details><summary>Other permitted requesters · {requesters.length}</summary><small>You are always permitted. The bot must also allow other users in its local runner policy: <code>make bot-run BOT_ALLOW_OTHER_USERS=1</code>.</small>
      {members.filter(member => member.id !== bot.created_by && member.role !== "viewer").map(member => <label key={member.id}>
        <input type="checkbox" checked={requesters.includes(member.id)} onChange={event => setRequesters(current => event.target.checked ? [...current, member.id] : current.filter(id => id !== member.id))} />{member.name}</label>)}
    </details></>}
    {error && <p role="alert">{error}</p>}{saved && <p role="status">Developer access saved.</p>}
    <button type="button" disabled={busy} onClick={() => { setBusy(true); setError(""); setSaved(false);
      void request(root + "/my-bots/" + bot.id + "/developer", { method: "PUT", body: { enabled,
        repositories: repositories.split(/[\n,]/).map(value => value.trim()).filter(Boolean), requesters } })
        .then(() => { setSaved(true); onSaved(); }).catch(cause => setError(errorMessage(cause))).finally(() => setBusy(false)); }}>Save developer access</button>
  </details>;
}

type IssueApproval = { id: string; repository: string; title: string; body: string; status: string; requester_name: string; issue_url: string | null };
export function BotIssueApprovals({ organizationId }: { organizationId: string }) {
  const [rows, setRows] = useState<IssueApproval[]>([]), [error, setError] = useState(""), [busy, setBusy] = useState(false);
  const path = organizationPath(organizationId) + "/my-bots/issue-approvals";
  useEffect(() => { const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    const poll = async () => { try { const value = await request<IssueApproval[]>(path, { signal: controller.signal }); if (!controller.signal.aborted) setRows(value); }
      catch (cause) { if (!aborted(cause)) setError(errorMessage(cause)); }
      if (!controller.signal.aborted) timer = setTimeout(poll, 2000); };
    void poll(); return () => { controller.abort(); clearTimeout(timer); };
  }, [path]);
  const decide = (id: string, allow: boolean) => { setBusy(true); setError("");
    void request(path + "/" + id, { method: "POST", body: { allow } }).then(() => setRows(current => current.filter(row => row.id !== id)))
      .catch(cause => setError(errorMessage(cause))).finally(() => setBusy(false)); };
  return <section aria-label="GitHub issue approvals">{error && <p role="alert">{error}</p>}
    {rows.map(row => <details key={row.id} open={row.status === "pending"}><summary>GitHub · {row.repository} · {row.title} · {row.status === "consumed" ? "outcome unconfirmed" : row.status}</summary>
      <small>Requested by {row.requester_name}</small><pre style={{ whiteSpace: "pre-wrap" }}>{row.body}</pre>
      {row.status === "pending" && <><p>This exact title/body will leave Reasoning and may be public. Check for private conversation content and secrets before approving.</p>
        <button disabled={busy} onClick={() => decide(row.id, true)}>Publish this exact issue once</button>{" "}<button disabled={busy} onClick={() => decide(row.id, false)}>Deny</button></>}
      {row.issue_url && /^https:\/\/github\.com\/[A-Za-z0-9._/-]+\/issues\/[1-9][0-9]*$/.test(row.issue_url) && <a href={row.issue_url} target="_blank" rel="noopener noreferrer">Open GitHub issue</a>}
      {["consumed", "outcome_unknown"].includes(row.status) && <p>Inspect GitHub and the bot's retained receipt. No automatic duplicate issue will be created.</p>}
    </details>)}
  </section>;
}
