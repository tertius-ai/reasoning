import { PollBackoff, schedulePoll } from "./polling";
import React, { useEffect, useRef, useState } from "react";
import { publishComputeActivity, useTopicCommand } from "./TopicCommands";
import { ActivityCard, ActivityDialog, CardStop, TopicActivity } from "./TopicActivity";
import { ApiError, aborted, errorMessage, organizationPath, request, sharedRequest, topicPath } from "./organization-api";
import {
  ComputeArtifact, ComputeAttempt, ComputeConfiguration, ComputeEvent, ComputeJob, ComputeResources as Limits,
  DEFAULT_COMPUTE_RESOURCES, activeCompute, appendComputeEvents, canCancelCompute, captureComputeAttempt, computeJobPath,
  deliverComputeAttempt, initialComputeResources, matchesComputeAttempt,
} from "./compute-api";
import { ComputeResources } from "./ComputeResources";

export const LEAN_PROJECT_EXAMPLE = JSON.stringify({ format: 1, files: {
  "Proofs/Main.lean": "import Mathlib.Tactic.NormNum\n\ntheorem target : (2 : Nat) + 3 = 5 := by norm_num\n",
}, entry: "Proofs/Main.lean", target: "target", expected_type: "(2 : Nat) + 3 = 5" }, null, 2);

export function ComputeJobSummary({ job, canCancel, busy, onSelect, onCancel }: {
  job: ComputeJob; canCancel: boolean; busy: boolean; onSelect: () => void; onCancel: () => void;
}) {
  return <li className="org-compute-job"><header><button type="button" onClick={onSelect}>{job.image.name} · {job.id}</button>
    <span>{job.status === "outcome_unknown" ? "Outcome unknown · reservation held" : job.status}</span>
  </header><small>{job.parameters.language} · {job.parameters.resources.cpu} cores · {job.parameters.resources.wall_time_seconds}s maximum ·
    {job.accounted_at === null ? ` ${job.reserved_cpu_seconds} CPU-seconds reserved` : ` ${job.used_cpu_seconds} CPU-seconds charged`}</small>
    {!!job.cancel_requested && activeCompute(job) && <p role="status">Cancellation requested; remote termination is not yet confirmed.</p>}
    {job.error_code && <p className="org-error">{job.error_code}</p>}
  </li>;
}

function ReconcileCompute({ job, onUpdated, onAuthLost }: { job: ComputeJob; onUpdated: () => void; onAuthLost: () => void }) {
  const [elapsed, setElapsed] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  return <details className="org-reconcile"><summary>Reconcile uncertain computation</summary>
    <p className="org-help">Inspect worker {job.worker_id ?? "(unavailable)"} and verify that its container has stopped. This action releases a held slot and reservation; it never reruns the program or deletes cloud hardware.</p>
    <form onSubmit={(event) => { event.preventDefault(); if (busy || !confirmed) return; setBusy(true); setError("");
      void (async () => {
        try {
          const value = Number(elapsed);
          if (!/^\d+$/.test(elapsed) || !Number.isSafeInteger(value) || value > 86400000) throw new Error("Enter verified elapsed milliseconds from 0 to 86400000.");
          await request(computeJobPath(job.organization_id, job.id) + "/reconciliation", { method: "PUT", body: { elapsed_ms: value, cleanup_confirmed: true } });
          if (alive.current) onUpdated();
        } catch (cause) { if (alive.current) { if (cause instanceof ApiError && cause.status === 401) onAuthLost(); else setError(errorMessage(cause)); } }
        finally { if (alive.current) setBusy(false); }
      })();
    }}>
      <label>Verified elapsed time (milliseconds)<input required type="number" min={0} max={86400000} step={1} disabled={busy} value={elapsed} onChange={(event) => setElapsed(event.target.value)} /></label>
      <label className="org-compute-choice"><input required type="checkbox" disabled={busy} checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />I verified that the remote container has stopped.</label>
      {error && <p role="alert" className="org-error">{error}</p>}<button disabled={busy || !confirmed}>Record verified cleanup and usage</button>
    </form>
  </details>;
}

