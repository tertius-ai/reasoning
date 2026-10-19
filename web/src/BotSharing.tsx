// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import React, { useEffect, useState } from "react";
import { aborted, BotMetadata, BotSharingPolicy, errorMessage, Member, organizationPath, request } from "./organization-api";

const CHOICES: { id: BotSharingPolicy; owner: string; administrator: string }[] = [
  { id: "owner", owner: "Only me", administrator: "Only its owner" },
  { id: "members", owner: "Specific members", administrator: "Specific members" },
  { id: "topic_writers", owner: "Everyone who can write in its topics", administrator: "Everyone who can write in its topics" },
];

export function sharingLabel(bot: BotMetadata, administrator = false): string {
  const policy = bot.sharing?.policy ?? "owner";
  if (policy === "members") {
    const count = bot.sharing?.members.length ?? 0;
    return `Used by ${administrator ? "its owner" : "you"} and ${count} ${count === 1 ? "member" : "members"}`;
  }
  if (policy === "topic_writers") return "Used by everyone who can write in its topics";
  return administrator ? "Used only by its owner" : "Used only by you";
}

/** The owner's server-side choice of who may invoke a bot; other members' requests outside it show "Not run". */
export function BotSharing({ organizationId, bot, path, administrator = false, onSaved }: {
  organizationId: string; bot: BotMetadata; path: string; administrator?: boolean; onSaved: () => void;
}) {
  const [policy, setPolicy] = useState<BotSharingPolicy>(bot.sharing?.policy ?? "owner");
  const [selected, setSelected] = useState<string[]>(bot.sharing?.members ?? []);
  const [members, setMembers] = useState<Member[]>([]);
  const [busy, setBusy] = useState(false), [error, setError] = useState(""), [saved, setSaved] = useState(false);
  useEffect(() => {
    if (policy !== "members" || members.length) return;
    const controller = new AbortController();
    void request<Member[]>(organizationPath(organizationId) + "/members", { signal: controller.signal }).then(setMembers)
      .catch(cause => { if (!aborted(cause)) setError(errorMessage(cause)); });
    return () => controller.abort();
  }, [organizationId, policy, members.length]);
  const current = CHOICES.find(choice => choice.id === (bot.sharing?.policy ?? "owner")) ?? CHOICES[0];
  const name = `bot-sharing-${bot.id}`;
  return <details className="org-bot-developer org-bot-sharing">
    <summary>Who can use this bot · {administrator ? current.administrator : current.owner}</summary>
    <fieldset>
      <legend>Who can use {bot.name}</legend>
      {CHOICES.map(choice => <label key={choice.id}>
        <input type="radio" name={name} value={choice.id} checked={policy === choice.id}
          onChange={() => { setPolicy(choice.id); setSaved(false); }} />
        {administrator ? choice.administrator : choice.owner}</label>)}
    </fieldset>
    <p className="org-help">Requesters also need write access to the topic. A request from anyone else is not run, and the
      topic tells them that this setting excludes them. Saving stops waiting requests from people it no longer includes.</p>
    {policy === "members" && <div role="group" aria-label="Members who can use this bot">
      {members.filter(member => member.id !== bot.created_by && member.role !== "viewer").map(member =>
        <label key={member.id}><input type="checkbox" checked={selected.includes(member.id)}
          onChange={event => { const checked = event.target.checked; setSaved(false);
            setSelected(value => checked ? [...value, member.id] : value.filter(id => id !== member.id)); }} />{member.name}</label>)}
      <small>Members removed from the organization lose access.</small>
    </div>}
    {policy !== "owner" && <p className="org-help" role="note">
      {administrator ? "The bot's owner is responsible for making sure it may be shared, for example that its CLI subscription or API terms allow use by other people."
        : "You are responsible for making sure you may share this bot, for example that your CLI subscription or API terms allow use by other people."}
      {" "}Reasoning does not check how the bot authenticates.</p>}
    {error && <p role="alert">{error}</p>}{saved && <p role="status">Who can use this bot saved.</p>}
    <button type="button" disabled={busy || policy === "members" && !selected.length} onClick={() => {
      setBusy(true); setError(""); setSaved(false);
      void request(path, { method: "PUT", body: { policy, members: policy === "members" ? selected : [] } })
        .then(() => { setSaved(true); onSaved(); }).catch(cause => setError(errorMessage(cause))).finally(() => setBusy(false));
    }}>Save who can use this bot</button>
  </details>;
}
