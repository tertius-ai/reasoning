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

/** The thread line under a running reply: what was added and what it has read. */
export function additionsSummary(additions: ReplyAddition[]): string {
  if (!additions.length) return "Add to this reply";
  const read = additions.filter(addition => addition.read_at !== null).length;
  return `${additions.length} addition${additions.length === 1 ? "" : "s"} · ${read} read · ${additions.length - read} waiting`;
}

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
