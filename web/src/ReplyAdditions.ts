// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
/**
 * Messages added to a running model reply. A reply reads its context when it is requested; a later message of the
 * same topic joins it only when added, and the reply reads it before its next provider call. A reply and its agent
 * continuations form one thread, named by its root run, which answers a topic message.
 */
import type { Message, ModelRun, ReplyAddition } from "./organization-api";

export type ReplyThread = { root: ModelRun; active: boolean; additions: ReplyAddition[] };

export const threadRoot = (run: ModelRun) => run.agent?.root_run ?? run.id;
const working = (run: ModelRun) => ["queued", "running"].includes(run.status) && !run.cancel_requested;

/** Replies to topic messages, each with whether any run of its thread still works. */
export function replyThreads(runs: ModelRun[]): ReplyThread[] {
  return runs.filter(run => run.context_through_sequence > 0 && threadRoot(run) === run.id).map(root => ({
    root, additions: root.additions ?? [],
    active: runs.some(run => threadRoot(run) === root.id && working(run)),
  }));
}

/** The running reply to the message at ``sequence``, which its ↩ joins instead of replying. */
export const threadAnswering = (threads: ReplyThread[], sequence: number) =>
  threads.find(thread => thread.active && thread.root.context_through_sequence === sequence);

/** The latest running reply to an earlier question, which a later message can move into. */
export const moveTarget = (threads: ReplyThread[], message: Message) => threads
  .filter(thread => thread.active && thread.root.context_through_sequence < message.sequence)
  .sort((a, b) => b.root.context_through_sequence - a.root.context_through_sequence)[0];

/** Why a posted message cannot move: only a message whose own reply has not started can. */
export function moveBlocked(runs: ModelRun[], message: Message, userId: string): string | null {
  const own = runs.filter(run => run.context_through_sequence === message.sequence && run.requested_by === userId);
  return own.some(run => run.status !== "queued")
    ? "Its own reply has started. Moving now would pay for both; stop that reply first." : null;
}

export const modelName = (model: string) => model.split("/").at(-1) ?? model;

/** The receipt under an added message. */
export function additionReceipt(addition: ReplyAddition, model: string, active: boolean): string {
  const name = modelName(model);
  const lead = addition.moved ? `Moved into ${name}'s reply` : `Added to ${name}'s reply`;
  const cancelled = addition.moved && addition.cancelled_runs.length ? "; its own reply was cancelled before it started" : "";
  if (addition.read_at !== null) {
    return `${lead} · read ${addition.read_step ? `at step ${addition.read_step}` : "when the reply resumed"}${cancelled}`;
  }
  return active ? `${lead} · waiting for its next step${cancelled}` : `${lead} · it finished before reading this${cancelled}`;
}

/**
 * An added message shows in its reply's timeline while the reply runs, and folded under the answer that read it.
 * Anywhere else (a reply that finished before reading it, or failed without an answer) it stays in the transcript.
 */
export const shownInReply = (addition: ReplyAddition | undefined, active: boolean, folded: ReadonlySet<string>) =>
  Boolean(addition && (active || folded.has(addition.message_id)));

type Activity = NonNullable<ModelRun["activity"]>;
export type StepIcon = "think" | "search" | "read" | "write" | "compute" | "answer" | "summary" | "delegate";
export type TimelineEntry =
  | { kind: "step"; key: string; label: string; icon: StepIcon; current: boolean }
  | { kind: "addition"; key: string; addition: ReplyAddition; waiting: boolean; nextStep?: number };

const iconFor = (detail: string): StepIcon => detail.startsWith("searched") ? "search"
  : detail.startsWith("read") || detail.startsWith("fetched") ? "read"
    : detail.startsWith("saved") || detail.startsWith("updated") ? "write"
      : detail.startsWith("submitted") || detail.startsWith("checked") ? "compute" : "think";

/**
 * How many steps a reply took. Its recorded activity keeps only the latest events, so the count is the highest step
 * number rather than the number of steps kept.
 */
export const stepCount = (activity: Activity) =>
  activity.filter(event => event.phase === "round").reduce((most, event, index) => Math.max(most, event.round ?? index + 1), 0);

/**
 * A reply's steps in order, each described by what its tool calls did, and every addition where the reply read it:
 * an addition is read just before the provider call that starts a step. While the reply runs, unread additions follow
 * the last step, waiting for the next one. `unplaced` also lists read additions whose read event is not among these
 * steps (read by another run of the thread), for the folded record under an answer.
 */
export function replyTimeline(activity: Activity, additions: ReplyAddition[], active: boolean, unplaced = false): TimelineEntry[] {
  const entries: TimelineEntry[] = [], byId = new Map(additions.map(addition => [addition.message_id, addition]));
  const placed = new Set<string>();
  let step: { round: number; done: string[]; using?: string; writing: boolean } | null = null, rounds = 0;
  const close = () => {
    if (!step) return;
    const shown = [...new Set(step.done)], parts = [`Step ${step.round}`];
    if (shown.length) parts.push(shown.slice(0, 2).join("; ") + (shown.length > 2 ? ` +${shown.length - 2} more` : ""));
    if (step.using) parts.push(step.using);
    if (step.writing) parts.push("writing the answer");
    entries.push({ kind: "step", key: "step-" + step.round, current: false, label: parts.join(" · "),
      icon: step.writing && !step.using ? "answer" : iconFor(step.using ?? shown[0] ?? "") });
    step = null;
  };
  for (const event of activity) {
    if (event.phase === "round") { close(); rounds = event.round ?? rounds + 1; step = { round: rounds, done: [], writing: false }; }
    else if (event.phase === "tool_requested" && step) step.using = event.detail ?? (event.tool ?? "").replaceAll("_", " ");
    else if (event.phase === "tool_completed" && step) {
      step.done.push(event.detail ?? `used ${(event.tool ?? "").replaceAll("_", " ")}`); step.using = undefined;
    } else if (event.phase === "answer_ready" && step) step.writing = true;
    else if (event.phase === "additions_read") {
      close();
      for (const id of event.message_ids ?? []) {
        const addition = byId.get(id);
        if (addition) { entries.push({ kind: "addition", key: "addition-" + id, addition, waiting: false }); placed.add(id); }
      }
    } else if (["compacting", "compaction_failed", "compaction_skipped", "delegated"].includes(event.phase)) {
      close();
      entries.push({ kind: "step", key: "event-" + event.sequence, current: false, icon: event.phase === "delegated" ? "delegate" : "summary",
        label: event.summary ?? (event.phase === "delegated" ? "Delegated work" : "Summarizing earlier messages") });
    }
  }
  close();
  // The recorded activity keeps only the latest events; say so when the earliest steps fell outside it.
  const first = activity.find(event => event.phase === "round")?.round ?? 1;
  if (first > 1) entries.unshift({ kind: "step", key: "earlier", current: false, icon: "summary",
    label: first === 2 ? "Step 1 not shown" : `Steps 1–${first - 1} not shown` });
  const last = entries.map(entry => entry.kind).lastIndexOf("step");
  if (active && last >= 0) entries[last] = { ...entries[last], current: true } as TimelineEntry;
  if (unplaced) {
    for (const addition of additions.filter(item => item.read_at !== null && !placed.has(item.message_id))) {
      entries.push({ kind: "addition", key: "addition-" + addition.message_id, addition, waiting: false });
    }
  }
  if (active) {
    for (const addition of additions.filter(item => item.read_at === null)) {
      entries.push({ kind: "addition", key: "addition-" + addition.message_id, addition, waiting: true, nextStep: rounds + 1 });
    }
  }
  return entries;
}
