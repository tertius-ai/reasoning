import React, { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ActivityCard, TopicActivity } from "./TopicActivity";
import { MessageUsageDetails } from "./MessageUsageDetails";
import { RunProgress, runFailureMessage } from "./BetaConversation";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import { ComputeJobDetail } from "./ComputeConversation";
import { normalizeMathDelimiters } from "./markdownMath";
import { ApiError, BotRun, ModelRun, aborted, errorMessage, organizationPath, request, topicPath, usd } from "./organization-api";
import { WorkflowDefinition, WorkflowVersion, workflowPath } from "./workflow-api";
import "./workflow-conversation.css";
import { OutputActions } from "./ChatFeedback";
import { TransportControls, dispatchTopicCommand, useComputeActivity, useTopicCommand } from "./TopicCommands";
import { useBotActivity } from "./BotPlayback";
import { BotRunTiming } from "./BotRunTiming";
import {
  WorkflowActionSpec, WorkflowExecutionAttempt, WorkflowNodeAttempt, WorkflowRun, WorkflowRunSummary,
  canApproveWorkflow, canControlWorkflow, captureWorkflowAction, captureWorkflowStart,
  deliverWorkflowExecution, terminalWorkflow, workflowRunPath,
} from "./workflow-run-api";

type Inputs = { run_id: string; node_id: string; attempt: number; inputs: Record<string, unknown>; inputs_digest: string };
type Configuration = { enabled: boolean; worker_running: boolean; inference_enabled: boolean };
type Access = { userId: string; canWrite: boolean; canManage: boolean };

const isComparisonDefinition = (definition: WorkflowVersion["definition"]) => definition.nodes.length >= 2 && definition.nodes.every((node) =>
  node.kind === "llm" && node.depends_on.length === 0 && node.parameters.prompt === definition.nodes[0].parameters.prompt);
const isComparison = (run: WorkflowRun) => isComparisonDefinition(run.definition);

export function WorkflowActivityCard({ run, initiallyOpen, children }: {
  run: WorkflowRunSummary; initiallyOpen: boolean; children: React.ReactNode;
}) {
  const [open, setOpen] = useState(initiallyOpen);
  if (!run.comparison) return <ActivityCard title={run.name ?? "Workflow"} status={run.status}
    createdAt={run.created_at} initiallyOpen={initiallyOpen}>{children}</ActivityCard>;
  return <details className="org-message org-run-message org-comparison-message" data-status={run.status}
    open={open} onToggle={event => { if (event.target === event.currentTarget) setOpen(event.currentTarget.open); }}>
    <summary><strong className="org-comparison-question">{run.comparison.question}</strong>
      <span className="org-comparison-meta"><span className="org-comparison-models">{run.comparison.models.join(" · ")}</span>
        <time dateTime={new Date(run.created_at * 1000).toISOString()}>{new Date(run.created_at * 1000).toLocaleString()}</time></span>
      {!terminalWorkflow(run) && <span role="status">{run.status.replaceAll("_", " ")}</span>}
    </summary>{open && children}
  </details>;
}

