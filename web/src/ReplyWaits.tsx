import React, { useState } from "react";
import { computeJobPath } from "./compute-api";
import { workingFor } from "./ReplyTimeline";
import { ApiError, ModelRun, ReplyWait, errorMessage, request, topicPath } from "./organization-api";

/** What a paused reply still waits on. Empty once nothing is pending, or for a reply that is not waiting. */
export const replyWaits = (run: ModelRun): ReplyWait[] =>
  run.answer_state?.status === "waiting" && run.agent?.thread_status !== "cancelled" ? run.answer_state.waits ?? [] : [];

/** A reply that paused for a result it is still waiting on: it stays a live card until the wait ends. */
export const waitingReply = (run: ModelRun) => run.status === "succeeded" && replyWaits(run).length > 0;

/** The card's status while it waits, naming the first thing that holds it. */
export function waitingLabel(waits: ReplyWait[]): string {
  const kinds = new Set(waits.map(wait => wait.kind));
  return kinds.has("proposal") ? "Waiting for approval" : kinds.has("compute") ? "Waiting for a computation"
    : kinds.has("agents") ? "Waiting for delegated work" : kinds.has("review") ? "Waiting for a review"
    : kinds.has("workflow") ? "Waiting for a workflow" : "Waiting";
}

const minutes = (seconds?: number) => seconds == null ? "" : seconds < 60 ? `${seconds} s` : `${Math.round(seconds / 60)} min`;

function computeSummary(compute: NonNullable<Extract<ReplyWait, { kind: "proposal" }>["compute"]>) {
  const { cpu, wall_time_seconds: wall } = compute.resources;
  return [compute.image_id, compute.language, cpu ? `${cpu} CPU` : "", wall ? `up to ${minutes(wall)}` : ""].filter(Boolean).join(" · ");
}

/**
 * The decisions a paused reply waits on, inside its card. Deciding a proposal or cancelling a computation ends that wait,
 * and the reply continues on its own once nothing is pending.
 */
export function ReplyWaits({ organizationId, topicId, run, canWrite, online, onUpdated, onAuthLost }: {
  organizationId: string; topicId: string; run: ModelRun; canWrite: boolean; online: boolean;
  onUpdated: () => void; onAuthLost: () => void;
}) {
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const computeAvailable = run.answer_state?.compute_available !== false;
  const act = (path: string, body: object) => {
    setBusy(true); setError("");
    void request(path, { method: "POST", body }).then(onUpdated).catch(cause => {
      if (cause instanceof ApiError && cause.status === 401) onAuthLost(); else setError(errorMessage(cause));
    }).finally(() => setBusy(false));
  };
  const decide = (id: string, approve: boolean) => act(topicPath(organizationId, topicId) + "/research-proposals/" + id, { approve });
  const disabled = busy || !online;
  const unavailable = <p className="org-help">This server can't run computations: no compute worker or scaler is set up
    for this organization.</p>;
  const tooLarge = <p className="org-help">No compute worker or scaler here accepts a job this large for this image, so it
    would never run.</p>;
  // Something can run it: a worker or scaler serves its image at this size. Older servers don't say, so assume it can.
  const runs = (wait: ReplyWait) => computeAvailable && (!("runnable" in wait) || wait.runnable !== false);
  return <div className="org-reply-waits" aria-label="What this reply is waiting for">
    {replyWaits(run).map(wait => {
      if (wait.kind === "proposal") {
        return <article key={wait.id} className="org-reply-wait">
          <strong>{wait.compute ? "Approve this computation?" : `Approve the proposed ${wait.action}?`}</strong>
          {wait.compute && <>
            <small>{computeSummary(wait.compute)}</small>
            <details><summary>Program</summary><pre>{wait.compute.program}{wait.compute.program_truncated ? "\n…" : ""}</pre></details>
            {!computeAvailable ? unavailable : !runs(wait) && tooLarge}
          </>}
          {canWrite && wait.status === "pending" && <div className="org-reply-wait-actions">
            {(!wait.compute || runs(wait)) && <button type="button" disabled={disabled} onClick={() => decide(wait.id, true)}>
              {wait.compute ? "Approve and run" : "Approve"}</button>}
            <button type="button" disabled={disabled} onClick={() => decide(wait.id, false)}>Reject and continue</button>
          </div>}
          {wait.status === "reviewing" && <small>Someone is approving this action.</small>}
        </article>;
      }
      if (wait.kind === "compute") {
        const waited = workingFor({ created_at: wait.created_at, started_at: null });
        return <article key={wait.id} className="org-reply-wait">
          <strong>{wait.status === "running" ? "Computation running" : !computeAvailable ? "Queued, but nothing can run it"
            : !runs(wait) ? "Queued, but too large for any worker" : "Queued · waiting for a compute worker"}</strong>
          {waited && <small>Waiting {waited}</small>}
          {wait.status !== "running" && (!computeAvailable ? unavailable : !runs(wait) && tooLarge)}
          {canWrite && <div className="org-reply-wait-actions">
            <button type="button" disabled={disabled} onClick={() => act(computeJobPath(organizationId, wait.id) + "/cancel", {})}>
              Cancel and continue</button></div>}
        </article>;
      }
      if (wait.kind === "agents") {
        return <article key="agents" className="org-reply-wait"><strong>Waiting for {wait.count} delegated
          {wait.count === 1 ? " task" : " tasks"}</strong></article>;
      }
      return <article key={wait.id} className="org-reply-wait">
        <strong>{wait.kind === "review" ? "Waiting for an independent review" : "Waiting for a workflow run"}</strong>
      </article>;
    })}
    {error && <p role="alert" className="org-error">{error}</p>}
  </div>;
}
