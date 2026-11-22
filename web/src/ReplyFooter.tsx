// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
/** The row under a model answer, in a topic and in a side chat: "Show full answer" when it is folded, its steps and its usage. */
import React, { useState } from "react";
import { FoldToggle, type Fold } from "./FoldedReply";
import { MessageUsageDetails } from "./MessageUsageDetails";
import { stepCount } from "./ReplyAdditions";
import { ReplyTimeline } from "./ReplyTimeline";
import type { Message, ModelRun } from "./organization-api";

/** The folded line under a model answer: how many steps its reply took and how many later messages it read. */
export function replyRecordSummary(message: Pick<Message, "steps" | "additions">): string {
  const steps = stepCount(message.steps ?? []), added = message.additions?.length ?? 0;
  return [steps ? `${steps} ${steps === 1 ? "step" : "steps"}` : "",
    added ? `took ${added} later ${added === 1 ? "message" : "messages"} into account` : ""].filter(Boolean).join(" · ")
    .replace(/^./, letter => letter.toUpperCase());
}

/**
 * The later reply that continued a model answer's work, if one has: as the message was loaded, or from the reply's run once
 * the server handed its work on since, so a reply that waited in this page says so without a reload.
 */
export function continuation(message: Pick<Message, "continued" | "inference_run_id">,
  runs: Pick<ModelRun, "id" | "message_id" | "answer_state">[]): Message["continued"] {
  const run = message.inference_run_id ? runs.find(item => item.id === message.inference_run_id) : undefined;
  const next = run?.answer_state?.next_run;
  if (!message.continued && !next) return undefined;
  return { message_id: message.continued?.message_id ?? runs.find(item => item.id === next)?.message_id ?? null };
}

/**
 * One row under a model answer: "Show full answer" when it is folded, its folded steps, and its usage at the right; the
 * steps open below the row. An answer whose work a later reply continued says so above the row, with a link to that reply.
 */
export function ReplyFooter({ message, userId, names, fold, onShowMessage }: {
  message: Pick<Message, "steps" | "additions" | "author_kind" | "usage" | "continued">; userId: string; names: Record<string, string>;
  fold?: Fold;
  /** Shows a message of the topic, for the link to the reply that continued this one. */
  onShowMessage?: (id: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const record = !!message.steps?.some(step => step.phase === "round") || !!message.additions?.length;
  const later = message.continued?.message_id;
  if (!record && !fold && message.author_kind !== "assistant") return null;
  return <>
    {message.continued && <p className="org-reply-continued" role="note"><span aria-hidden="true">↪</span>
      <span>A later reply continued this work and carries the answer.</span>
      {later && onShowMessage && <button type="button" onClick={() => onShowMessage(later)}>Show that reply</button>}</p>}
    <div className="org-usage-row">
      {fold && <FoldToggle fold={fold} />}
      {record && <button type="button" className="org-reply-record-toggle" aria-expanded={open} onClick={() => setOpen(!open)}>
        {replyRecordSummary(message)}</button>}
      {message.author_kind === "assistant" && <MessageUsageDetails usage={message.usage} />}
    </div>
    {record && open && <div className="org-reply-record">
      <ReplyTimeline activity={message.steps ?? []} additions={message.additions ?? []} active={false} userId={userId}
        names={names} unplaced label="Steps and later messages of this reply" /></div>}
  </>;
}
