// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import { useState } from "react";
import { DirectConversation, Member, organizationPath } from "./organization-api";
import "./direct-messages.css";

export type { DirectConversation };

export const directPath = (organizationId: string, conversationId?: string) =>
  organizationPath(organizationId) + "/direct-conversations" + (conversationId ? "/" + encodeURIComponent(conversationId) : "");

/** Unread direct messages that should be announced: muted conversations keep their count but show no badge. */
export const directUnread = (conversation: DirectConversation) => conversation.muted ? 0 : conversation.unread_count;

/** The navigation section listing the signed-in member's direct conversations, with unread counts. */
export function DirectMessageNavigation({ conversations, members, userId, selected, canStart, onSelect, onStart }: {
  conversations: DirectConversation[]; members: Pick<Member, "id" | "name">[]; userId: string; selected: string | null;
  canStart: boolean; onSelect: (conversationId: string) => void; onStart: (userId: string) => void;
}) {
  const [picking, setPicking] = useState(false);
  const total = conversations.reduce((sum, conversation) => sum + directUnread(conversation), 0);
  const others = members.filter(member => member.id !== userId);
  return <nav className="org-direct-nav" aria-label="Direct messages">
    <div className="org-direct-heading"><strong>Direct messages</strong>
      {total > 0 && <span className="org-unread" aria-label={`${total} unread direct messages in total`}>{total}</span>}
      {canStart && <button type="button" className="org-tree-add" aria-label="New direct message" title="New direct message"
        aria-expanded={picking} onClick={() => setPicking(value => !value)}>+</button>}</div>
    {picking && <label className="org-direct-picker"><span className="org-visually-hidden">Message a member</span>
      <select autoFocus value="" aria-label="Message a member" onChange={event => {
        if (!event.target.value) return;
        setPicking(false); onStart(event.target.value);
      }} onKeyDown={event => { if (event.key === "Escape") setPicking(false); }}>
        <option value="">Choose a person</option>
        {others.map(member => <option key={member.id} value={member.id}>{member.name}</option>)}
      </select></label>}
    {conversations.map(conversation => {
      const unread = directUnread(conversation);
      return <button type="button" key={conversation.id} className={"org-direct-row" + (conversation.muted ? " org-unfollowed" : "")}
        aria-current={selected === conversation.id ? "page" : undefined} title={`Direct messages with ${conversation.other_user.name}`}
        onClick={() => onSelect(conversation.id)}>
        <span className="org-presence-avatar" aria-hidden="true">{conversation.other_user.name.slice(0, 1).toUpperCase()}</span>
        <span className="org-topic-name">{conversation.other_user.name}</span>
        {!conversation.other_member && <small className="org-private-mark">left</small>}
        {conversation.blocked && <small className="org-private-mark">blocked</small>}
        {conversation.muted && <small className="org-private-mark" aria-label="Muted">muted</small>}
        {unread > 0 && <span className="org-unread" aria-label={`${unread} unread direct messages`}>{unread}</span>}
      </button>;
    })}
    {!conversations.length && <p className="org-tree-empty">Click a name in People to send a message.</p>}
  </nav>;
}
