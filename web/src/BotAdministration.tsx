import React, { useCallback, useEffect, useRef, useState } from "react";
import { BotMetadata, Hierarchy, KIND_SYMBOL, errorMessage, organizationPath, request } from "./organization-api";
import { BotWebhooks } from "./BotWebhooks";
import { BotDeveloperAccess } from "./BotDeveloperAccess";
import { CloudBotWizard } from "./CloudBotWizard";

type Purpose = "notify" | "respond" | "observe";
type Credential = { token: string; name: string };

const PURPOSES: { id: Purpose; name: string; detail: string }[] = [
  { id: "notify", name: "Send updates", detail: "Post notifications without reading conversation history." },
  { id: "respond", name: "Watch and respond", detail: "Read messages and post replies in the same selected topics." },
  { id: "observe", name: "Read events", detail: "Read selected conversations without permission to post." },
];

function messageScopes(purpose: Purpose): ("messages:read" | "messages:write")[] {
  if (purpose === "notify") return ["messages:write"];
  if (purpose === "observe") return ["messages:read"];
  return ["messages:read", "messages:write"];
}

function purposeFor(bot: BotMetadata): Purpose {
  if (!bot.scopes.includes("messages:read")) return "notify";
  return bot.scopes.includes("messages:write") ? "respond" : "observe";
}

function permissionLabel(bot: BotMetadata): string {
  const messages = PURPOSES.find((item) => item.id === purposeFor(bot))?.name ?? "Custom access";
  const knowledge = bot.scopes.includes("knowledge:write") ? " · publish knowledge" : bot.scopes.includes("knowledge:read") ? " · read knowledge" : "";
  return messages + knowledge;
}