export function ComputeJobDetail({ organizationId, jobId, userId, canWrite, canManage, onClose, onAuthLost, embedded = false }: {
  organizationId: string; jobId: string; userId: string; canWrite: boolean; canManage: boolean; onClose: () => void; onAuthLost: () => void; embedded?: boolean;
}) {
  const [job, setJob] = useState<ComputeJob | null>(null);
  const [events, setEvents] = useState<ComputeEvent[]>([]);
  const [artifacts, setArtifacts] = useState<ComputeArtifact[]>([]);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const alive = useRef(true);
  const cursor = useRef(0);
  const root = computeJobPath(organizationId, jobId);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => {
    const controller = new AbortController(), backoff = new PollBackoff(2000, 30000, 1); let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      let backlog = false, active = true;
      try {
        const [current, page, files] = await Promise.all([
          request<ComputeJob>(root, { signal: controller.signal }),
          request<ComputeEvent[]>(root + "/events?after=" + cursor.current, { signal: controller.signal }),
          request<ComputeArtifact[]>(root + "/artifacts", { signal: controller.signal }),
        ]);
        if (controller.signal.aborted) return;
        appendComputeEvents([], page, cursor.current);
        const after = cursor.current;
        setEvents((existing) => appendComputeEvents(existing, page, after));
        cursor.current = page.at(-1)?.sequence ?? cursor.current;
        setJob(current); setArtifacts(files); setError(""); backlog = page.length === 200; active = activeCompute(current) || page.length > 0;
      } catch (cause) {
        if (aborted(cause)) return;
        if (cause instanceof ApiError && cause.status === 401) { onAuthLost(); return; }
        setError(errorMessage(cause));
      }
      // A finished job's record, log and artifacts no longer change; keep only a slow consistency check.
      if (!controller.signal.aborted) timer = schedulePoll(poll, backlog ? 0 : backoff.next(active), controller.signal);
    };
    void poll(); return () => { controller.abort(); clearTimeout(timer); };
  }, [root, refresh, onAuthLost]);
  return <section className="org-compute-detail" aria-label="Selected computation">
    {!embedded && <header><h3>Computation {jobId}</h3><button type="button" onClick={onClose}>Close computation</button></header>}
    {error && <p role="alert" className="org-error">{error}</p>}
    {!job ? <p role="status">Loading computation…</p> : <>
      <p>{job.status} · Exit code {job.exit_code ?? "pending"} · Elapsed {job.elapsed_ms ?? "pending"} ms</p>
      {!!job.cancel_requested && activeCompute(job) && <p role="status">Cancellation requested; remote termination is not yet confirmed.</p>}
      <details><summary>Immutable program and image evidence</summary><pre>{job.parameters.program}</pre>
        <dl><dt>Image</dt><dd>{job.image.reference}</dd><dt>Source revision</dt><dd>{job.image.source_revision}</dd>
          <dt>Program SHA-256</dt><dd>{job.program_digest}</dd><dt>Capabilities SHA-256</dt><dd>{job.image.capabilities_digest}</dd>
          <dt>SBOM SHA-256</dt><dd>{job.image.sbom_digest}</dd></dl>
        <pre>{JSON.stringify(job.parameters.resources, null, 2)}</pre>
      </details>
      <h4>Program output</h4><pre aria-label="Compute logs">{events.filter((entry) => entry.event.type === "log").map((entry) => entry.event.text ?? "").join("") || "No log output yet."}</pre>
      <h4>Committed artifacts</h4><p className="org-help">Files are untrusted output. Download only after reviewing the name and size; do not execute them. Desktop downloads are disabled; use this server in your browser to download.</p>
      {!artifacts.length ? <p>No committed artifacts.</p> : <ul>{artifacts.map((file) => <li key={file.id}>
        <a href={"/api" + root + "/artifacts/" + encodeURIComponent(file.id)} download={file.name}>{file.name}</a> · {file.size_bytes} bytes
        <small>SHA-256 {file.digest}</small>
      </li>)}</ul>}
      <details><summary>State events</summary><ul>{events.filter((entry) => entry.event.type !== "log").map((entry) => <li key={entry.sequence}>
        {new Date(entry.created_at * 1000).toLocaleString()} · {entry.event.type}{entry.event.status ? " · " + entry.event.status : ""}
      </li>)}</ul></details>
      {canManage && job.status === "outcome_unknown" && <ReconcileCompute key={job.id} job={job} onUpdated={() => setRefresh((value) => value + 1)} onAuthLost={onAuthLost} />}
    </>}
  </section>;
}

