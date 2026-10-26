import React, { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ApiError, aborted, errorMessage, organizationPath, request } from "./organization-api";
import { WorkflowActionAttempt, captureWorkflowAction, deliverWorkflowExecution, workflowRunPath } from "./workflow-run-api";
import { TopicItemDialog } from "./TopicActivity";
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

type Kind = "occupied" | "waiting" | "uncertain" | "released";
const WAITING = new Set(["awaiting_approval", "waiting_approval"]);
const kindOf = (run: CapacityRun, kind: Kind) => kind === "released" ? !!run.capacity_released : !run.capacity_released
  && (kind === "occupied" || (kind === "waiting" ? WAITING.has(run.status) : run.status === "outcome_unknown"));
const mark = (run: CapacityRun) => run.capacity_released ? ["released", "–"] : run.status === "running" ? ["running", "▶"]
  : WAITING.has(run.status) ? ["waiting", "‖"] : run.status === "outcome_unknown" ? ["uncertain", "?"] : ["queued", "·"];

/** The organization's workflow places as a list like Project files: filters with counts, a search and one row per run. */
export function WorkflowCapacitySummary({ value, onInspect, locked = false }: { value: Capacity; onInspect: (id: string) => void; locked?: boolean }) {
  const [kind, setKind] = useState<Kind>("occupied"), [query, setQuery] = useState("");
  const needle = query.trim().toLocaleLowerCase();
  const shown = value.runs.filter(run => kindOf(run, kind) && [run.workflow_name, run.project_name, run.channel_name, run.topic_name, run.requested_by_name]
    .some(text => text.toLocaleLowerCase().includes(needle)));
  const count = (which: Kind) => value.runs.filter(run => kindOf(run, which)).length;
  return <>
    <p className="org-capacity-count"><strong>{value.in_use} of {value.limit}</strong> places in use · {value.available} available</p>
    <meter aria-label="Organization workflow allowance" min={0} max={value.limit} value={value.in_use} />
    <nav className="org-capacity-kinds" aria-label="Kind of run">
      {([["occupied", "In use"], ["waiting", "Waiting"], ["uncertain", "Needs inspection"], ["released", "Released"]] as const).map(([which, label]) =>
        <button key={which} type="button" aria-pressed={kind === which} onClick={() => setKind(which)}>{label} <span>{count(which)}</span></button>)}
    </nav>
    <input type="search" aria-label="Search workflows and topics" placeholder="Search workflows and topics…" value={query} onChange={event => setQuery(event.target.value)} />
    {kind === "released" && <p className="org-help">These runs do not occupy places. Their results and any held charges may still need confirmation.</p>}
    {shown.length ? <ul className="org-capacity-runs">{shown.map(run => {
      const [tone, symbol] = mark(run);
      return <li key={run.id}><span className="org-capacity-mark" data-tone={tone} aria-hidden="true">{symbol}</span>
        <span className="org-capacity-text"><strong>{run.workflow_name}</strong>
          <small>{run.project_name} / {run.channel_name} / {run.topic_name} · {run.requested_by_name} · {dateLabel(run.created_at)}{!!run.topic_deleted && " · Deleted topic"}</small></span>
        <span className="org-capacity-status">{run.capacity_released ? "Released" : statusLabel(run.status)}</span>
        <button type="button" disabled={locked} onClick={() => onInspect(run.id)} aria-label={`Inspect ${run.workflow_name} · ${dateLabel(run.created_at)}`}>Inspect</button>
      </li>;
    })}</ul> : <p className="org-help">{needle ? "No runs match." : kind === "released" ? "No released runs are visible."
      : kind !== "occupied" ? "No runs here." : value.in_use ? "No occupied runs are visible in your current topics." : "No workflow places are occupied."}</p>}
    {value.hidden_in_use > 0 && <p role="status" className="org-help">{value.hidden_in_use} occupied {value.hidden_in_use === 1 ? "run is" : "runs are"} in topics you cannot access. An organization administrator can inspect retained runs, including deleted topics.</p>}
    {kind === "released" && value.truncated && <p className="org-help">Showing the most recent retained records; {value.released_count} released runs are accessible in total.</p>}
    <p className="org-help">Each unfinished workflow, including a model comparison, uses one place until it finishes. Waiting for approval or an uncertain result keeps its place. Model calls and computations have separate limits.</p>
  </>;
}

/**
 * Workflow activity in a workflow topic's details card: the organization's places in use out of its allowance, opening a dialog
 * that lists them and inspects one run in place, where an administrator can release a stuck run's place.
 */