function ComparisonAnswer({ organizationId, topicId, node, model, canWrite, onAuthLost }: {
  organizationId: string; topicId: string; node: WorkflowNodeAttempt; model: string; canWrite: boolean; onAuthLost: () => void;
}) {
  const [open, setOpen] = useState(true), [run, setRun] = useState<ModelRun | null>(null), [error, setError] = useState("");
  const [botRun, setBotRun] = useState<BotRun | null>(null);
  useEffect(() => {
    if (!node.inference_run_id && !node.bot_run_id) { setRun(null); setBotRun(null); return; }
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const value = node.bot_run_id ? await request<BotRun>(organizationPath(organizationId) + "/bot-runs/" + node.bot_run_id, { signal: controller.signal })
          : await request<ModelRun>(organizationPath(organizationId) + "/inference-runs/" + encodeURIComponent(node.inference_run_id!), { signal: controller.signal });
        if (controller.signal.aborted) return;
        if (node.bot_run_id) { setBotRun(value as BotRun); setRun(null); } else { setRun(value as ModelRun); setBotRun(null); }
        setError("");
        if (!["succeeded", "failed", "cancelled", "interrupted", "outcome_unknown"].includes(value.status)) timer = setTimeout(poll, 1500);
      } catch (cause) {
        if (controller.signal.aborted || aborted(cause)) return;
        if (cause instanceof ApiError && cause.status === 401) onAuthLost(); else setError(errorMessage(cause));
      }
    };
    void poll(); return () => { controller.abort(); clearTimeout(timer); };
  }, [organizationId, node.inference_run_id, node.bot_run_id, onAuthLost]);
  const status = botRun?.status ?? run?.status ?? node.status;
  const output = botRun?.output_text ?? run?.output_text;
  const failed = ["failed", "cancelled", "interrupted", "outcome_unknown"].includes(status);
  useEffect(() => { if (failed) setOpen(false); }, [failed]);
  return <details className="org-comparison-answer" data-status={status} open={open} onToggle={(event) => {
    if (event.target === event.currentTarget) setOpen(event.currentTarget.open);
  }}>
    <summary><span className="org-comparison-swatch" /><strong>{node.bot_run_id ? `${botRun?.bot_name ?? "Bot"} · ${botRun?.model ?? model.split("/").slice(2).join("/")}` : model}</strong>{status !== "succeeded" && <span>{status}</span>}
      {run?.diagnostics?.http_status && <span>HTTP {run.diagnostics.http_status}</span>}
      {failed && run?.actual_microusd != null && <small>{usd(run.actual_microusd)}</small>}
    </summary>
    <div className="org-comparison-body">
      {error && <p role="alert" className="org-error">{error}</p>}
      {node.error_code && <p className="org-error">{node.error_code}</p>}
      {run && <RunProgress run={run} />}
      {run?.error_code && <p className="org-error">{run.diagnostics?.message ?? runFailureMessage(run.error_code)}</p>}
      {run?.diagnostics?.action && <p className="org-help">{run.diagnostics.action}</p>}
      {botRun && ["running", "waiting_approval"].includes(status) && <p role="status" className="org-working">{botRun.phase.replaceAll("_", " ")}…</p>}
      {botRun && <BotRunTiming run={botRun} />}
      {!output ? (!["queued", "running"].includes(status) && <p role="status">{["pending", "ready", "dispatched"].includes(status) ? "Waiting for answer…" : "No answer returned."}</p>)
        : <ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[rehypeKatex]} components={{ img: () => null }} skipHtml>{normalizeMathDelimiters(output)}</ReactMarkdown>}
      {run && <MessageUsageDetails usage={run.usage} />}
      {output && <OutputActions organizationId={organizationId} topicId={topicId} targetKind="node"
        targetId={node.run_id} nodeId={node.node_id} canWrite={canWrite} onAuthLost={onAuthLost} text={output} />}
    </div>
  </details>;
}

function ComparisonResults({ organizationId, run, canWrite, onAuthLost }: { organizationId: string; run: WorkflowRun; canWrite: boolean; onAuthLost: () => void }) {
  return <section className="org-comparison-results" aria-label="Model comparison answers">
    {run.node_order.map((id) => {
      const node = run.nodes.find((item) => item.node_id === id), spec = run.definition.nodes.find((item) => item.id === id);
      return node && spec ? <ComparisonAnswer key={id} organizationId={organizationId} topicId={run.topic_id} node={node} model={String(spec.parameters.model)} canWrite={canWrite} onAuthLost={onAuthLost} /> : null;
    })}
  </section>;
}

