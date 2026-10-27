// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import React, { useCallback, useEffect, useState } from "react";
import { errorMessage, organizationPath, request } from "./organization-api";

type CloudBot = {
  id: string; name: string; engine: "codex" | "claude"; phase: string; error: string | null;
  console_url: string | null; login_command: string; tools: string[];
  quote: { region: string; size: string; quote: { compute_hourly_usd: string; estimated_730_hours_usd: string } };
};

export function CloudBotWizard({ organizationId, onChanged }: { organizationId: string; onChanged?: () => Promise<void> }) {
  const root = organizationPath(organizationId) + "/my-cloud-bots";
  const [jobs, setJobs] = useState<CloudBot[]>([]);
  const [engine, setEngine] = useState<"codex" | "claude">("codex");
  const [name, setName] = useState("cloud-bot");
  const [region, setRegion] = useState("fra1");
  const [size, setSize] = useState("s-1vcpu-2gb");
  const [token, setToken] = useState("");
  const [ceiling, setCeiling] = useState("");
  const [cas, setCas] = useState(false);
  const [lean, setLean] = useState(false);
  const [confirmation, setConfirmation] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [consoleAccess, setConsoleAccess] = useState<Record<string, { username: string; password: string; command: string }>>({});
  useEffect(() => {
    setConsoleAccess((current) => Object.fromEntries(Object.entries(current).filter(([id]) =>
      jobs.some((job) => job.id === id && ["login_required", "start_failed"].includes(job.phase)))));
  }, [jobs]);
  const reload = useCallback(async () => {
    setJobs(await request<CloudBot[]>(root));
    await onChanged?.();
  }, [root, onChanged]);
  useEffect(() => {
    let live = true;
    const refresh = () => { if (live) void reload().catch((cause) => { if (live) setError(errorMessage(cause)); }); };
    refresh();
    const timer = window.setInterval(refresh, 5000);
    return () => { live = false; window.clearInterval(timer); };
  }, [reload]);
  const action = async (work: () => Promise<void>) => {
    setBusy(true); setError("");
    try { await work(); await reload(); } catch (cause) { setError(errorMessage(cause)); }
    finally { setBusy(false); }
  };
  return <details className="org-bot-next"><summary>Create a cloud bot · DigitalOcean</summary>
    <p>Dedicated hardware in your account, running Codex or Claude Code. You need a DigitalOcean account with billing
      and a supported provider account. No local CLI installation or private repository access is needed.</p>
    <p>The Reasoning server needs outbound SSH (TCP 22). For a standard DigitalOcean deployment, its operator runs
      <code> make bot-cloud-enable</code> once after upgrading. This does not open inbound SSH. Alternatively run <code>make bot-cloud</code> on your computer.</p>
    <p><a href="https://cloud.digitalocean.com/registrations/new" target="_blank" rel="noopener noreferrer">DigitalOcean account</a>
      {" · "}<a href="https://cloud.digitalocean.com/account/api/tokens" target="_blank" rel="noopener noreferrer">Create API token</a></p>
    <p>Token permissions: account:read, sizes:read, ssh_key:read/create, tag:read/create,
      firewall:read/create, droplet:read/create. Include Droplet and Tag reads so firewall ownership is visible.
      Your token is encrypted in owner-scoped Reasoning setup state, never sent to the bot.</p>
    {error && <p role="alert" className="org-error">{error}</p>}
    <form className="org-bot-wizard" onSubmit={(event) => { event.preventDefault(); void action(async () => {
      await request<CloudBot>(root + "/quote", { method: "POST", body: {
        name, engine, models: ["default"], region, size, token,
        maximum_hourly_usd: ceiling, cas, lean,
      } }); setToken("");
    }); }}>
      <label>Bot name<input value={name} onChange={(event) => setName(event.target.value)} pattern="[a-z0-9][a-z0-9-]{0,40}" required /></label>
      <label>Provider<select value={engine} onChange={(event) => setEngine(event.target.value as "codex" | "claude")}>
        <option value="codex">Codex</option><option value="claude">Claude Code</option>
      </select></label>
      <p>Model choices are discovered automatically after provider sign-in and stay up to date while the bot runs.</p>
      <label>Region<input value={region} onChange={(event) => setRegion(event.target.value)} required /></label>
      <label>CPU Droplet size<input value={size} onChange={(event) => setSize(event.target.value)} required /></label>
      <label>Maximum hardware USD/hour<input value={ceiling} onChange={(event) => setCeiling(event.target.value)} inputMode="decimal" required /></label>
      <label>DigitalOcean token<input type="password" value={token} onChange={(event) => setToken(event.target.value)} autoComplete="off" required /></label>
      <label><input type="checkbox" checked={cas} onChange={(event) => { const checked = event.target.checked; setCas(checked);
        setSize(lean ? "s-4vcpu-8gb" : checked ? "s-2vcpu-4gb" : "s-1vcpu-2gb"); }} />Local CAS · SymPy, GAP, PARI/GP, Singular (4 GiB minimum)</label>
      <label><input type="checkbox" checked={lean} onChange={(event) => { const checked = event.target.checked; setLean(checked);
        setSize(checked ? "s-4vcpu-8gb" : cas ? "s-2vcpu-4gb" : "s-1vcpu-2gb"); }} />Lean 4 + mathlib (8 GiB minimum)</label>
      <p>The bot follows your topic access. Only you can use it until you choose otherwise under <strong>Who can use this bot</strong> in My bots.
        Local execution still requires your approval in People, even when someone else requests it.</p>
      <button disabled={busy}>Get quote · no cloud resources created</button>
    </form>
    {jobs.map((job) => {
      const login = ["login_required", "start_failed"].includes(job.phase);
      const active = ["provisioning", "installing", "starting"].includes(job.phase);
      const verb = login ? "start" : "create";
      const expected = `${verb} bot ${job.name}`;
      return <section key={job.id} className="org-bot-next" aria-label={`Cloud bot ${job.name}`}>
        <h4>{job.name} · {job.engine} · {job.phase.replaceAll("_", " ")}</h4>
        <p>{job.quote.region} · {job.quote.size} · ${job.quote.quote.compute_hourly_usd}/hour ·
          approximately ${job.quote.quote.estimated_730_hours_usd}/730 hours. {job.tools.join(", ") || "Inference only"}.</p>
        <p>Hardware billing continues while powered off or awaiting sign-in. Destroy the dedicated Droplet in DigitalOcean to end VM charges.
          Provider inference, tax, transfer, storage and other services are separate.</p>
        {job.error && <p role="alert" className="org-error">{job.error}</p>}
        {job.console_url && <a href={job.console_url} target="_blank" rel="noopener noreferrer">Open owned Droplet</a>}
        {login && <><p>In DigitalOcean, choose Settings → Recovery console → Launch Console, not the SSH-based Droplet Console.
          Press Enter at the console, then log in as the unprivileged bot user. No root password or public SSH rule is needed.</p>
          <button type="button" disabled={busy} onClick={() => void action(async () => {
            const access = await request<{ username: string; password: string; command: string }>(root + `/${job.id}/console-access`,
              { method: "POST", body: {} });
            setConsoleAccess((current) => ({ ...current, [job.id]: access }));
          })}>Show my private setup-console access</button>
          {consoleAccess[job.id] && <><p>Username: <code>{consoleAccess[job.id].username}</code>. Paste with Ctrl-V if the console needs it.</p>
            <label>Temporary console password<input readOnly value={consoleAccess[job.id].password} type="text" autoComplete="off"
              onFocus={(event) => event.target.select()} /></label>
            <p>Run after logging in:</p><pre><code>{consoleAccess[job.id].command}</code></pre>
            <p>This password is only for initial console sign-in; it is locked after successful start. Password-based SSH stays disabled.</p></>}
          <p>{job.engine === "codex" ? "Enable device authentication in your ChatGPT account/workspace if requested."
            : "Open Claude’s sign-in URL in your browser and return the login code to the console."}
            {" "}Provider credentials stay on this VM; don’t paste them into chat.</p></>}
        {job.phase !== "ready" && !active && <form onSubmit={(event) => { event.preventDefault(); void action(async () => {
          const changed = await request<CloudBot>(root + `/${job.id}/${verb}`, { method: "POST", body: { confirmation: confirmation[job.id] ?? "" } });
          setJobs((current) => current.map((item) => item.id === changed.id ? changed : item));
          setConfirmation((current) => ({ ...current, [job.id]: "" }));
        }); }}>
          <label>Type <code>{expected}</code><input value={confirmation[job.id] ?? ""}
            onChange={(event) => setConfirmation((current) => ({ ...current, [job.id]: event.target.value }))} autoComplete="off" required /></label>
          <button disabled={busy || confirmation[job.id] !== expected}>{login ? "Verify sign-in and start" : job.phase === "quoted" ? "Create bot VM" : "Resume same VM setup"}</button>
        </form>}
        {active && <p role="status">Working… setup may take several minutes. Keep this setup state; do not create a second VM.</p>}
        {job.phase === "ready" && <p>Runner started. Approval requests appear in People; manage access in My bots.</p>}
      </section>;
    })}
  </details>;
}
