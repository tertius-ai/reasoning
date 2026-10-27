import { PollBackoff, startPolling } from "./polling";
import { LIVE_SAFETY_POLL_MS, subscribeTopicEvents } from "./topicEvents";
// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import React, { useEffect, useState } from "react";
import { ActivityCard, CardStop, TopicActivity } from "./TopicActivity";
import { aborted, ApiError, BotRun, errorMessage, ModelAttempt, organizationPath, request, topicPath } from "./organization-api";
import { canStopBot, publishBotActivity } from "./BotPlayback";
import { BotRunTiming } from "./BotRunTiming";
import { useTopicCommand } from "./TopicCommands";

export function submitModelAttempt(organizationId: string, topicId: string, attempt: ModelAttempt) {
  const model = attempt.models[0];
  return model.startsWith("bot/") ? request(topicPath(organizationId, topicId) + "/bot-runs", { method: "POST", body: {
    model, client_id: attempt.client_id, through_sequence: attempt.through_sequence, effort: attempt.effort ?? null,
  } }) : request(topicPath(organizationId, topicId) + "/inference-runs", { method: "POST", body: attempt });
}

export function BotRunStatus({ run }: { run: BotRun }) {
  return <p role="status" className={run.status === "running" ? "org-working" : undefined}>
    {run.error_code === "gateway_unavailable" || run.error_code === "gateway_not_attached"
      ? "Bot tools could not connect. Ask its owner to update the checkout, run make bot-doctor, and restart make bot-run."
      : run.error_code === "approval_transport_ended" ? "Bot transport ended before execution. Review the retained request in People, stop this invocation, and submit a new request."
      : run.error_code === "requester_not_permitted" ? run.status === "cancelled"
        ? "Not run: the bot owner's “Who can use this bot” setting does not include you. Ask its owner, or use your own bot."
        : "Stopped: the bot owner's “Who can use this bot” setting no longer includes you. The local outcome is unknown; ask the bot owner to inspect its retained invocation."
      : run.status === "waiting_approval" ? run.approval_by === "requester"
        ? "Waiting for the requesting member to approve the exact GitHub issue preview in People."
        : "Waiting for the bot owner's approval in People."
      : run.status === "outcome_unknown" ? run.error_code === "stop_requested" || run.error_code === "cancelled"
        ? "Stopping: waiting for the bot's runner to confirm that its CLI ended. Until it does, the local outcome is unknown."
        : "Runner interrupted. No automatic retry; ask the bot owner to reconcile its local receipt."
      : run.status === "cancelled" && (run.error_code === "stop_requested" || run.error_code === "cancelled") ? "Stopped."
        : run.status === "running" ? (run.activity?.at(-1)?.label ?? run.phase.replaceAll("_", " ")) + "…" : run.error_code?.replaceAll("_", " ") ?? run.status}
  </p>;
}

export function BotConversation({ organizationId, topicId, names, userId, canWrite, canManage, onActivity, onAuthLost, wake }: {
  organizationId: string; topicId: string; names: Record<string, string>;
  userId: string; canWrite: boolean; canManage: boolean;
  onActivity: (entries: TopicActivity[]) => void; onAuthLost: () => void;
  /** Changes when the transcript gains a message, so a new invocation is observed at the active cadence. */
  wake?: number;
}) {
  const [runs, setRuns] = useState<BotRun[]>([]), [connected, setConnected] = useState(false);
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController(), backoff = new PollBackoff(2000, 8000);
    // A live topic event stream delivers invocations; polling then only runs as a slow safety net, and a poll's
    // result is dropped if the stream delivered anything since that poll started.
    let live = false, streamed = 0, poller: { wake(): void } | null = null;
    const poll = async (): Promise<number | null> => {
      let active = true, failed = false;
      const version = streamed;
      try {
        const runs = await request<BotRun[]>(topicPath(organizationId, topicId) + "/bot-runs", { signal: controller.signal });
        if (controller.signal.aborted) return null;
        if (version === streamed) { setRuns(runs); setConnected(true); }
        active = runs.some(run => ["queued", "running", "waiting_approval"].includes(run.status));
      } catch (cause) {
        if (!aborted(cause)) setConnected(false);
        if (!aborted(cause) && cause instanceof ApiError && cause.status === 401) onAuthLost();
        else if (!aborted(cause)) onActivity([{ id: "bot:connection", created_at: 0,
          body: <p className="org-help" role="status">Bot activity unavailable: {errorMessage(cause)}</p> }]);
        failed = true;
      }
      return controller.signal.aborted ? null : live && !failed ? LIVE_SAFETY_POLL_MS : backoff.next(active);
    };
    const unsubscribe = subscribeTopicEvents(organizationId, topicId, {
      status: status => { const next = status === "live"; if (live && !next) poller?.wake(); live = next; },
      bots: runs => { streamed += 1; setRuns(runs); setConnected(true); },
    });
    poller = startPolling(poll, controller.signal, live && streamed ? LIVE_SAFETY_POLL_MS : 0);
    return () => { unsubscribe(); controller.abort(); };
  }, [organizationId, topicId, names, onActivity, onAuthLost, wake]);
  const stopRun = async (run: BotRun) => {
    setBusy(true); setError("");
    try {
      const stopped = await request<BotRun>(organizationPath(organizationId) + "/bot-runs/" + run.id + "/stop", { method: "POST", body: {} });
      setRuns(current => current.map(item => item.id === stopped.id ? stopped : item));
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 401) onAuthLost(); else setError(errorMessage(cause));
    } finally { setBusy(false); }
  };
  useEffect(() => { if (connected) onActivity(runs.filter(run => run.status !== "succeeded").map(run => ({
    id: "bot:" + run.id, created_at: run.created_at, after_sequence: run.context_through_sequence || undefined,
    body: <ActivityCard title={`${names[run.bot_id] ?? "Bot"} · ${run.model}`} status={run.status} createdAt={run.created_at}
      initiallyOpen={!["failed", "cancelled", "outcome_unknown", "succeeded"].includes(run.status)}
      action={canStopBot(run, userId, canWrite, canManage)
        ? <CardStop label="Stop this bot reply" disabled={busy || !connected} onStop={() => void stopRun(run)} /> : undefined}>{run.status !== "succeeded" && <BotRunStatus run={run} />}
      <BotRunTiming run={run} />
    </ActivityCard>,
  }))); }, [runs, connected, names, onActivity, busy, userId, canWrite, canManage]);
  const stoppable = runs.filter(run => canStopBot(run, userId, canWrite, canManage));
  useEffect(() => {
    publishBotActivity(topicId, connected && !busy && stoppable.length > 0);
    return () => publishBotActivity(topicId, false);
  }, [topicId, connected, busy, stoppable.length]);
  useTopicCommand(topicId, command => {
    if (command !== "stop" || !connected || busy || !stoppable.length) return;
    void (async () => { for (const run of stoppable) await stopRun(run); })();
  });
  return error ? <p className="org-error" role="alert">Unable to stop bot activity: {error}</p> : null;
}
