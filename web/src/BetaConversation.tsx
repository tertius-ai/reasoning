import { PollBackoff, schedulePoll } from "./polling";
import React, { useEffect, useRef, useState } from "react";
import { MessageUsageDetails } from "./MessageUsageDetails";
import { TopicActivity } from "./TopicActivity";
import { submitModelAttempt } from "./BotConversation";
import { OutputActions } from "./ChatFeedback";
import { createPortal } from "react-dom";
import { TransportControls, dispatchTopicCommand, useComputeActivity, useTopicCommand } from "./TopicCommands";
import { useBotActivity } from "./BotPlayback";
import {
  ApiError, ModelAttempt, ModelRun, PendingModelAttempt, UNDESCRIBED_ERROR,
  aborted, decimalBudget, errorMessage, organizationPath, request, sharedRequest, topicPath, usd,
} from "./organization-api";

/** A 4xx answer other than 408 means the server did not accept the request. */
export const definitiveRejection = (cause: unknown) =>
  cause instanceof ApiError && cause.status >= 400 && cause.status < 500 && cause.status !== 408;

/** Why acceptance of a reply request is unknown, without claiming either outcome. */
export function unconfirmedReason(cause: unknown): string {
  if (!(cause instanceof ApiError)) return "The connection failed before the server answered.";
  if (cause.status === 408) return "The server timed out (HTTP 408) before confirming it.";
  const detail = cause.message && cause.message !== UNDESCRIBED_ERROR ? ": " + cause.message.slice(0, 200) : "";
  return `The server reported an error (HTTP ${cause.status}${detail}) before confirming it.`;
}

/** Exactly the submitted parameters, so a retry replays the same request. */
const modelAttemptRequest = ({ client_id, models, max_completion_tokens, context_message_limit, through_sequence, effort }: ModelAttempt): ModelAttempt =>
  ({ client_id, models, max_completion_tokens, context_message_limit, through_sequence, effort });

/**
 * Submits a reply request and reports what is known about its acceptance:
 * cleared when accepted or definitively rejected, otherwise kept with its
 * client ID so a retry returns an already accepted run instead of a new one.
 * The outcome is always reported, because the caller's state outlives the view.
 */
export async function requestModelReply(organizationId: string, topicId: string, attempt: ModelAttempt,
  onAttempt: (attempt?: PendingModelAttempt) => void): Promise<void> {
  const body = modelAttemptRequest(attempt);
  try {
    await submitModelAttempt(organizationId, topicId, body);
    onAttempt(undefined);
  } catch (cause) {
    onAttempt(definitiveRejection(cause) ? undefined : { ...body, unconfirmed: { reason: unconfirmedReason(cause) } });
    throw cause;
  }
}

export function PendingReplyRequest({ attempt, busy, retrying, retryBlocked, onRetry, onDismiss }: {
  attempt: PendingModelAttempt; busy: boolean; retrying: boolean; retryBlocked: string; onRetry: () => void; onDismiss: () => void;
}) {
  const model = attempt.models[0];
  if (!attempt.unconfirmed) return <p role="status" className="org-working">Sending the reply request to {model}…</p>;
  return <div className="org-pending-request" role="group" aria-label="Unconfirmed reply request">
    <div>
      <strong>Reply request not confirmed</strong>
      <p>Your message was saved, but the server did not confirm the reply request to {model}, so it may or may not
        have started. {attempt.unconfirmed.reason}</p>
      <small>Conversation context ends at message #{attempt.through_sequence}{attempt.effort ? " · effort " + attempt.effort : ""}</small>
      <small>Retry sends the same request: if the server already accepted it, that reply continues and no second request
        is made. Dismissing clears only this browser's pending retry; it does not cancel an accepted reply or its charges.</small>
      {retryBlocked && <small role="status">{retryBlocked}</small>}
    </div>
    <div className="org-pending-actions">
      <button type="button" disabled={busy || !!retryBlocked} onClick={onRetry}>{retrying ? "Retrying…" : "Retry same request"}</button>
      <button type="button" disabled={busy} onClick={onDismiss}>Dismiss pending retry</button>
    </div>
  </div>;
}