export function BotAdministration({ organizationId, personal = false, readOnly = false, pairCode, onConnected }: {
  organizationId: string; personal?: boolean; readOnly?: boolean; pairCode?: string; onConnected?: () => void;
}) {
  const [bots, setBots] = useState<BotMetadata[]>([]);
  const [hierarchy, setHierarchy] = useState<Hierarchy | null>(null);
  const [name, setName] = useState("");
  const [topics, setTopics] = useState<string[]>([]);
  const [purpose, setPurpose] = useState<Purpose>(readOnly ? "observe" : personal ? "respond" : "notify");
  const [knowledge, setKnowledge] = useState<"none" | "read" | "write">(personal ? readOnly ? "read" : "write" : "none");
  const [publicAccess, setPublicAccess] = useState(personal);
  const [days, setDays] = useState(30);
  const [editing, setEditing] = useState<string | null>(null);
  const [credential, setCredential] = useState<Credential | null>(null);
  const [showCredential, setShowCredential] = useState(false);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [connection, setConnection] = useState<{ name: string; engine: string } | null>(null);
  const [pairDismissed, setPairDismissed] = useState(false);
  const nameInput = useRef<HTMLInputElement>(null);
  const connecting = Boolean(pairCode && !editing && !pairDismissed);
  const [saved, setSaved] = useState("");
  const root = organizationPath(organizationId);
  const botsPath = root + (personal ? "/my-bots" : "/bots");
  const reload = useCallback(async () => {
    await Promise.all([
      request<BotMetadata[]>(botsPath).then(setBots),
      request<Hierarchy>(root + "/hierarchy").then(setHierarchy),
    ]);
  }, [root, botsPath]);
  useEffect(() => { void reload().catch((cause) => setError(errorMessage(cause))); }, [reload]);
  useEffect(() => {
    if (!pairCode || editing || pairDismissed) return;
    let active = true;
    void request<{ name: string; engine: string }>("/bot/connect/" + encodeURIComponent(pairCode)).then((value) => {
      if (active) { setConnection(value); setName(value.name); }
    }).catch((cause) => { if (active) setError(errorMessage(cause)); });
    return () => { active = false; };
  }, [pairCode, editing, pairDismissed]);
  useEffect(() => { if (editing) { nameInput.current?.focus(); nameInput.current?.scrollIntoView({ block: "nearest" }); } }, [editing]);
  const action = async (work: () => Promise<void>) => {
    setBusy(true); setError(""); setSaved(""); setCredential(null); setShowCredential(false); setCopied(false);
    try { await work(); await reload(); } catch (cause) { setError(errorMessage(cause)); }
    finally { setBusy(false); }
  };
  const clear = () => { setName(""); setTopics([]); setPublicAccess(personal); setPurpose(readOnly ? "observe" : personal ? "respond" : "notify");
    setKnowledge(personal ? readOnly ? "read" : "write" : "none"); setEditing(null); };
  const groups = hierarchy?.projects
    .slice().sort((a, b) => a.name.localeCompare(b.name))
    .flatMap((project) => hierarchy.channels.filter((channel) => channel.project_id === project.id)
      .sort((a, b) => a.name.localeCompare(b.name)).map((channel) => ({
        project, channel, topics: hierarchy.topics.filter((topic) => topic.channel_id === channel.id)
          .sort((a, b) => a.name.localeCompare(b.name)),
      })).filter((group) => group.topics.length)) ?? [];
  const toggleTopic = (id: string, checked: boolean) => setTopics((current) => checked ? [...new Set([...current, id])] : current.filter((item) => item !== id));
  const origin = typeof window === "undefined" ? "https://your-reasoning-server" : window.location.origin;
  const configName = (credential?.name ?? "my-bot").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "my-bot";
  const configPath = `~/.local/share/reasoning/bots/${configName}/config.json`;

  if (credential) return <section className="org-bots" aria-label="Organization bots">
    <section className="org-bot-credential" aria-live="polite">
      <span className="org-bot-step">Bot created</span>
      <h3>Save {credential.name}&apos;s credential</h3>
      <p>This secret appears once. Reasoning cannot recover it later; you can rotate it if it is lost.</p>
      <label>Bot credential<input type={showCredential ? "text" : "password"} value={credential.token} readOnly autoComplete="off"
        onFocus={(event) => event.target.select()} /></label>
      <div className="org-bot-actions">
        <button type="button" onClick={() => { if (!navigator.clipboard) { setError("Clipboard unavailable. Show the credential and copy it manually."); return; }
          void navigator.clipboard.writeText(credential.token).then(() => setCopied(true)).catch(() => setError("Clipboard unavailable. Show the credential and copy it manually.")); }}>{copied ? "Copied" : "Copy credential"}</button>
        <button type="button" onClick={() => setShowCredential(!showCredential)}>{showCredential ? "Hide credential" : "Show credential"}</button>
      </div>
      {error && <p role="alert" className="org-error">{error}</p>}
      <ol className="org-bot-next">
        <li>Copy the credential above.</li>
        <li>On the machine that will run the bot, open your Reasoning checkout. Install <a href="https://docs.astral.sh/uv/getting-started/installation/" target="_blank" rel="noopener noreferrer">uv</a> if needed, then create a protected directory:<pre><code>{`mkdir -m 700 -p ~/.local/share/reasoning/bots/${configName}`}</code></pre></li>
        <li>Run this command and paste the credential at its private prompt:<pre><code>{`uv run --locked reasoning-bot configure --server ${origin} --output ${configPath}`}</code></pre></li>
        <li>Verify the connection and list your granted topic IDs:<pre><code>{`uv run --locked reasoning-bot identity --config ${configPath}\nuv run --locked reasoning-bot topics --config ${configPath}`}</code></pre></li>
      </ol>
      <p className="org-help">Creating access does not start a bot process. Your integration runs separately and uses this private configuration. See <a href="https://github.com/tertius-ai/reasoning/blob/main/docs/bots.md" target="_blank" rel="noopener noreferrer">the bot guide</a> for a test post and polling example.</p>
      <button className="org-primary" type="button" onClick={() => { setCredential(null); setShowCredential(false); setCopied(false); }}>I saved the credential</button>
    </section>
  </section>;

  return <section className="org-bots" aria-label="Organization bots">
    <header className="org-bot-intro"><div><h3>{editing ? "Edit bot access" : "Bots"}</h3>
      <p>Bots run outside Reasoning. {personal ? "Connect Codex, Claude Code, or your own program on your computer. Bots act only within your selected access." : "Manage organization bots and their delegated access."}</p></div><span>External process</span></header>
    {error && <p role="alert" className="org-error">{error}</p>}
    {saved && <p role="status">{saved}</p>}
    {personal && !readOnly && !pairCode && !editing && <CloudBotWizard organizationId={organizationId} onChanged={reload} />}
    {personal && !pairCode && !editing && <details className="org-bot-next">
      <summary>Connect from your computer · Codex or Claude Code</summary>
      <p>On the bot computer, run this from a Reasoning checkout. The wizard opens this site to approve its access, then keeps the bot online. Leave the terminal open; Ctrl-C stops it. No token copying is needed.</p>
      <pre><code>{`make bot-connect BOT_SERVER=${origin}`}</code></pre>
      <p>Sign in to Codex or Claude Code on that computer first. Local CAS and workspace access are choices on that computer, not permissions granted by chat.</p>
    </details>}
    {connecting && <section className="org-bot-next"><h3>Approve {connection?.engine === "claude" ? "Claude Code" : "Codex"} connection</h3>
      <p>Confirm this code matches the terminal on your bot computer: <strong>{pairCode}</strong>. Only approve a connection you started yourself.</p>
      <p>Select the organization above before approving. This grants selected chat access, not permission to execute on your computer.</p>
    </section>}
    <details open={Boolean(connecting || editing || !personal)}><summary>{editing ? "Edit access" : connecting ? "Select access and approve" : "Custom bot / manual credential"}</summary>
    <form className="org-bot-wizard" onSubmit={(event) => { event.preventDefault(); void action(async () => {
      const body = { name, topic_ids: topics, public_access: publicAccess, scopes: [...messageScopes(purpose),
        ...(knowledge === "read" ? ["knowledge:read"] : knowledge === "write" ? ["knowledge:read", "knowledge:write"] : [])] };
      if (editing) { await request(botsPath + "/" + editing, { method: "PUT", body }); setSaved("Bot access saved."); }
      else if (connecting && pairCode) {
        await request(botsPath + "/connect/" + encodeURIComponent(pairCode), { method: "POST", body: {
          topic_ids: topics, public_access: publicAccess, scopes: body.scopes, lifetime_seconds: days * 86400,
        } });
        onConnected?.();
      } else {
        const result = await request<BotMetadata & { token: string }>(botsPath, { method: "POST", body: { ...body, lifetime_seconds: days * 86400 } });
        setCredential({ token: result.token, name });
      }
      clear();
    }); }}>
      <fieldset className="org-bot-purpose"><legend><span>1</span> Choose what the bot can do</legend>
        <div>{PURPOSES.map((choice) => <label key={choice.id} className={purpose === choice.id ? "selected" : ""}>
          <input type="radio" name="bot-purpose" value={choice.id} checked={purpose === choice.id} disabled={readOnly && choice.id !== "observe"} onChange={() => setPurpose(choice.id)} />
          <span><strong>{choice.name}</strong><small>{choice.detail}</small></span>
        </label>)}</div>
      </fieldset>
      <fieldset className="org-bot-topics"><legend><span>2</span> Access</legend>
        <label><input type="checkbox" checked={publicAccess} onChange={event => setPublicAccess(event.target.checked)} />
          {personal ? "Follow my access, including private projects and new topics" : "All organization-public conversations, including new topics"}</label>
        <p id="org-bot-topic-help">{personal ? "Following access rechecks your current permissions on every request. Disable it to restrict this bot to exact topics below." : "Private conversations require explicit topic grants."} Context is fetched narrowly, per request.</p>
        {!publicAccess && <details open={Boolean(editing)}><summary>Restricted topics · {topics.length} selected</summary>
        {!groups.length && <p className="org-help">Create a project, channel, and topic before restricting a bot.</p>}
        {groups.map((group) => {
          const ids = group.topics.map((topic) => topic.id);
          const selected = ids.every((id) => topics.includes(id));
          return <section key={group.channel.id}><header><strong>{group.project.name} / #{group.channel.name}</strong>
            <button type="button" onClick={() => setTopics((current) => selected ? current.filter((id) => !ids.includes(id)) : [...new Set([...current, ...ids])].slice(0, 64))}>{selected ? "Clear" : "Select all"}</button></header>
            <div>{group.topics.map((topic) => <label key={topic.id}><input type="checkbox" value={topic.id} checked={topics.includes(topic.id)}
              onChange={(event) => toggleTopic(topic.id, event.target.checked)} /><span><i>{KIND_SYMBOL[topic.kind]}</i>-{topic.name}</span></label>)}</div>
          </section>;
        })}
        <small>{topics.length} of 64 topics selected</small>
        </details>}
      </fieldset>
      <fieldset><legend><span className="org-bot-step-number">3</span> Shared knowledge</legend>
        <label>Knowledge access<select value={knowledge} onChange={(event) => setKnowledge(event.target.value as typeof knowledge)}>
          <option value="none">No ledger, bibliography, or journal access</option>
          <option value="read">Read shared knowledge</option>
          {!readOnly && <option value="write">Read and publish shared knowledge</option>}
        </select></label><p className="org-help">Publishing creates attributed, versioned entries. Draft journal posts remain excluded from beta-topic context.</p>
      </fieldset>
      <fieldset className="org-bot-details"><legend><span>4</span> {editing ? "Save access" : "Name and create"}</legend>
        <label>Bot name<input ref={nameInput} required readOnly={connecting} maxLength={200} value={name} onChange={(event) => setName(event.target.value)} placeholder="Literature alerts" /></label>
        {!editing && <label>Credential lifetime<select value={days} onChange={(event) => setDays(Number(event.target.value))}>
          <option value={7}>7 days</option><option value={30}>30 days (recommended)</option><option value={90}>90 days</option><option value={365}>1 year</option>
        </select><small>Rotate the credential before it expires. Rotation immediately disables the old credential.</small></label>}
      </fieldset>
      <div className="org-bot-submit"><button className="org-primary" disabled={busy || !name.trim() || (!topics.length && !publicAccess) || topics.length > 64}>{editing ? "Save access" : connecting ? "Approve connection" : "Create bot and show credential"}</button>
        {editing && <button type="button" disabled={busy} onClick={clear}>Cancel</button>}</div>
    </form>
    </details>
    {personal && <p className="org-help">Execution, compute and GitHub requests appear beside your bot in People. Click its approval indicator to review.</p>}
    <section className="org-bot-existing"><h3>Existing bots</h3>{!bots.length && <p className="org-help">No bot access has been created yet.</p>}
      <ul className="org-admin-list">{bots.map((bot) => <li key={bot.id} className="org-bot-card"><div className="org-bot-card-heading"><span aria-label={bot.developer?.enabled ? "Developer bot" : "Bot"}>{bot.developer?.enabled ? "⬡" : "◆"}</span><strong title={[bot.runtime?.engine === "claude" ? "Claude Code" : bot.runtime?.engine === "codex" ? "Codex" : "External bot", ...(bot.runtime?.models ?? [])].join(" · ")}>{bot.name}</strong>
        <small>{bot.revoked_at ? "Revoked" : !bot.enabled ? "Access expired" : bot.runtime?.online ? "Online" : "Offline"}</small></div>
        <div className="org-bot-card-access"><span>{permissionLabel(bot)}</span><span>{bot.public_access ? personal ? "Follows your access" : "Organization-public access" : `${bot.explicit_topic_ids?.length ?? bot.topic_ids.length} restricted topics`}</span>
          <span>{bot.runtime?.local_tools ? "Local execution · approval required" : "Local execution not enabled"}</span></div>
        <small title={new Date(bot.expires_at * 1000).toLocaleString()}>Credential expires {new Date(bot.expires_at * 1000).toLocaleDateString()}</small>
        {personal && !readOnly && !bot.revoked_at && <BotDeveloperAccess organizationId={organizationId} bot={bot} onSaved={() => void reload()} />}
        {!bot.revoked_at && <div className="org-bot-row-actions">
          <button disabled={busy} onClick={() => { setPairDismissed(true); setError(""); setSaved(""); setEditing(bot.id); setName(bot.name); setTopics(bot.explicit_topic_ids ?? bot.topic_ids);
            setPublicAccess(bot.public_access ?? false); setPurpose(purposeFor(bot));
            setKnowledge(bot.scopes.includes("knowledge:write") ? "write" : bot.scopes.includes("knowledge:read") ? "read" : "none"); }}>Edit access</button>
          <button disabled={busy} onClick={() => { if (window.confirm("Rotate this bot credential? Its current token will stop working immediately. The new token expires after 30 days and you become its sponsor.")) void action(async () => {
            const result = await request<BotMetadata & { token: string }>(botsPath + "/" + bot.id + "/token", { method: "POST", body: { lifetime_seconds: 2592000 } }); setCredential({ token: result.token, name: bot.name });
          }); }}>Rotate credential</button>
          <button disabled={busy} onClick={() => { if (window.confirm("Permanently revoke this bot? Its messages remain, but its credential cannot be reactivated.")) void action(async () => {
            await request(botsPath + "/" + bot.id, { method: "DELETE" }); if (editing === bot.id) clear();
          }); }}>Revoke</button>
        </div>}
      </li>)}</ul>
    </section>
    {!personal && <BotWebhooks key={organizationId} organizationId={organizationId} bots={bots} />}
  </section>;
}