export function WorkflowRunView({ run, userId, canWrite, canManage, locked, onAction, onInspect }: Access & {
  run: WorkflowRun; locked: boolean; onAction: (action: WorkflowActionSpec) => void; onInspect: (node: WorkflowNodeAttempt) => void;
}) {
  return <>
    <p aria-label="Workflow run status">{run.status} · revision {run.revision}</p>
    <p>{run.definition.name} · version {run.definition_version} · <code>{run.definition_digest}</code></p>
    {!!run.restore_fenced && <p role="status">Restored history only. New actions and dispatch are fenced; create a deliberate new run after recovery.</p>}
    {!!run.cancel_requested && !terminalWorkflow(run) && <p role="status">Cancellation requested. Active or uncertain children must settle before this run can finish.</p>}
    {run.status === "outcome_unknown" && <p role="status">Child outcome unknown; reservations remain held. An administrator must verify provider usage or physical worker cleanup before reconciliation.</p>}
    <ol className="org-workflow-nodes">{run.node_order.map((id) => run.nodes.find((node) => node.node_id === id)).filter((node): node is WorkflowNodeAttempt => !!node).map((node) => {
      const spec = run.definition.nodes.find((value) => value.id === node.node_id)!;
      return <li key={node.node_id}>
        <header><strong>{node.node_id}</strong><span>{node.kind} · {node.status} · attempt {node.attempt}</span></header>
        {node.error_code && <p className="org-error">{node.error_code}</p>}
        {node.kind === "approval" && <p>{String(spec.parameters.message)} <small>Requires {String(spec.parameters.required_role)} ·
          {node.approved_by ? ` ${node.approval_decision} by ${node.approved_by}` : " no decision recorded"}</small></p>}
        {canApproveWorkflow(run, node, canWrite, canManage) && <div className="org-topic-actions">
          <button disabled={locked} onClick={() => onAction({ operation: "approve", node_id: node.node_id, attempt: node.attempt, decision: "approve" })}>Approve {node.node_id} attempt {node.attempt}</button>
          <button disabled={locked} onClick={() => onAction({ operation: "approve", node_id: node.node_id, attempt: node.attempt, decision: "reject" })}>Reject {node.node_id} attempt {node.attempt}</button>
        </div>}
        <button onClick={() => onInspect(node)}>Inspect {node.node_id} attempts and results</button>
      </li>;
    })}</ol>
    <details><summary>Pinned definition and image evidence</summary><pre>{JSON.stringify({ definition: run.definition, images: run.images }, null, 2)}</pre></details>
  </>;
}

function WorkflowModelResult({ organizationId, runId, onAuthLost, onClose }: {
  organizationId: string; runId: string; onAuthLost: () => void; onClose: () => void;
}) {
  const [run, setRun] = useState<ModelRun | null>(null), [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const value = await request<ModelRun>(organizationPath(organizationId) + "/inference-runs/" + encodeURIComponent(runId), { signal: controller.signal });
        if (controller.signal.aborted) return;
        if (value.id !== runId) throw new Error("Model result does not match the selected child.");
        setRun(value); setError("");
      } catch (cause) {
        if (controller.signal.aborted || aborted(cause)) return;
        if (cause instanceof ApiError && cause.status === 401) { onAuthLost(); return; }
        setError(errorMessage(cause));
      }
      if (!controller.signal.aborted) timer = setTimeout(poll, 2000);
    };
    void poll(); return () => { controller.abort(); clearTimeout(timer); };
  }, [organizationId, runId, onAuthLost]);
  return <section aria-label="Workflow model result"><header><h4>Model child {runId}</h4><button onClick={onClose}>Close model result</button></header>
    {error && <p role="alert" className="org-error">{error}</p>}
    {!run ? <p role="status">Loading model result…</p> : <>
      <p>{run.model} · {run.status} · {run.accounting_state} · {run.actual_microusd === null ? `${usd(run.reserved_microusd)} reserved` : `${usd(run.actual_microusd)} charged`}</p>
      {run.error_code && <p className="org-error">{run.error_code}</p>}
      {run.status !== "succeeded" && run.output_text && <p>Partial output; this is not a successful dependency result.</p>}
      <pre aria-label="Workflow model output">{run.output_text || "No output yet."}</pre>
      {run.accounting_state === "uncertain" && <p>Charge unknown; an administrator must verify the provider record before reconciling this child.</p>}
    </>}
  </section>;
}

