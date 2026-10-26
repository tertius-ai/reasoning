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

export type TimelineEntry =
  | { kind: "step"; key: string; label: string; current: boolean }
  | { kind: "addition"; key: string; addition: ReplyAddition; waiting: boolean };

const tool = (name: string) => name.replaceAll("_", " ");

/**
 * A reply's steps in order, each with the tools it used, and every addition where the reply read it: an addition is
 * read just before the provider call that starts a step. Unread additions follow the last step while the reply runs.
 */
export function replyTimeline(run: ModelRun, additions: ReplyAddition[], active: boolean): TimelineEntry[] {
  const entries: TimelineEntry[] = [], byId = new Map(additions.map(addition => [addition.message_id, addition]));
  let step: { round: number; tools: string[]; writing: boolean; using?: string } | null = null;
  const close = () => {
    if (!step) return;
    const tools = [...new Set(step.tools)].map(tool).join(", ");
    entries.push({ kind: "step", key: "step-" + step.round, current: false,
      label: `Step ${step.round}` + (tools ? ` · used ${tools}` : "") + (step.writing ? " · writing the answer" : "")
        + (step.using ? ` · using ${tool(step.using)}` : "") });
    step = null;
  };
  for (const event of run.activity ?? []) {
    if (event.phase === "round") { close(); step = { round: event.round ?? entries.length + 1, tools: [], writing: false }; }
    else if (event.phase === "tool_requested" && step && event.tool) step.using = event.tool;
    else if (event.phase === "tool_completed" && step && event.tool) { step.tools.push(event.tool); step.using = undefined; }
    else if (event.phase === "answer_ready" && step) step.writing = true;
    else if (event.phase === "additions_read") {
      close();
      for (const id of event.message_ids ?? []) {
        const addition = byId.get(id);
        if (addition) entries.push({ kind: "addition", key: "addition-" + id, addition, waiting: false });
      }
    } else if (["compacting", "compaction_failed", "compaction_skipped", "delegated"].includes(event.phase)) {
      close();
      entries.push({ kind: "step", key: "event-" + event.sequence, current: false,
        label: event.summary ?? (event.phase === "delegated" ? "Delegated work" : "Summarizing earlier messages") });
    }
  }
  close();
  const last = entries.map(entry => entry.kind).lastIndexOf("step");
  if (active && last >= 0) entries[last] = { ...entries[last], current: true } as TimelineEntry;
  if (active) {
    for (const addition of additions.filter(item => item.read_at === null)) {
      entries.push({ kind: "addition", key: "addition-" + addition.message_id, addition, waiting: true });
    }
  }
  return entries;
}