export function runFailureMessage(code: string): string {
  const messages: Record<string, string> = {
    agent_empty_response: "The model returned no visible answer, even after bounded recovery.",
    agent_invalid_tool_arguments: "The model returned an invalid tool request. No invalid request was executed.",
    agent_tool_limit: "The model requested more tools after the turn's tool limit was reached.",
    agent_content_filtered: "The provider filtered the response before a complete answer was returned.",
    agent_provider_finish: "The model stopped without a complete answer or executable tool request.",
    output_token_limit: "The output allowance was exhausted. Any available partial answer is preserved.",
    agent_incomplete: "The research turn did not complete. Inspect Context and the inference ledger before resuming.",
    agent_execution_error: "The research executor hit an internal error. Retained work and charges are preserved; copy topic JSON for diagnosis.",
    execution_not_authorized: "Execution stopped before another call could be authorized. Check budgets, goal limits, and access before resuming.",
    agent_charge_unknown: "An agent's provider charge or local execution outcome is unknown. Inspect its retained record before resuming.",
    agent_handoff_refused: "Agent results are retained, but continuation was refused. Check access, spending policy, and context before resuming.",
    agent_handoff_error: "Agent results are retained. An internal handoff error paused this request; copy topic JSON for diagnosis.",
    agent_response_failed: "The agent response failed. Inspect its error and accounting before resuming the retained request.",
  };
  return messages[code] ?? code.replaceAll("_", " ");
}

function ReconcileCost({ root, run, onUpdated, onAuthLost }: {
  root: string; run: ModelRun; onUpdated: () => void; onAuthLost: () => void;
}) {
  const [cost, setCost] = useState("");
  const [checked, setChecked] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  return <details className="org-reconcile"><summary>Reconcile unknown charge</summary>
    <p className="org-help">Check the OpenRouter activity record for generation {run.provider_generation_id ?? "(ID unavailable)"} before changing this reservation. Never assume cancellation was free.</p>
    <form onSubmit={(event) => {
      event.preventDefault(); setBusy(true); setError("");
      void (async () => {
        try {
          await request(root + "/inference-runs/" + run.id + "/cost", { method: "PUT", body: { actual_microusd: decimalBudget(cost) } });
          onUpdated();
        } catch (cause) {
          if (cause instanceof ApiError && cause.status === 401) onAuthLost();
          else setError(errorMessage(cause));
        } finally { setBusy(false); }
      })();
    }}>
      <label>Verified provider cost (USD)<input required inputMode="decimal" value={cost} disabled={busy} onChange={(event) => setCost(event.target.value)} /></label>
      <label className="org-model-choice"><input type="checkbox" required checked={checked} disabled={busy} onChange={(event) => setChecked(event.target.checked)} />I checked the provider record for this run.</label>
      {error && <p role="alert" className="org-error">{error}</p>}
      <button disabled={busy || !checked}>Record verified cost</button>
    </form>
  </details>;
}