export function WorkflowCapacity({ organizationId, userId, canManage, onAuthLost, menuTarget = null }: {
  organizationId: string; userId: string; canManage: boolean; onAuthLost: () => void; menuTarget?: HTMLElement | null;
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
  // The card's count is read when the topic opens and each time the card opens; the dialog keeps it current while open.
  useEffect(() => {
    const card = menuTarget?.closest("details");
    if (!card) return;
    const opened = () => { if (card.open) setRefresh(value => value + 1); };
    card.addEventListener("toggle", opened); return () => card.removeEventListener("toggle", opened);
  }, [menuTarget]);
  useEffect(() => {
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    const load = async () => {
      try {
        const result = await request<Capacity>(root + "/workflow-capacity", { signal: controller.signal });
        if (!controller.signal.aborted) { setValue(result); setError(""); }
      } catch (cause) { if (!aborted(cause)) { if (open) setValue(null); fail(cause, setError); } }
      if (!controller.signal.aborted && open) timer = schedulePoll(load, 10000, controller.signal);
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
  const back = () => { setSelected(null); setInspection(null); setInspectionError(""); setActionError(""); };
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
  return <>
    {menuTarget && createPortal(<button type="button" className="org-topic-card-row" onClick={() => {
      // Choosing an item closes the details card, as choosing a menu item does, and opens the item's dialog.
      menuTarget.closest("details")?.removeAttribute("open");
      setOpen(true);
    }}><span className="org-account-mark" aria-hidden="true">⟳</span><span>Workflow activity</span>
      <small className="org-topic-card-count">{value && `${value.in_use} of ${value.limit}`}<span aria-hidden="true"> ›</span></small></button>, menuTarget)}
    {open && <TopicItemDialog title="Workflow activity" onClose={close}><div className="org-workflow-capacity">
      {!selected && <>
        {value ? <WorkflowCapacitySummary value={value} onInspect={inspect} locked={busy || !!pending} />
          : !error && <p role="status">Loading workflow activity…</p>}
        {error && <p role="alert" className="org-error">{error}</p>}
        {pending && <p role="status">A release request is awaiting confirmation. <button type="button" disabled={busy}
          onClick={() => inspect(pending.run_id)}>Review and retry the same release</button></p>}
      </>}
      {selected && <section className="org-capacity-inspection" aria-label="Inspect workflow place">
        <button type="button" className="org-capacity-back" onClick={back}>‹ All runs</button>
        {inspectionError && <p role="alert" className="org-error">{inspectionError}</p>}
        {!inspection && !inspectionError && <p role="status">Loading run details…</p>}
        {inspection && <>
          <h3>{inspection.run.workflow_name}</h3>
          <p className="org-help">{inspection.run.project_name} / {inspection.run.channel_name} / {inspection.run.topic_name} · {inspection.run.requested_by_name} · {dateLabel(inspection.run.created_at)}</p>
          <p>{inspection.run.capacity_released ? "Place released; this run does not count toward the allowance." : inspection.run.finished_at ? "Finished; this run no longer uses a place." : `Place occupied · ${statusLabel(inspection.run.status)}`}</p>
          <h4>Work recorded for this run</h4><ul className="org-capacity-nodes">{inspection.nodes.map(node => <li key={node.node_id}>
            <span><strong>{node.node_id}</strong> · {node.kind}</span><span className="org-capacity-status">{statusLabel(node.status)}</span>
            {node.children.map(child => <p key={child.id}>{child.kind} · {statusLabel(child.status)}{child.accounting_state && ` · ${chargeLabel(child.accounting_state)}`}</p>)}
          </li>)}</ul>
          {inspection.run.status === "outcome_unknown" && <p className="org-capacity-warning">An uncertain result needs inspection. Any held charges remain until usage or cleanup is verified.</p>}
          {inspection.release_blocked_reason === "active_children" && <p>Some work is still running or queued. Stop it and inspect its result before releasing this place.</p>}
          {inspection.release_blocked_reason === "missing_child_record" && <p>A child record is unavailable. An operator must inspect it before this place can be released.</p>}
          {inspection.run.status === "outcome_unknown" && !inspection.run.capacity_released && inspection.release_blocked_reason === "administrator_required" && <p>An organization administrator can release an inactive run's place after inspection.</p>}
          {inspection.run.topic_deleted ? <p>This topic was deleted. Administrators can inspect its retained execution records here.</p> : null}
          <div className="org-capacity-actions">
            {canManage && inspection.can_release && !pending && !confirm && <button type="button" disabled={busy} onClick={() => setConfirm(true)}>Release workflow slot</button>}
            {!inspection.run.topic_deleted && <a href={"/organization.html?" + new URLSearchParams({ organization: organizationId, topic: inspection.run.topic_id })}>Open workflow topic</a>}
          </div>
          {confirm && !pending && inspection.can_release && <div className="org-capacity-confirm"><p>Allow another workflow to use this place? This run will not start any more work. Its uncertain result, records and any held charges remain.</p>
            <button type="button" disabled={busy} onClick={() => void release()}>Confirm slot release</button>
            <button type="button" disabled={busy} onClick={() => setConfirm(false)}>Keep place reserved</button></div>}
          {pending?.run_id === inspection.run.id && <button type="button" disabled={busy} onClick={() => void release()}>Retry same slot release</button>}
          {actionError && <p role="alert" className="org-error">{actionError}</p>}
          <details><summary>Record identifiers</summary><p>Workflow run: <code>{inspection.run.id}</code></p>
            {inspection.nodes.flatMap(node => node.children.map(child => <p key={child.id}>{node.node_id}: <code>{child.id}</code></p>))}</details>
        </>}
      </section>}
    </div></TopicItemDialog>}
  </>;
}