export function WorkflowRunDetail({ organizationId, topicId, runId, userId, canWrite, canManage, locked, refresh, onAction, onClose, onAuthLost, embedded = false }: Access & {
  organizationId: string; topicId: string; runId: string; locked: boolean; refresh: number;
  onAction: (action: WorkflowActionSpec) => void; onClose: () => void; onAuthLost: () => void; embedded?: boolean;
}) {
  const [run, setRun] = useState<WorkflowRun | null>(null), [error, setError] = useState("");
  const [inspection, setInspection] = useState<{ node: string; attempt: number } | null>(null);
  const [attempts, setAttempts] = useState<WorkflowNodeAttempt[]>([]), [inputs, setInputs] = useState<Inputs | null>(null);
  const [inspectionError, setInspectionError] = useState(""), [inspectionLoading, setInspectionLoading] = useState(false);
  const [child, setChild] = useState<{ kind: "compute" | "llm"; id: string } | null>(null);
  const root = workflowRunPath(organizationId, runId);
  useEffect(() => {
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const value = await request<WorkflowRun>(root, { signal: controller.signal });
        if (controller.signal.aborted) return;
        if (value.id !== runId || value.organization_id !== organizationId || value.topic_id !== topicId) throw new Error("Workflow run does not match this topic.");
        setRun(value); setError("");
      } catch (cause) {
        if (controller.signal.aborted || aborted(cause)) return;
        if (cause instanceof ApiError && cause.status === 401) { onAuthLost(); return; }
        setError(errorMessage(cause));
      }
      if (!controller.signal.aborted) timer = setTimeout(poll, 1500);
    };
    void poll(); return () => { controller.abort(); clearTimeout(timer); };
  }, [root, runId, organizationId, topicId, refresh, onAuthLost]);
  useEffect(() => {
    if (!inspection) return;
    const controller = new AbortController(); setInspectionLoading(true); setInspectionError(""); setInputs(null); setAttempts([]);
    void (async () => {
      try {
        const path = root + "/nodes/" + encodeURIComponent(inspection.node) + "/attempts";
        const records = await request<WorkflowNodeAttempt[]>(path, { signal: controller.signal });
        if (controller.signal.aborted) return;
        if (records.length > 16 || records.some((value) => value.run_id !== runId || value.organization_id !== organizationId || value.node_id !== inspection.node)) {
          throw new Error("Attempt history does not match this node.");
        }
        const selected = records.find((value) => value.attempt === inspection.attempt);
        if (!selected) throw new Error("Selected workflow attempt is unavailable.");
        setAttempts(records);
        if (selected.inputs_digest) {
          const captured = await request<Inputs>(path + "/" + inspection.attempt + "/inputs", { signal: controller.signal });
          if (controller.signal.aborted) return;
          if (captured.run_id !== runId || captured.node_id !== inspection.node || captured.attempt !== inspection.attempt || captured.inputs_digest !== selected.inputs_digest) {
            throw new Error("Input capture does not match this attempt.");
          }
          setInputs(captured);
        }
      } catch (cause) {
        if (controller.signal.aborted || aborted(cause)) return;
        if (cause instanceof ApiError && cause.status === 401) onAuthLost(); else setInspectionError(errorMessage(cause));
      } finally { if (!controller.signal.aborted) setInspectionLoading(false); }
    })();
    return () => controller.abort();
  }, [root, runId, organizationId, inspection, run?.revision, onAuthLost]);
  const inspected = attempts.find((value) => value.attempt === inspection?.attempt);
  return <section className="org-workflow-run-detail" aria-label="Selected workflow run">
    {!embedded && <header><h3>{run && isComparison(run) ? "Model comparison" : `Workflow run ${runId}`}</h3><button onClick={onClose}>{run && isComparison(run) ? "Close comparison" : "Close workflow run"}</button></header>}
    {error && <p role="alert" className="org-error">{error}</p>}
    {!run ? <p role="status">Loading workflow run…</p> : isComparison(run)
      ? <>
        <ComparisonResults organizationId={organizationId} run={run} canWrite={canWrite} onAuthLost={onAuthLost} /></>
      : <WorkflowRunView run={run} userId={userId} canWrite={canWrite} canManage={canManage}
        locked={locked || !!error} onAction={onAction} onInspect={(node) => { setInspection({ node: node.node_id, attempt: node.attempt }); setChild(null); }} />}
    {run && !isComparison(run) && run.nodes.filter(node => node.kind === "llm" && (node.inference_run_id || node.bot_run_id)).map(node =>
      <ComparisonAnswer key={node.node_id} organizationId={organizationId} topicId={topicId} node={node} canWrite={canWrite}
        model={String(run.definition.nodes.find(spec => spec.id === node.node_id)?.parameters.model)} onAuthLost={onAuthLost} />)}
    {inspection && <section aria-label="Workflow attempt inspection"><h4>{inspection.node} · attempt {inspection.attempt}</h4>
      {inspectionLoading && <p role="status">Loading attempt…</p>}
      {inspectionError && <p role="alert" className="org-error">{inspectionError}</p>}
      <nav aria-label="Node attempt history">{attempts.map((value) => <button key={value.attempt} aria-current={value.attempt === inspection.attempt ? "true" : undefined}
        onClick={() => { setInspection({ node: inspection.node, attempt: value.attempt }); setChild(null); }}>Attempt {value.attempt} · {value.status}</button>)}</nav>
      {inputs ? <details><summary>Captured dependency inputs</summary><p><code>{inputs.inputs_digest}</code></p><pre>{JSON.stringify(inputs.inputs, null, 2)}</pre></details>
        : !inspectionLoading && !inspectionError && <p>Dependency inputs have not been captured.</p>}
      {inspected?.error_code && <p className="org-error">{inspected.error_code}</p>}
      {inspected?.compute_job_id && <button onClick={() => setChild({ kind: "compute", id: inspected.compute_job_id! })}>View computation result</button>}
      {inspected?.inference_run_id && <button onClick={() => setChild({ kind: "llm", id: inspected.inference_run_id! })}>View model result</button>}
      {inspected?.approval_decision && <p>{inspected.approval_decision} by {inspected.approved_by}</p>}
      {child?.kind === "compute" && <ComputeJobDetail key={child.id} organizationId={organizationId} jobId={child.id} userId={userId}
        canWrite={false} canManage={false} onClose={() => setChild(null)} onAuthLost={onAuthLost} />}
      {child?.kind === "llm" && <WorkflowModelResult key={child.id} organizationId={organizationId} runId={child.id} onClose={() => setChild(null)} onAuthLost={onAuthLost} />}
    </section>}
  </section>;
}

