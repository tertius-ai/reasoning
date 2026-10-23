import React, { useEffect, useRef, useState } from "react";
import { ApiError, aborted, errorMessage, organizationPath, request } from "./organization-api";
import { WorkflowActionAttempt, captureWorkflowAction, deliverWorkflowExecution, workflowRunPath } from "./workflow-run-api";
import { ActivityDialog } from "./TopicActivity";
import { schedulePoll } from "./polling";
import "./workflow-capacity.css";

export type CapacityRun = { id: string; topic_id: string; created_at: number; status: string; revision: number; capacity_released: number; finished_at: number | null;
  workflow_name: string; topic_name: string; channel_name: string; project_name: string; requested_by_name: string; topic_deleted: number };
export type Capacity = { limit: number; in_use: number; available: number; hidden_in_use: number; released_count: number;
  runs: CapacityRun[]; truncated: boolean };
type Inspection = { run: CapacityRun; can_release: boolean; release_blocked_reason: string | null;
  nodes: { node_id: string; kind: string; status: string; children: { id: string; kind: string; status: string; accounting_state?: string | null }[] }[] };
const statusLabel = (status: string) => ({ running: "In progress", awaiting_approval: "Waiting for approval",
  outcome_unknown: "Needs inspection", record_unavailable: "Record unavailable", queued: "Queued", waiting_approval: "Waiting for approval" }[status] ?? status.replaceAll("_", " "));
const chargeLabel = (state: string) => ({ uncertain: "Charge awaiting confirmation", reserved: "Budget reserved", settled: "Usage settled", released: "Budget reservation released" }[state] ?? "Accounting needs inspection");
const dateLabel = (stamp: number) => new Date(stamp * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });

export function WorkflowCapacitySummary({ value, onInspect, locked = false }: { value: Capacity; onInspect: (id: string) => void; locked?: boolean }) {
  const occupied = value.runs.filter(run => !run.capacity_released), released = value.runs.filter(run => run.capacity_released);
  const rows = (runs: CapacityRun[]) => <ul className="org-capacity-runs">{runs.map(run => <li key={run.id}>
    <button type="button" disabled={locked} className="org-capacity-run" onClick={() => onInspect(run.id)}
      aria-label={`Inspect ${run.workflow_name} · ${dateLabel(run.created_at)}`}>
      <strong>{run.workflow_name}</strong><span>{run.project_name} / {run.channel_name} / {run.topic_name}</span>
      <small>{dateLabel(run.created_at)} · {run.requested_by_name}{!!run.topic_deleted && " · Deleted topic"}</small>
    </button><span className="org-capacity-status">{run.capacity_released ? "Released" : statusLabel(run.status)}</span>
  </li>)}</ul>;
  return <>
    <p className="org-capacity-count"><strong>{value.in_use} of {value.limit}</strong> places occupied · {value.available} available</p>
    <meter aria-label="Organization workflow allowance" min={0} max={value.limit} value={value.in_use} />
    <p>Each unfinished workflow uses one place, including a model comparison. Waiting for approval or an uncertain result also keeps its place reserved. Model calls and computations have separate execution limits.</p>
    <h3>Using places now</h3>
    {rows(occupied)}
    {!occupied.length && <p>{value.in_use ? "No occupied runs are visible in your current topics." : "No workflow places are occupied."}</p>}
    {value.hidden_in_use > 0 && <p role="status">{value.hidden_in_use} occupied {value.hidden_in_use === 1 ? "run is" : "runs are"} in topics you cannot access. An organization administrator can inspect retained runs, including deleted topics.</p>}
    <details><summary>Released places · {value.released_count} retained {value.released_count === 1 ? "run" : "runs"}</summary>
      <p>These runs do not occupy places. Their results and any held charges may still need confirmation.</p>{rows(released)}
      {value.truncated && <p>Showing the most recent retained records; {value.released_count} released runs are accessible in total.</p>}
      {!released.length && <p>No released runs are visible.</p>}
    </details>
  </>;
}

