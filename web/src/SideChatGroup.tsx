// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
/**
 * Side-chat messages someone brought into a topic, folded into one group: a header naming whose side chat they came
 * from, a one-line preview of each, and the full messages when opened. A context note says how many earlier side
 * messages stayed private and which ledger statements those recorded.
 */
import React, { useState } from "react";
import { replyExcerpt } from "./MessageReplies";
import { modelName } from "./ReplyAdditions";
import type { SideChatGroup as Group, SideStatement } from "./organization-api";
import "./side-chat.css";

/** A ledger statement as a chip: a short ID, with the statement's text on hover. */
export function StatementChips({ statements }: { statements: SideStatement[] }) {
  if (!statements.length) return null;
  return <ul className="org-side-statements" aria-label="Ledger statements">{statements.map(statement =>
    <li key={statement.claim_id} title={statement.statement_text ?? statement.claim_id}>
      <span className="org-side-statement-id">{statement.claim_id.slice(0, 12)}</span>
      {statement.statement_text && <span className="org-side-statement-text">{replyExcerpt(statement.statement_text, 60)}</span>}
    </li>)}</ul>;
}

export const sideSpeaker = (message: { author_kind: "human" | "assistant"; assistant_model: string | null }, owner: string) =>
  message.author_kind === "assistant" ? modelName(message.assistant_model ?? "model") : owner;

export function SideChatGroup({ group, owner, own, renderMarkdown }: {
  group: Group; owner: string; own: boolean; renderMarkdown: (markdown: string) => React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const count = group.messages.length, statements = group.omitted_statements ?? [];
  const speaker = own ? "You" : owner;
  return <div className="org-side-group">
    <button type="button" className="org-side-group-toggle" aria-expanded={open} onClick={() => setOpen(value => !value)}>
      <span className="org-side-chip">{own ? "From your side chat" : `From ${owner}'s side chat`} · {count} message{count === 1 ? "" : "s"}</span>
      <span className="org-side-group-action">{open ? "Fold" : "Show all"}</span>
    </button>
    {group.omitted > 0 && <p className="org-side-group-note">Builds on {group.omitted} earlier side message{group.omitted === 1 ? "" : "s"} that
      stayed private{statements.length ? ", which recorded:" : "."}</p>}
    {group.omitted > 0 && <StatementChips statements={statements} />}
    {open ? <ol className="org-side-group-messages">{group.messages.map(message => <li key={message.id}>
      <strong className={"org-side-speaker org-side-speaker-" + message.author_kind}>{sideSpeaker(message, speaker)}</strong>
      {renderMarkdown(message.content)}
      <StatementChips statements={message.statements} />
    </li>)}</ol>
      : <ul className="org-side-group-preview">{group.messages.map(message => <li key={message.id}>
        <strong className={"org-side-speaker org-side-speaker-" + message.author_kind}>{sideSpeaker(message, speaker)}</strong> {replyExcerpt(message.content, 110)}
      </li>)}</ul>}
  </div>;
}