export function WorkflowRuns({ organizationId, topicId, userId, canWrite, canManage, version, attempt, onAttempt, definitionBusy, onAuthLost,
  autoStartKey, onAutoStarted, toolbarTarget, onActivity, onUncap, hidePlay }: Access & {
  organizationId: string; topicId: string; version: WorkflowVersion | null; definitionBusy: boolean;
  attempt?: WorkflowExecutionAttempt; onAttempt: (value?: WorkflowExecutionAttempt) => void; onAuthLost: () => void;
  autoStartKey?: string | null; onAutoStarted?: () => void;
  toolbarTarget: HTMLDivElement | null; onActivity: (records: TopicActivity[]) => void;
  onUncap?: (definition: WorkflowDefinition) => void;
  hidePlay?: boolean;
}) {
  const [runs, setRuns] = useState<WorkflowRunSummary[]>([]), [configuration, setConfiguration] = useState<Configuration | null>(null);
  const [selected, setSelected] = useState<string | null>(() => attempt && attempt.operation !== "start" ? attempt.run_id : null);
  const [connected, setConnected] = useState(false), [busy, setBusy] = useState(false), [more, setMore] = useState(false);
  const [visibleLimit, setVisibleLimit] = useState(20);
  const [error, setError] = useState(""), [refresh, setRefresh] = useState(0);
  const [pollError, setPollError] = useState("");
  const alive = useRef(true), working = useRef(false), pending = useRef(attempt), updateAttempt = useRef(onAttempt);
  pending.current = attempt; updateAttempt.current = onAttempt;
  const root = organizationPath(organizationId), path = topicPath(organizationId, topicId) + "/workflow-runs";
  const computeActive = useComputeActivity(topicId);
  const botActive = useBotActivity(topicId);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => {
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const [config, first] = await Promise.all([
          request<Configuration>(root + "/workflow-configuration", { signal: controller.signal }),
          request<WorkflowRunSummary[]>(path, { signal: controller.signal }),
        ]);
        let page = first;
        const records = [...first];
        while (!controller.signal.aborted && page.length === 20 && records.length < visibleLimit) {
          page = await request<WorkflowRunSummary[]>(path + "?before=" + page.at(-1)!.sequence, { signal: controller.signal });
          records.push(...page);
        }
        if (controller.signal.aborted) return;
        if (records.some((value) => value.organization_id !== organizationId || value.topic_id !== topicId)) throw new Error("Run history does not match this topic.");
        setConfiguration(config); setConnected(true); setPollError("");
        setRuns(records); setMore(page.length === 20);
      } catch (cause) {
        if (controller.signal.aborted || aborted(cause)) return;
        setConnected(false);
        if (cause instanceof ApiError && cause.status === 401) { onAuthLost(); return; }
        setPollError(errorMessage(cause));
      }
      if (!controller.signal.aborted) timer = setTimeout(poll, 2000);
    };
    void poll(); return () => { controller.abort(); clearTimeout(timer); };
  }, [root, path, organizationId, topicId, refresh, visibleLimit, onAuthLost]);
  const failure = (cause: unknown) => {
    if (!alive.current) return;
    if (cause instanceof ApiError && cause.status === 401) onAuthLost(); else setError(errorMessage(cause));
  };
  const deliver = async (captured: WorkflowExecutionAttempt) => {
    if (working.current || !canWrite) return;
    working.current = true; pending.current = captured; updateAttempt.current(captured); setBusy(true); setError("");
    try {
      const receipt = await deliverWorkflowExecution(organizationId, topicId, userId, captured);
      if (!alive.current) return;
      pending.current = undefined; updateAttempt.current(undefined);
      setSelected(captured.operation === "start" ? (receipt as WorkflowRun).id : captured.run_id);
      setRefresh((value) => value + 1);
    } catch (cause) { failure(cause); }
    finally { working.current = false; if (alive.current) setBusy(false); }
  };
  useEffect(() => {
    if (!autoStartKey || autoStartKey !== (version ? `${version.workflow_id}:${version.version}` : null) || definitionBusy || pending.current
      || working.current || !canWrite || !connected || !configuration?.enabled || !configuration.worker_running) return;
    onAutoStarted?.();
    try { void deliver(captureWorkflowStart(version!, crypto.randomUUID())); } catch (cause) { failure(cause); }
  }, [autoStartKey, version, definitionBusy, canWrite, connected, configuration?.enabled, configuration?.worker_running]);
  const start = () => {
    if (!version || definitionBusy || pending.current || working.current || !canWrite || !connected || !configuration?.enabled || !configuration.worker_running) return;
    try { void deliver(captureWorkflowStart(version, crypto.randomUUID())); } catch (cause) { failure(cause); }
  };
  const action = (runId: string, spec: WorkflowActionSpec) => {
    if (!runId || pending.current || working.current || !canWrite) return;
    const message = spec.operation === "cancel" ? "Request cancellation of this workflow? Running or uncertain children retain their reservations until cleanup and usage settle." :
      spec.operation === "retry" ? "Retry unsuccessful nodes at this revision? Successful results are retained; new attempts may incur compute and model charges." :
      `${spec.decision === "approve" ? "Approve" : "Reject"} ${spec.node_id} attempt ${spec.attempt}? ${spec.decision === "approve" ? "Dependent computation or model calls may incur charges." : "This fails the node and blocks its dependents."}`;
    if (spec.operation !== "cancel" && !window.confirm(message)) return;
    try { void deliver(captureWorkflowAction(runId, crypto.randomUUID(), spec)); } catch (cause) { failure(cause); }
  };
  const rerun = async (value: WorkflowRunSummary, uncap = false) => {
    if (pending.current || working.current || !canWrite || definitionBusy || !connected) return;
    working.current = true; setBusy(true);
    try {
      const pinned = await request<WorkflowVersion>(workflowPath(organizationId, value.workflow_id) + "/versions/" + value.definition_version);
      if (!alive.current) return;
      if (pinned.workflow_id !== value.workflow_id || pinned.version !== value.definition_version || pinned.definition_digest !== value.definition_digest) {
        throw new Error("Pinned workflow version does not match the original run.");
      }
      if (uncap && onUncap) {
        onUncap({ ...pinned.definition, nodes: pinned.definition.nodes.map(node => node.kind === "llm"
          ? { ...node, parameters: { ...node.parameters, max_completion_tokens: null } } : node) });
        return;
      }
      working.current = false;
      await deliver(captureWorkflowStart(pinned, crypto.randomUUID()));
    } catch (cause) { failure(cause); } finally { working.current = false; if (alive.current) setBusy(false); }
  };
  useEffect(() => {
    onActivity(runs.map((value, index) => ({ id: "workflow:" + value.id, created_at: value.created_at, sequence: value.sequence,
      body: <WorkflowActivityCard run={value}
        initiallyOpen={index === 0 || selected === value.id || !terminalWorkflow(value)}>
        <WorkflowRunDetail organizationId={organizationId} topicId={topicId} runId={value.id} userId={userId}
          canWrite={canWrite} canManage={canManage} locked={busy || !!attempt || !connected} refresh={refresh}
          onAction={spec => action(value.id, spec)} onClose={() => setSelected(null)} onAuthLost={onAuthLost} embedded />
      </WorkflowActivityCard> })));
  }, [runs, selected, busy, attempt, connected, refresh, definitionBusy, canWrite, canManage, onActivity, onAuthLost]);
  const latest = runs[0];
  const active = runs.filter(run => !terminalWorkflow(run) && !run.cancel_requested && canControlWorkflow(run, userId, canWrite, canManage));
  const available = canWrite && !busy && !definitionBusy && connected && !!configuration?.enabled && !!configuration.worker_running;
  const canPlay = available && (!!attempt || !!version || !!latest && terminalWorkflow(latest) && !latest.restore_fenced);
  const canStop = canWrite && !busy && !definitionBusy && !attempt && (botActive || connected && (active.length > 0 || computeActive));
  const play = () => {
    if (!canPlay) return;
    if (attempt) { void deliver(attempt); return; }
    if (latest && terminalWorkflow(latest)) {
      if (["failed", "cancelled"].includes(latest.status) && canControlWorkflow(latest, userId, canWrite, canManage))
        action(latest.id, { operation: "retry", revision: latest.revision });
      else void rerun(latest);
    } else start();
  };
  const stop = async () => {
    if (!canStop) return;
    for (const run of active) {
      await deliver(captureWorkflowAction(run.id, crypto.randomUUID(), { operation: "cancel" }));
      if (pending.current) break; // An unknown acknowledgement retains its exact retry identity.
    }
  };
  useTopicCommand(topicId, command => {
    if (command === "play") play();
    if (command === "stop") void stop();
    if (command === "uncap" && latest && terminalWorkflow(latest) && onUncap) void rerun(latest, true);
  });
  return <section className="org-workflow-runs" aria-label="Workflow activity controls">
    {!connected && <p role="status">{configuration ? "Reconnecting workflow progress…" : "Loading workflow configuration…"}</p>}
    {configuration && !configuration.worker_running && <p role="status">Workflow reconciler unavailable. Existing requests remain durable.</p>}
    {error && <p role="alert" className="org-error">{error}</p>}
    {pollError && <p role="alert" className="org-error">{pollError}</p>}
    {attempt && <div className="org-workflow-pending" role="status"><p>Original {attempt.operation} request <code>{attempt.client_id}</code> is retained.
      Retry sends the same pinned version, revision, or approval attempt; it does not create a new request.</p>
      <button type="button" disabled={busy || !canWrite} onClick={() => void deliver(attempt)}>Retry original {attempt.operation} request</button>
      <button type="button" disabled={busy} onClick={() => {
        if (working.current || !window.confirm("Forget this local request ID? The server may already have accepted it. This does not cancel work or remove its results. Check the topic activity before deliberately starting new work.")) return;
        pending.current = undefined; updateAttempt.current(undefined); setError("");
      }}>Forget retained request</button>
    </div>}
    {autoStartKey && <p role="status">Starting workflow…</p>}
    {toolbarTarget && createPortal(<TransportControls {...{ canPlay, canStop, hidePlay }} onPlay={() => dispatchTopicCommand(topicId, "play")} onStop={() => dispatchTopicCommand(topicId, "stop")} />, toolbarTarget)}
    {more && <button type="button" disabled={busy} onClick={() => setVisibleLimit(limit => limit + 20)}>Earlier workflow activity</button>}
  </section>;
}
