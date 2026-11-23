import React, { useEffect, useRef, useState } from "react";
import { ApiError, aborted, errorMessage, organizationPath, request } from "./organization-api";
import { LEAN_ENVIRONMENT_STATUS, LeanEnvironment, captureLeanEnvironmentRequest } from "./compute-api";

/** Every source an environment fetches, by URL and exact revision: what an administrator approves. */
export function LeanEnvironmentSources({ environment }: { environment: LeanEnvironment }) {
  return <div className="org-lean-sources-wrap"><table className="org-lean-sources" aria-label={`Sources for ${environment.specification.toolchain}`}>
    <thead><tr><th>Source</th><th>Repository</th><th>Revision</th></tr></thead>
    <tbody>{environment.sources.map((source) => <tr key={source.kind + ":" + source.name}>
      <td>{source.kind === "toolchain" ? "Lean toolchain" : source.name}</td>
      <td><a href={source.url} target="_blank" rel="noreferrer noopener">{source.url}</a>{source.sub_dir && <small>Sub-directory {source.sub_dir}</small>}</td>
      <td><code>{source.revision}</code></td>
    </tr>)}</tbody>
  </table></div>;
}

export function LeanEnvironments({ organizationId, onAuthLost }: { organizationId: string; onAuthLost: () => void }) {
  const [records, setRecords] = useState<LeanEnvironment[] | null>(null);
  const [toolchain, setToolchain] = useState(""), [manifest, setManifest] = useState("");
  const [error, setError] = useState(""), [busy, setBusy] = useState(false), [refresh, setRefresh] = useState(0);
  const alive = useRef(true), working = useRef(false);
  const root = organizationPath(organizationId) + "/compute-environments";
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => {
    const controller = new AbortController();
    void request<LeanEnvironment[]>(root, { signal: controller.signal }).then((value) => {
      if (!controller.signal.aborted) setRecords(value);
    }).catch((cause) => {
      if (aborted(cause) || controller.signal.aborted) return;
      if (cause instanceof ApiError && cause.status === 401) onAuthLost(); else setError(errorMessage(cause));
    });
    return () => controller.abort();
  }, [root, refresh, onAuthLost]);
  const action = async (work: () => Promise<void>) => {
    if (working.current) return;
    working.current = true; setBusy(true); setError("");
    try { await work(); }
    catch (cause) { if (alive.current) { if (cause instanceof ApiError && cause.status === 401) onAuthLost(); else setError(errorMessage(cause)); } }
    finally { working.current = false; if (alive.current) { setBusy(false); setRefresh((value) => value + 1); } }
  };
  const decide = (environment: LeanEnvironment, decision: "approved" | "declined") => {
    const count = environment.sources.length;
    const question = decision === "approved"
      ? `Approve ${environment.specification.toolchain} with these ${count} sources, each at exactly the listed revision? An approved environment stays approved.`
      : `Decline ${environment.specification.toolchain}? An administrator can still approve it later.`;
    if (!window.confirm(question)) return;
    void action(async () => {
      await request(root + "/" + encodeURIComponent(environment.id) + "/decision", { method: "PUT", body: { decision, key: environment.key } });
    });
  };
  return <section className="org-lean-environments" aria-label="Lean environments">
    <h3>Lean environments</h3>
    <p className="org-help">A Lean environment is a project's toolchain and pinned dependencies. Paste the project's <code>lean-toolchain</code> and
      {" "}<code>lake-manifest.json</code>; the server keeps only the official Lean release or release candidate, and each git package on
      {" "}github.com with its full commit hash. The same files always name the same environment, so a request for one the organization already
      has returns it. Approve only sources you trust: approval records your decision for exactly the listed revisions. A commit hash
      does not show that the commit is on the named repository, because GitHub also serves a fork's commits under the original URL.
      An operator prepares an approved environment on the Modal relays with
      {" "}<code>make compute-environment-prepare ENVIRONMENT=&lt;ID&gt;</code>, which first checks that each commit is on a branch or
      tag of its named repository. Naming a prepared environment in a job is not available yet.</p>
    {error && <p role="alert" className="org-error">{error}</p>}
    <form onSubmit={(event) => { event.preventDefault(); void action(async () => {
      const body = captureLeanEnvironmentRequest(toolchain, manifest);
      const record = await request<LeanEnvironment>(root, { method: "POST", body });
      if (!record || !/^[a-f0-9]{32}$/.test(record.id) || !/^[a-f0-9]{64}$/.test(record.key) || record.organization_id !== organizationId) {
        throw new Error("The server did not acknowledge the Lean environment request. Reload the list before asking again.");
      }
      if (alive.current) { setToolchain(""); setManifest(""); }
    }); }}>
      <label>lean-toolchain<textarea rows={1} maxLength={128} value={toolchain} disabled={busy} spellCheck={false}
        placeholder="leanprover/lean4:v4.24.0" onChange={(event) => setToolchain(event.target.value)} /></label>
      <label>lake-manifest.json<textarea rows={8} value={manifest} disabled={busy} spellCheck={false}
        onChange={(event) => setManifest(event.target.value)} /></label>
      <button className="org-primary" disabled={busy || !toolchain.trim() || !manifest.trim()}>Review sources</button>
    </form>
    {records === null ? <p role="status">Loading Lean environments…</p> : !records.length ? <p className="org-help">No Lean environments yet.</p> :
      <ul className="org-lean-environment-list">{records.map((environment) => <li key={environment.id} className="org-lean-environment">
        <header><strong>{environment.specification.toolchain}</strong>
          <span>{environment.specification.packages.length === 1 ? "1 package" : `${environment.specification.packages.length} packages`}</span>
          <span className={"org-lean-status org-lean-status-" + environment.status}>{LEAN_ENVIRONMENT_STATUS[environment.status]}</span></header>
        <small>ID {environment.id} · key {environment.key} · requested {new Date(environment.requested_at * 1000).toLocaleString()}
          {environment.decided_at !== null && <> · {environment.status} {new Date(environment.decided_at * 1000).toLocaleString()}</>}</small>
        <LeanEnvironmentSources environment={environment} />
        {environment.status !== "approved" && <div className="org-lean-environment-actions">
          <button className="org-primary" disabled={busy} onClick={() => decide(environment, "approved")}>Approve these sources</button>
          {environment.status === "requested" && <button disabled={busy} onClick={() => decide(environment, "declined")}>Decline</button>}
        </div>}
      </li>)}</ul>}
  </section>;
}
