import React, { useState } from "react";
import { ApiError, errorMessage, organizationPath, request } from "./organization-api";
import { WorkflowActionAttempt, captureWorkflowAction, deliverWorkflowExecution } from "./workflow-run-api";

type Blocker = { id: string; topic_id: string; created_at: number; status: string; revision: number; capacity_released: number };
type Capacity = { limit: number; in_use: number; runs: Blocker[]; truncated: boolean };

export function WorkflowCapacity({ organizationId, userId, canManage, onAuthLost }: {
  organizationId: string; userId: string; canManage: boolean; onAuthLost: () => void;
}) {
  const [open, setOpen] = useState(false), [value, setValue] = useState<Capacity | null>(null);
  const [error, setError] = useState(""), [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<WorkflowActionAttempt | null>(null);
  const fail = (cause: unknown) => {
    if (cause instanceof ApiError && cause.status === 401) onAuthLost(); else setError(errorMessage(cause));
  };
  const load = async () => {
    setError("");
    try { setValue(await request<Capacity>(organizationPath(organizationId) + "/workflow-capacity")); }
    catch (cause) { fail(cause); }
  };
  const release = async (run?: Blocker) => {
    const attempt = pending ?? (run ? captureWorkflowAction(run.id, crypto.randomUUID(), {
      operation: "release-capacity", revision: run.revision,
    }) : null);
    if (!attempt || busy) return;
    if (!pending && !window.confirm("Release this workflow's admission slot? Inspect its children first. Unknown outcomes and held charges remain; this stops future workflow dispatch and does not retry or settle its work.")) return;
    setPending(attempt); setBusy(true); setError("");
    try {
      await deliverWorkflowExecution(organizationId, run?.topic_id ?? "", userId, attempt);
      setPending(null); await load();
    } catch (cause) { fail(cause); }
    finally { setBusy(false); }
  };
  return <div className="org-workflow-capacity"><button type="button" onClick={() => {
    setOpen(!open); if (!open) void load();
  }}>Workflow capacity</button>{open && <section aria-label="Workflow capacity">
    <h4>Workflow capacity</h4>
    {value && <p>{value.in_use} of {value.limit} organization slots in use.</p>}
    <p>Uncertain work keeps its records and held charges. An administrator can release its admission slot after inspection.</p>
    {error && <p role="alert" className="org-error">{error}</p>}
    {pending && <button disabled={busy} onClick={() => void release()}>Retry the same capacity release</button>}
    {value?.runs.map(run => <div key={run.id}>
      <a href={"/organization.html?" + new URLSearchParams({ organization: organizationId, topic: run.topic_id })}>Open workflow topic</a>
      <span> · {new Date(run.created_at * 1000).toLocaleString()} · {run.status.replaceAll("_", " ")}</span>
      {run.capacity_released ? <small> · Slot released; outcome remains unresolved.</small>
        : canManage && run.status === "outcome_unknown" && <button disabled={busy || !!pending} onClick={() => void release(run)}>Release workflow slot</button>}
    </div>)}
    {value && !value.runs.length && <p>No unfinished workflows are visible in your accessible topics.</p>}
    {value?.truncated && <p>Showing up to 200 unfinished workflows, with occupied slots first.</p>}
    <button disabled={busy} onClick={() => void load()}>Refresh capacity</button>
  </section>}</div>;
}
