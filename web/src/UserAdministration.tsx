// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, Invitation, Member, Role, aborted, errorMessage, invitationEmailLabel, organizationPath, request } from "./organization-api";

type UserAdministrationData = {
  members: Member[]; invitations: Invitation[]; mail: { enabled: boolean; daily_limit: number | null; worker_running?: boolean; used_today?: number };
  recoveries: { user_id: string; state: string; created_at: number; error_code: string | null }[];
  events: { sequence: number; actor_name: string | null; action: string; target_id: string; created_at: number }[];
};
const invitationState = (invite: Invitation) => invite.accepted_at ? "Joined" : invite.revoked_at ? "Revoked"
  : invite.expires_at * 1000 <= Date.now() ? "Expired" : "Pending";
const eventLabels: Record<string, string> = {
  "invitation.create": "Created invitation", "invitation.revoke": "Revoked invitation", "invitation.accept": "Accepted invitation",
  "membership.role": "Changed organization role", "membership.remove": "Removed organization membership",
  "account.recovery.request": "Requested recovery email", "account.password.recovered": "Recovered password",
  "account.password.change": "Changed password", "account.host-password-reset": "Host administrator reset password",
  "account.sessions.revoke": "Signed out other sessions", "account.password.notification_limited": "Password notification exceeded email limit",
  "invitation.email.queue": "Queued invitation email", "invitation.email.accepted": "Invitation email accepted by provider",
  "invitation.email.rejected": "Invitation email rejected by provider", "invitation.email.unknown": "Invitation email outcome unknown",
};
export function UserAdministration({ organization, onRefresh, onAuthLost }: {
  organization: { id: string; name: string; role: Role }; onRefresh: () => void; onAuthLost: () => void;
}) {
  const [data, setData] = useState<UserAdministrationData>();
  const [query, setQuery] = useState(""), [filter, setFilter] = useState("all");
  const [busy, setBusy] = useState(false), [error, setError] = useState(""), [message, setMessage] = useState(""), [link, setLink] = useState("");
  const attempts = useRef<Record<string, string>>({});
  const root = organizationPath(organization.id);
  const failed = useCallback((cause: unknown) => {
    if (cause instanceof ApiError && cause.status === 401) onAuthLost();
    else if (!aborted(cause)) setError(errorMessage(cause));
  }, [onAuthLost]);
  const reload = useCallback(async (signal?: AbortSignal) => {
    const result = await request<UserAdministrationData>(root + "/user-administration", { signal });
    setData(result);
  }, [root]);
  useEffect(() => { const controller = new AbortController();
    void reload(controller.signal).catch(failed);
    return () => controller.abort();
  }, [reload, failed]);
  const action = async (operation: () => Promise<unknown>) => {
    setBusy(true); setError(""); setMessage("");
    try { await operation(); await reload(); onRefresh(); } catch (cause) { failed(cause); }
    finally { setBusy(false); }
  };
  const needle = query.trim().toLocaleLowerCase();
  const matches = (...values: string[]) => values.some(value => value.toLocaleLowerCase().includes(needle));
  const members = (data?.members ?? []).filter(member => ["all", "members"].includes(filter) && matches(member.name, member.email));
  const invitations = (data?.invitations ?? []).filter(invite => filter !== "members" &&
    (filter === "all" || filter === "invitations" || invitationState(invite).toLowerCase() === filter) && matches(invite.email));
  const names = new Map([...(data?.members ?? []).map(member => [member.id, member.email] as const),
    ...(data?.invitations ?? []).map(invite => [invite.id, invite.email] as const)]);
  return <section className="org-user-administration" aria-label="User administration">
    <div className="org-users-search"><label>Search users and invitations<input type="search" placeholder="Name or email" value={query}
      onChange={event => setQuery(event.target.value)} /></label><label>Show<select aria-label="Show users and invitations" value={filter} onChange={event => setFilter(event.target.value)}>
        <option value="all">Everyone</option><option value="members">Members</option><option value="invitations">All invitations</option>
        <option value="pending">Pending invitations</option><option value="expired">Expired invitations</option>
        <option value="revoked">Revoked invitations</option><option value="joined">Accepted invitations</option>
      </select></label><button disabled={busy} onClick={() => void action(() => Promise.resolve())}>Refresh users</button></div>
    {error && <p role="alert" className="org-error">{error}</p>}{message && <p role="status">{message}</p>}
    {!data && !error && <p role="status">Loading users…</p>}
    {data && <><p role="status">{members.length} members · {invitations.length} invitations shown.</p>
      {data.mail?.enabled && data.mail.used_today !== undefined && <p>Email allowance: {data.mail.used_today} of {data.mail.daily_limit} requests in the last 24 hours.
        {data.mail.daily_limit !== null && data.mail.used_today >= data.mail.daily_limit ? " New emails cannot be queued until allowance is available. Contact the operator if access recovery is urgent." : ""}</p>}
      {data.mail?.enabled && data.mail.worker_running === false && <p role="status">Email delivery is delayed. Requests stay queued until the email worker recovers.</p>}
      {!members.length && !invitations.length && <p>No users or invitations match your search.</p>}
      {members.length > 0 && <><h3>Members</h3><ul className="org-admin-list">{members.map(member => {
        const delivery = data.recoveries?.find(row => row.user_id === member.id);
        return <li key={member.id}><div><strong>{member.name}</strong><small>{member.email}</small><small>Member · {member.role}</small>
          {delivery && <small>Recovery email: {delivery.state === "sent" ? "accepted by provider; inbox delivery unconfirmed" : delivery.state}
            {" · "}{new Date(delivery.created_at * 1000).toLocaleString()}</small>}</div>
          <div className="org-user-actions">
            <button disabled={busy || !data.mail?.enabled} onClick={() => {
              if (!window.confirm(`Email a password reset link to ${member.email}? They choose their own password.`)) return;
              void action(async () => {
                const key = "reset:" + member.id, client_id = attempts.current[key] ??= crypto.randomUUID();
                await request(root + "/members/" + member.id + "/password-reset", { method: "POST", body: { client_id } });
                delete attempts.current[key]; setMessage("Recovery email requested. The recipient chooses their own password.");
              });
            }}>Send password reset</button>
            {organization.role === "owner" || !["owner", "admin"].includes(member.role) ? <>
              <select aria-label={"Role for " + member.name} value={member.role} disabled={busy} onChange={event => void action(() =>
                request(root + "/members/" + member.id, { method: "PATCH", body: { role: event.target.value } }))}>
                {(organization.role === "owner" ? ["owner", "admin", "member", "viewer"] : ["member", "viewer"]).map(value => <option key={value}>{value}</option>)}
              </select><button disabled={busy} onClick={() => {
                if (window.confirm(`Remove ${member.name} from ${organization.name}? Their account and message history remain.`))
                  void action(() => request(root + "/members/" + member.id, { method: "DELETE" }));
              }}>Remove</button></> : null}
          </div></li>;
      })}</ul></>}
      {invitations.length > 0 && <><h3>Invitations</h3><ul className="org-admin-list">{invitations.map(invite => {
        const state = invitationState(invite), currentMember = data.members?.some(member => member.email.toLowerCase() === invite.email.toLowerCase());
        return <li key={invite.id}><div><strong>{invite.email}</strong><small>{invite.role} · {state}</small>
          <small>{invitationEmailLabel(invite)}</small><small>Expires {new Date(invite.expires_at * 1000).toLocaleString()}</small>
          {state === "Pending" && currentMember && <small>Already a member. Use sign in or password recovery; this invitation is still unused.</small>}</div>
          <div className="org-user-actions">
            {!invite.accepted_at && !invite.revoked_at && <>
              {data.mail?.enabled && <button disabled={busy} onClick={() => void action(async () => {
                const key = "invite:" + invite.id, client_id = attempts.current[key] ??= crypto.randomUUID();
                const next = await request<Invitation & { token: string }>(root + "/invitations/" + invite.id + "/resend", { method: "POST", body: { client_id } });
                delete attempts.current[key]; setLink(window.location.origin + "/#invite=" + encodeURIComponent(next.token));
                setMessage("Fresh invitation created. Ask the recipient to use the newest link.");
              })}>New link and email</button>}
              <button disabled={busy} onClick={() => { if (window.confirm("Revoke this invitation? The link will no longer work."))
                void action(() => request(root + "/invitations/" + invite.id, { method: "DELETE" })); }}>Revoke</button>
            </>}
          </div></li>;
      })}</ul></>}
      {!data.mail?.enabled && <p>Email is not configured. An operator can recover passwords; use the Invitations tab to share an invitation privately.</p>}
      {link && <label className="org-invite-link">Fresh invitation link<input readOnly type="password" value={link} onFocus={event => event.target.select()} />
        <button onClick={() => void navigator.clipboard.writeText(link).then(() => setMessage("Invitation link copied. Share it privately.")).catch(failed)}>Copy link</button></label>}
      <details><summary>Recent user administration activity</summary><ul className="org-user-audit">{(data.events ?? []).map(event =>
        <li key={event.sequence}><time>{new Date(event.created_at * 1000).toLocaleString()}</time>{" · "}{event.actor_name ?? "System"}
          {" · "}{eventLabels[event.action] ?? event.action.replaceAll(".", " ")}{" · "}{names.get(event.target_id) ?? "Retained record"}</li>)}</ul></details>
    </>}
  </section>;
}