export function ComputeConversation({ organizationId, topicId, userId, canWrite, canManage, attempt, onAttempt, onAuthLost, toolbarTarget, onActivity }: {
  organizationId: string; topicId: string; userId: string; canWrite: boolean; canManage: boolean;
  attempt?: ComputeAttempt; onAttempt: (attempt?: ComputeAttempt) => void; onAuthLost: () => void;
  toolbarTarget: HTMLDivElement | null; onActivity: (records: TopicActivity[]) => void;
}) {
  const [configuration, setConfiguration] = useState<ComputeConfiguration | null>(null);
  const [jobs, setJobs] = useState<ComputeJob[]>([]);
  const [imageId, setImageId] = useState(attempt?.image_id ?? "");
  const [language, setLanguage] = useState(attempt?.language ?? "");
  const [program, setProgram] = useState(attempt?.program ?? "");
  const [limits, setLimits] = useState<Limits>(attempt?.resources ?? { ...DEFAULT_COMPUTE_RESOURCES });
  const [selected, setSelected] = useState<string | null>(null);
  const [editorOpen, setEditorOpen] = useState(!!attempt), [visibleLimit, setVisibleLimit] = useState(50), [more, setMore] = useState(false);
  const [busy, setBusy] = useState(false);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const alive = useRef(true), working = useRef(false), pending = useRef(attempt), updateAttempt = useRef(onAttempt);
  pending.current = attempt; updateAttempt.current = onAttempt;
  const root = organizationPath(organizationId);
  const path = topicPath(organizationId, topicId) + "/compute-jobs";
  const image = configuration?.images.find((item) => item.id === imageId);
  const allowed = configuration?.images.filter((item) => configuration.policy.allowed_images.includes(item.id)) ?? [];
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => {
    const controller = new AbortController(), backoff = new PollBackoff(2000, 10000); let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      let active = true;
      try {
        const first = await request<ComputeJob[]>(path + "?limit=50&standalone=true", { signal: controller.signal });
        const records = [...first]; let page = first;
        while (!controller.signal.aborted && page.length === 50 && records.length < visibleLimit) {
          page = await request<ComputeJob[]>(path + "?limit=50&standalone=true&before=" + page.at(-1)!.sequence, { signal: controller.signal });
          records.push(...page);
        }
        setMore(page.length === 50);
        if (controller.signal.aborted) return;
        setJobs(records); setConnected(true);
        active = Boolean(pending.current) || records.some(activeCompute);
        const saved = pending.current;
        if (saved && records.some((job) => matchesComputeAttempt(job, saved, organizationId, topicId, userId))) {
          pending.current = undefined; updateAttempt.current(undefined);
        }
      } catch (cause) {
        if (aborted(cause)) return;
        setConnected(false);
        if (cause instanceof ApiError && cause.status === 401) { onAuthLost(); return; }
        setError(errorMessage(cause));
      }
      if (!controller.signal.aborted) timer = schedulePoll(poll, backoff.next(active), controller.signal);
    };
    void poll(); return () => { controller.abort(); clearTimeout(timer); };
  }, [root, path, refresh, visibleLimit, organizationId, topicId, userId, onAuthLost]);
  useEffect(() => {
    // Images, policy and CPU balance are shown only in the submission dialog.
    if (!editorOpen) return;
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const value = await sharedRequest<ComputeConfiguration>(root + "/compute-configuration", { maxAge: 1000, signal: controller.signal });
        if (!controller.signal.aborted) setConfiguration(value);
      } catch (cause) {
        if (aborted(cause)) return;
        if (cause instanceof ApiError && cause.status === 401) { onAuthLost(); return; }
        setError(errorMessage(cause));
      }
      if (!controller.signal.aborted) timer = schedulePoll(poll, 5000, controller.signal);
    };
    void poll(); return () => { controller.abort(); clearTimeout(timer); };
  }, [root, editorOpen, refresh, onAuthLost]);
  const fail = (cause: unknown) => { if (cause instanceof ApiError && cause.status === 401) onAuthLost(); else setError(errorMessage(cause)); };
  const submit = async (event: React.FormEvent) => {
    event.preventDefault(); if (working.current || !canWrite || !connected) return;
    let captured: ComputeAttempt;
    try {
      if (!pending.current && (!image || !allowed.some((item) => item.id === image.id))) throw new Error("Select an allowed image.");
      captured = pending.current ?? captureComputeAttempt(image!, language, program, limits, crypto.randomUUID());
    } catch (cause) { fail(cause); return; }
    working.current = true; pending.current = captured; updateAttempt.current(captured); setBusy(true); setError("");
    try {
      const job = await deliverComputeAttempt(organizationId, topicId, userId, captured);
      if (!alive.current) return;
      pending.current = undefined; updateAttempt.current(undefined); setSelected(job.id); setEditorOpen(false); setRefresh((value) => value + 1);
    } catch (cause) { if (alive.current) fail(cause); }
    finally { working.current = false; if (alive.current) setBusy(false); }
  };
  const cancel = async (job: ComputeJob) => {
    if (working.current) return;
    working.current = true; setBusy(true); setError("");
    try { await request(computeJobPath(organizationId, job.id) + "/cancel", { method: "POST", body: {} }); if (alive.current) setRefresh((value) => value + 1); }
    catch (cause) { if (alive.current) fail(cause); }
    finally { working.current = false; if (alive.current) setBusy(false); }
  };
  useEffect(() => {
    onActivity(jobs.map((job, index) => ({ id: "compute:" + job.id, created_at: job.created_at, sequence: job.sequence,
      body: <ActivityCard title={"Compute · " + job.image.name} status={job.status} createdAt={job.created_at}
        initiallyOpen={index === 0 || selected === job.id || activeCompute(job)}
        action={canCancelCompute(job, userId, canWrite, canManage)
          ? <CardStop label="Stop this computation" disabled={busy} onStop={() => void cancel(job)} /> : undefined}>
        <ComputeJobDetail organizationId={organizationId} jobId={job.id} userId={userId} canWrite={canWrite} canManage={canManage}
          onClose={() => setSelected(null)} onAuthLost={onAuthLost} embedded />
      </ActivityCard> })));
  }, [jobs, busy, selected, canWrite, canManage, userId, onActivity, onAuthLost]);
  const active = jobs.filter(job => canCancelCompute(job, userId, canWrite, canManage));
  useEffect(() => { publishComputeActivity(topicId, active.length > 0); }, [topicId, active.length]);
  useEffect(() => () => { publishComputeActivity(topicId, false); }, [topicId]);
  useTopicCommand(topicId, command => {
    if (command === "compute" && canWrite && !busy) setEditorOpen(true);
    if (command === "stop" && canWrite && !busy) void (async () => { for (const job of active) await cancel(job); })();
  });
  return <section className="org-compute" aria-label="Containerized computations">
    {!editorOpen && error && <p role="alert" className="org-error">{error}</p>}
    {!editorOpen && attempt && <button type="button" onClick={() => setEditorOpen(true)}>Resume computation request</button>}
    {editorOpen && <ActivityDialog title="Containerized computation" onClose={() => setEditorOpen(false)}>
      <p className="org-help">Programs run on separate enrolled workers using operator-approved immutable images. Results are shared with this organization. This submits a single computation, not a versioned workflow.</p>
      {!configuration ? <p role="status">Loading compute configuration…</p> : <>
        <p className="org-budget">Available {configuration.balance.available_cpu_seconds} · Used {configuration.balance.used_cpu_seconds} · Held {configuration.balance.held_cpu_seconds} CPU-seconds</p>
        <p className="org-help">CPU-time quota is not a provider spending cap. Boot time, idle machines, storage, and network charges are additional.</p>
        {!allowed.length || configuration.policy.monthly_cpu_seconds === 0 ? <p className="org-help">An administrator must allow an approved image and set a positive CPU-time quota. A separate compatible worker must be installed to execute jobs.</p> : <p className="org-help">Queued jobs wait for a compatible worker; submission does not provision hardware.</p>}
      </>}
      {error && <p role="alert" className="org-error">{error}</p>}
      {canWrite ? <form onSubmit={(event) => void submit(event)}>
        <label htmlFor={`compute-image-${topicId}`}>Compute image</label><select id={`compute-image-${topicId}`} required disabled={busy || !!attempt} value={imageId} onChange={(event) => {
          const next = allowed.find((item) => item.id === event.target.value); setImageId(event.target.value); setLanguage(next?.id === "lean-mathlib" ? "lean" : next?.languages[0] ?? "");
          if (next) setLimits(initialComputeResources(next.maximum_resources, { ...DEFAULT_COMPUTE_RESOURCES,
            memory_bytes: next.id === "lean-mathlib" ? 4 * 1024 ** 3 : DEFAULT_COMPUTE_RESOURCES.memory_bytes }));
        }}><option value="">Select approved image</option>{allowed.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}</select>
        <label htmlFor={`compute-language-${topicId}`}>Program language</label><select id={`compute-language-${topicId}`} required disabled={busy || !!attempt} value={language} onChange={(event) => setLanguage(event.target.value)}>
          <option value="">Select language</option>{image?.languages.map((item) => <option key={item}>{item}</option>)}
        </select>
        {imageId === "lean-mathlib" && language === "lean" && <div className="org-compute-choice">
          <button type="button" disabled={busy || !!attempt} onClick={() => { if (!program.trim() || window.confirm("Replace the current editor contents with a Lean/mathlib project example?")) setProgram(LEAN_PROJECT_EXAMPLE); }}>Lean project example</button>
          <span>Lean source or project JSON · offline mathlib · report and source artifacts</span>
        </div>}
        <label htmlFor={`compute-program-${topicId}`}>Compute program</label><textarea id={`compute-program-${topicId}`} required rows={7} disabled={busy || !!attempt} value={program} onChange={(event) => setProgram(event.target.value)} spellCheck={false} />
        <ComputeResources value={limits} maximum={image?.maximum_resources} disabled={busy || !!attempt} onChange={setLimits} />
        {attempt && <div role="status"><p>Unacknowledged request {attempt.client_id}. Retry sends exactly the same captured program and limits. Switching topics preserves it; reloading closes this in-memory record.</p>
          <button type="button" disabled={busy} onClick={() => { if (window.confirm("Stop retrying this request? It may already be queued or running. Inspect shared jobs before deliberately submitting a new computation; this does not cancel an accepted job.")) {
            pending.current = undefined; updateAttempt.current(undefined); setError("");
          } }}>Stop retrying request</button></div>}
        <button className="org-primary" disabled={busy || !connected || (!attempt && (!image || !allowed.some((item) => item.id === image.id) || !program.trim() || !configuration?.policy.monthly_cpu_seconds))}>
          {busy ? "Submitting…" : attempt ? "Retry unchanged computation" : "Submit computation"}
        </button>
      </form> : <p className="org-help">You have read-only access to programs, logs, and committed artifacts.</p>}
      {!connected && configuration && <p role="status">Reconnecting to compute state…</p>}
    </ActivityDialog>}
    {more && <button type="button" onClick={() => setVisibleLimit(limit => limit + 50)}>Earlier compute activity</button>}
  </section>;
}
