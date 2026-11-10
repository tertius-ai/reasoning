// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
/** The row under a model answer, in a topic and in a side chat: its folded steps and its usage. */
import React, { useState } from "react";
import { MessageUsageDetails } from "./MessageUsageDetails";
import { stepCount } from "./ReplyAdditions";
import { ReplyTimeline } from "./ReplyTimeline";
import type { Message } from "./organization-api";

/** The folded line under a model answer: how many steps its reply took and how many later messages it read. */
export function replyRecordSummary(message: Pick<Message, "steps" | "additions">): string {
  const steps = stepCount(message.steps ?? []), added = message.additions?.length ?? 0;
  return [steps ? `${steps} ${steps === 1 ? "step" : "steps"}` : "",
    added ? `took ${added} later ${added === 1 ? "message" : "messages"} into account` : ""].filter(Boolean).join(" · ")
    .replace(/^./, letter => letter.toUpperCase());
}

/** One row under a model answer: its folded steps on the left and its usage at the right; the steps open below the row. */
export function ReplyFooter({ message, userId, names }: {
  message: Pick<Message, "steps" | "additions" | "author_kind" | "usage">; userId: string; names: Record<string, string>;
}) {
  const [open, setOpen] = useState(false);
  const record = !!message.steps?.some(step => step.phase === "round") || !!message.additions?.length;
  if (!record && message.author_kind !== "assistant") return null;
  return <>
    <div className="org-usage-row">
      {record && <button type="button" className="org-reply-record-toggle" aria-expanded={open} onClick={() => setOpen(!open)}>
        {replyRecordSummary(message)}</button>}
      {message.author_kind === "assistant" && <MessageUsageDetails usage={message.usage} />}
    </div>
    {record && open && <div className="org-reply-record">
      <ReplyTimeline activity={message.steps ?? []} additions={message.additions ?? []} active={false} userId={userId}
        names={names} unplaced label="Steps and later messages of this reply" /></div>}
  </>;
}