export function WorkflowCapacity({ organizationId, userId, canManage, onAuthLost }: {
  organizationId: string; userId: string; canManage: boolean; onAuthLost: () => void;
}) {
  const [open, setOpen] = useState(false), [value, setValue] = useState<Capacity | null>(null);
  const [error, setError] = useState(""), [actionError, setActionError] = useState(""), [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<WorkflowActionAttempt | null>(null), [refresh, setRefresh] = useState(0);
  const [selected, setSelected] = useState<string | null>(null), [inspection, setInspection] = useState<Inspection | null>(null);
  const [inspectionError, setInspectionError] = useState(""), [confirm, setConfirm] = useState(false);
  const working = useRef(false), root = organizationPath(organizationId);
  const fail = (cause: unknown, display: (message: string) => void) => {
    if (cause instanceof ApiError && cause.status === 401) onAuthLost(); else display(errorMessage(cause));
  };
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    const load = async () => {
      try {
        const result = await request<Capacity>(root + "/workflow-capacity", { signal: controller.signal });
        if (!controller.signal.aborted) { setValue(result); setError(""); }
      } catch (cause) { if (!aborted(cause)) { setValue(null); fail(cause, setError); } }
      if (!controller.signal.aborted) timer = schedulePoll(load, 10000, controller.signal);
    };
    void load(); return () => { controller.abort(); clearTimeout(timer); };
  }, [root, open, refresh]);
  useEffect(() => {
    if (!selected || !open) return;
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    const load = async () => {
      try {
        const result = await request<Inspection>(workflowRunPath(organizationId, selected) + "/capacity-inspection", { signal: controller.signal });
        if (!controller.signal.aborted) { setInspection(result); setInspectionError(""); }
      } catch (cause) { if (!aborted(cause)) { setInspection(null); fail(cause, setInspectionError); } }
      if (!controller.signal.aborted) timer = schedulePoll(load, 10000, controller.signal);
    };
    void load(); return () => { controller.abort(); clearTimeout(timer); };
  }, [root, selected, open, refresh]);
  useEffect(() => { setConfirm(false); }, [selected, inspection?.run.revision]);
  const inspect = (id: string) => { setSelected(id); setInspection(null); setInspectionError(""); setActionError(""); };
  const release = async () => {
    if (!inspection || working.current || pending && pending.run_id !== inspection.run.id) return;
    const retry = !!pending;
    const attempt = pending ?? captureWorkflowAction(inspection.run.id, crypto.randomUUID(), {
      operation: "release-capacity", revision: inspection.run.revision,
    });
    working.current = true; setPending(attempt); setBusy(true); setActionError("");
    try {
      await deliverWorkflowExecution(organizationId, inspection.run.topic_id, userId, attempt);
      setPending(null); setConfirm(false); setRefresh(value => value + 1);
    } catch (cause) {
      // A first, definite refusal has no unknown earlier delivery; keep uncertain retries exact.
      if (!retry && cause instanceof ApiError && cause.status >= 400 && cause.status < 500 && cause.status !== 408) {
        setPending(null); setRefresh(value => value + 1);
      }
      fail(cause, setActionError);
    } finally { working.current = false; setBusy(false); }
  };
  const close = () => { setSelected(null); setOpen(false); };
  return <div className="org-workflow-capacity"><button type="button" className="org-capacity-launcher" aria-haspopup="dialog" aria-expanded={open}
    onClick={() => setOpen(true)}>Workflow activity</button>
    {open && <ActivityDialog title="Organization workflows" onClose={close}>
      {value ? <WorkflowCapacitySummary value={value} onInspect={inspect} locked={busy || !!pending} />
        : !error && <p role="status">Loading workflow activity…</p>}
      {error && <p role="alert" className="org-error">{error}</p>}
      {pending && <p role="status">A release request is awaiting confirmation. <button type="button" disabled={busy}
        onClick={() => inspect(pending.run_id)}>Review and retry the same release</button></p>}
      <button type="button" disabled={busy} onClick={() => setRefresh(value => value + 1)}>Refresh activity</button>
      {selected && <ActivityDialog title="Inspect workflow place" onClose={() => setSelected(null)}>
        {inspectionError && <p role="alert" className="org-error">{inspectionError}</p>}
        {!inspection && !inspectionError && <p role="status">Loading run details…</p>}
        {inspection && <>
          <h3>{inspection.run.workflow_name}</h3><p>{inspection.run.project_name} / {inspection.run.channel_name} / {inspection.run.topic_name}</p>
          <p>{dateLabel(inspection.run.created_at)} · {inspection.run.requested_by_name}</p>
          <p>{inspection.run.capacity_released ? "Place released; this run does not count toward the allowance." : inspection.run.finished_at ? "Finished; this run no longer uses a place." : `Place occupied · ${statusLabel(inspection.run.status)}`}</p>
          {inspection.run.topic_deleted ? <p>This topic was deleted. Administrators can inspect its retained execution records here.</p>
            : <a href={"/organization.html?" + new URLSearchParams({ organization: organizationId, topic: inspection.run.topic_id })}>Open workflow topic</a>}
          <h4>Work recorded for this run</h4><ul className="org-capacity-nodes">{inspection.nodes.map(node => <li key={node.node_id}>
            <strong>{node.node_id}</strong> · {node.kind} · {statusLabel(node.status)}
            {node.children.map(child => <p key={child.id}>{child.kind} · {statusLabel(child.status)}{child.accounting_state && ` · ${chargeLabel(child.accounting_state)}`}</p>)}
          </li>)}</ul>
          <details><summary>Record identifiers</summary><p>Workflow run: <code>{inspection.run.id}</code></p>
            {inspection.nodes.flatMap(node => node.children.map(child => <p key={child.id}>{node.node_id}: <code>{child.id}</code></p>))}</details>
          {inspection.run.status === "outcome_unknown" && <p>An uncertain result needs inspection. Any held charges remain until usage or cleanup is verified.</p>}
          {inspection.release_blocked_reason === "active_children" && <p>Some work is still running or queued. Stop it and inspect its result before releasing this place.</p>}
          {inspection.release_blocked_reason === "missing_child_record" && <p>A child record is unavailable. An operator must inspect it before this place can be released.</p>}
          {inspection.run.status === "outcome_unknown" && !inspection.run.capacity_released && inspection.release_blocked_reason === "administrator_required" && <p>An organization administrator can release an inactive run's place after inspection.</p>}
          {canManage && inspection.can_release && !pending && !confirm && <button type="button" disabled={busy} onClick={() => setConfirm(true)}>Release workflow slot</button>}
          {confirm && !pending && inspection.can_release && <div className="org-capacity-confirm"><p>Allow another workflow to use this place? This run will not start any more work. Its uncertain result, records and any held charges remain.</p>
            <button type="button" disabled={busy} onClick={() => void release()}>Confirm slot release</button>
            <button type="button" disabled={busy} onClick={() => setConfirm(false)}>Keep place reserved</button></div>}
          {pending?.run_id === inspection.run.id && <button type="button" disabled={busy} onClick={() => void release()}>Retry same slot release</button>}
          {actionError && <p role="alert" className="org-error">{actionError}</p>}
        </>}
      </ActivityDialog>}
    </ActivityDialog>}
  </div>;
}