export function BetaConversation({ organizationId, topicId, userId, canWrite, canManage, online, attempt, onAttempt, onAuthLost, onActivity, toolbarTarget, hidePlay, wake }: {
  organizationId: string; topicId: string; userId: string; canWrite: boolean; canManage: boolean;
  online: boolean; attempt?: PendingModelAttempt; onAttempt: (attempt?: PendingModelAttempt) => void; onAuthLost: () => void;
  onActivity: (entries: TopicActivity[]) => void;
  toolbarTarget?: HTMLElement | null;
  hidePlay?: boolean;
  /** Changes when the transcript gains a message, so a new request is observed at the active cadence. */
  wake?: number;
}) {
  const [runs, setRuns] = useState<ModelRun[]>([]);
  const [threads, setThreads] = useState<{ root_run: string; status: string; reason?: string | null; requested_by: string; tasks: number }[]>([]);
  const [goal, setGoal] = useState<{ id: string; status: string; owned_by: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [error, setError] = useState("");
  const [connected, setConnected] = useState(false);
  const [pollError, setPollError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const alive = useRef(true);
  const root = organizationPath(organizationId);
  const path = topicPath(organizationId, topicId) + "/inference-runs";
  const computeActive = useComputeActivity(topicId);
  const botActive = useBotActivity(topicId);

  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => {
    const controller = new AbortController(), backoff = new PollBackoff(1000, 5000); let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      let active = true;
      try {
        const [records, agents, currentGoal] = await Promise.all([
          request<ModelRun[]>(path, { signal: controller.signal }),
          request<typeof threads>(topicPath(organizationId, topicId) + "/agents", { signal: controller.signal }),
          // Research controls read the same goal; one request serves both views.
          sharedRequest<typeof goal>(topicPath(organizationId, topicId) + "/goal", { maxAge: 500, signal: controller.signal }),
        ]);
        if (controller.signal.aborted) return;
        setRuns(records); setThreads(agents); setGoal(currentGoal); setConnected(true); setPollError("");
        active = records.some(run => ["queued", "running"].includes(run.status)) || agents.some(thread => thread.status === "active")
          || currentGoal?.status === "active";
      } catch (cause) {
        if (aborted(cause)) return;
        setConnected(false);
        if (cause instanceof ApiError && cause.status === 401) { onAuthLost(); return; }
        setPollError("Unable to refresh agent activity; reconnecting. " + errorMessage(cause));
      }
      if (!controller.signal.aborted) timer = schedulePoll(poll, backoff.next(active), controller.signal);
    };
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [path, refresh, onAuthLost, attempt, wake]);

  const failed = (cause: unknown) => {
    if (cause instanceof ApiError && cause.status === 401) onAuthLost();
    else setError(errorMessage(cause));
  };
  const retryBlocked = !canWrite ? "Retry needs write access to this topic."
    : !online ? "Retry is available when this browser is back online."
    : !connected ? "Reconnecting to the server before retry is available…" : "";
  const retry = async () => {
    if (!attempt?.unconfirmed || busy || retryBlocked) return;
    setBusy(true); setRetrying(true); setError("");
    try {
      await requestModelReply(organizationId, topicId, attempt, onAttempt);
      if (alive.current) setRefresh((value) => value + 1);
    } catch (cause) {
      // An unconfirmed outcome is already shown with its new reason.
      if (alive.current && definitiveRejection(cause)) failed(cause);
    } finally { if (alive.current) { setBusy(false); setRetrying(false); } }
  };
  const controllable = threads.filter(thread => canWrite && (canManage || thread.requested_by === userId));
  const activeRuns = runs.filter(run => ["queued", "running"].includes(run.status) && !run.cancel_requested
    && canWrite && (canManage || run.requested_by === userId));
  const controlledGoal = goal && canWrite && (canManage || goal.owned_by === userId) ? goal : null;
  const canPlay = !busy && online && connected && canWrite && (!!attempt?.unconfirmed || controllable.some(thread => thread.status === "paused")
    || !!controlledGoal && ["paused", "blocked"].includes(controlledGoal.status));
  const canStop = !busy && online && (botActive || connected && (computeActive || activeRuns.length > 0
    || controllable.some(thread => thread.status === "active") || controlledGoal?.status === "active"));
  const pausedReason = controllable.find(thread => thread.status === "paused" && thread.reason)?.reason;
  const transport = async (playing: boolean) => {
    if (playing && !canPlay || !playing && !canStop) return;
    if (playing && attempt?.unconfirmed) { await retry(); return; }
    setBusy(true); setError("");
    try {
      const selected = controllable.filter(thread => thread.status === (playing ? "paused" : "active"));
      for (const thread of selected) await request(topicPath(organizationId, topicId) + "/agents/" + thread.root_run + "/control", {
        method: "POST", body: { status: playing ? "active" : "paused" },
      });
      if (controlledGoal && (playing ? ["paused", "blocked"].includes(controlledGoal.status) : controlledGoal.status === "active"))
        await request(topicPath(organizationId, topicId) + "/goal/" + controlledGoal.id, { method: "PATCH", body: { status: playing ? "active" : "paused" } });
      // Thread stop keeps its continuation identity. Orphan responses use the
      // existing cancellation API, whose accounting settles independently.
      if (!playing && !selected.length) for (const run of activeRuns) await request(root + "/inference-runs/" + run.id + "/cancel", { method: "POST", body: {} });
      if (alive.current) setRefresh(value => value + 1);
    } catch (cause) { if (alive.current) failed(cause); }
    finally { if (alive.current) setBusy(false); }
  };
  useTopicCommand(topicId, command => { if (command === "play" || command === "stop") void transport(command === "play"); });
  const visibleRuns = runs.filter((run) => (run.context_through_sequence !== 0 || run.agent) && (run.status !== "succeeded" || !run.message_id));
  useEffect(() => { onActivity(visibleRuns.map(run => ({ id: "run:" + run.id, created_at: run.created_at ?? run.started_at ?? 0,
    after_sequence: run.context_through_sequence || undefined,
    body: <BetaRunCard key={run.id} root={root} organizationId={organizationId} topicId={topicId} run={run} canManage={canManage}
      canReact={canWrite}
      onUpdated={() => setRefresh(value => value + 1)} onAuthLost={onAuthLost} />,
  }))); }, [runs, busy, online, connected, canWrite, canManage, userId, root, organizationId, topicId, onActivity, onAuthLost]);
  return <section className="org-beta" aria-label="Agent activity">
    {toolbarTarget && createPortal(<TransportControls {...{ canPlay, canStop, hidePlay }}
      playTitle={attempt?.unconfirmed ? "Retry the unconfirmed reply request"
        : pausedReason ? "Resume retained request — " + runFailureMessage(pausedReason) : undefined}
      onPlay={() => dispatchTopicCommand(topicId, "play")} onStop={() => dispatchTopicCommand(topicId, "stop")} />, toolbarTarget)}
    {attempt && <PendingReplyRequest attempt={attempt} busy={busy} retrying={retrying} retryBlocked={retryBlocked}
      onRetry={() => void retry()} onDismiss={() => {
        if (window.confirm("Dismiss the pending retry? This clears only the retry kept in this browser. Your saved message stays, "
          + "and a reply the server already accepted keeps running with its accounting unchanged. Use Stop current work (■) to cancel a running reply.")) {
          onAttempt(undefined); setError("");
        }
      }} />}
    {error && <p role="alert" className="org-error">{error}</p>}
    {pollError && <p role="alert" className="org-error">{pollError}</p>}
  </section>;
}

export function RunProgress({ run }: { run: ModelRun }) {
  if (!["queued", "running"].includes(run.status)) return null;
  const event = run.activity?.at(-1);
  const progress = event?.summary ?? (event ? event.phase === "round" ? `Round ${event.round} · working`
    : event.tool ? `${event.phase === "tool_completed" ? "Used" : "Using"} ${event.tool.replaceAll("_", " ")}` : event.phase.replaceAll("_", " ") : "Working");
  return <p role="status" className="org-working" aria-label="Research progress">{progress}…</p>;
}

function BetaRunCard({ root, organizationId, topicId, run, canManage, canReact, onUpdated, onAuthLost }: {
  root: string; organizationId: string; topicId: string; run: ModelRun; canManage: boolean; canReact: boolean;
  onUpdated: () => void; onAuthLost: () => void;
}) {
  const active = ["queued", "running"].includes(run.status), [open, setOpen] = useState(active);
  useEffect(() => { if (!active) setOpen(false); }, [active]);
  return <details data-run-id={run.id} data-status={run.status} className="org-model-run org-run-compact" open={open}
    onToggle={event => { if (event.target === event.currentTarget) setOpen(event.currentTarget.open); }}>
    <summary><strong>{run.agent ? `${run.agent.role.replaceAll("_", " ")} · ` : ""}{run.model}</strong><span role="status">{run.cancel_requested && active ? "Cancelling…" : run.status}</span>
      {!active && run.error_code && <span title={run.diagnostics?.message}>{run.diagnostics?.http_status ? `HTTP ${run.diagnostics.http_status}` : run.error_code.replaceAll("_", " ")}</span>}
      {run.actual_microusd != null && <small>{usd(run.actual_microusd)}</small>}
    </summary>
    <RunProgress run={run} />
    {run.output_text && <pre aria-label={"Streaming response from " + run.model}>{run.output_text}</pre>}
    {!active && run.output_text && <OutputActions {...{ organizationId, topicId, onAuthLost }} targetKind="run" targetId={run.id} canWrite={canReact} text={run.output_text} />}
    <MessageUsageDetails usage={run.usage} />
    {run.error_code && <p className="org-error">{run.diagnostics?.message ?? runFailureMessage(run.error_code)}</p>}
    {run.diagnostics && <p className="org-help">{run.diagnostics.action}</p>}
    {run.accounting_state === "uncertain" && <p className="org-help">Charge unknown; internal hold {usd(run.reserved_microusd)} is not a charge. Check provider activity.</p>}
    {active && run.cost_progress && <small className="org-help">Confirmed {usd(run.cost_progress.confirmed_microusd)} · next-call estimate {usd(run.cost_progress.next_call_estimate_microusd)}</small>}
    {run.accounting_state === "uncertain" && canManage && <ReconcileCost root={root} run={run} onUpdated={onUpdated} onAuthLost={onAuthLost} />}
  </details>;
}
