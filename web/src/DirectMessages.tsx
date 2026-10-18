// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import { DirectConversation, Member, organizationPath } from "./organization-api";
import "./direct-messages.css";

export type { DirectConversation };

export const directPath = (organizationId: string, conversationId?: string) =>
  organizationPath(organizationId) + "/direct-conversations" + (conversationId ? "/" + encodeURIComponent(conversationId) : "");

/** Unread direct messages that should be announced: muted conversations keep their count but show no badge. */
export const directUnread = (conversation: DirectConversation) => conversation.muted ? 0 : conversation.unread_count;

/** Announced unread direct messages across all conversations, for the People toggle. */
export const directUnreadTotal = (conversations: DirectConversation[]) =>
  conversations.reduce((sum, conversation) => sum + directUnread(conversation), 0);

const recency = (conversation: DirectConversation) => conversation.last_message_at ?? conversation.created_at;

/**
 * The People list as the direct-message inbox. People with a direct conversation come first, most recent message first,
 * each carrying its conversation; everyone else keeps the given order. A conversation partner missing from `people`
 * (offline without bots, or no longer a member) is added through `placeholder`.
 */
export function peopleInbox<P extends { id: string }>(people: P[], conversations: DirectConversation[],
  placeholder: (conversation: DirectConversation) => P): (P & { conversation?: DirectConversation })[] {
  const byUser = new Map(conversations.map(conversation => [conversation.other_user.id, conversation]));
  const listed = new Set(people.map(person => person.id));
  const talking = [...people.filter(person => byUser.has(person.id)),
    ...conversations.filter(conversation => !listed.has(conversation.other_user.id)).map(placeholder)]
    .map(person => ({ ...person, conversation: byUser.get(person.id) }))
    .sort((left, right) => recency(right.conversation!) - recency(left.conversation!));
  return [...talking, ...people.filter(person => !byUser.has(person.id))];
}

/** Marks beside a person in People for their direct conversation: state and the unread count. */
export function DirectMessageMarks({ conversation }: { conversation: DirectConversation }) {
  const unread = directUnread(conversation);
  return <span className="org-direct-marks">
    <svg className="org-direct-mark" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"
      aria-hidden="true"><path d="M4 5h16v11H9l-5 4z" /></svg>
    {!conversation.other_member && <small className="org-private-mark">left</small>}
    {conversation.blocked && <small className="org-private-mark">blocked</small>}
    {conversation.muted && <small className="org-private-mark" aria-label="Muted">muted</small>}
    {unread > 0 && <span className="org-unread" aria-label={`${unread} unread direct messages`}>{unread}</span>}
  </span>;
}

/** The People header control that opens the member picker for messaging anyone, including people not shown. */
export function NewDirectMessageButton({ open, onToggle }: { open: boolean; onToggle: () => void }) {
  return <button type="button" className="org-direct-new" aria-label="New direct message" title="New direct message"
    aria-expanded={open} onClick={onToggle}>+</button>;
}

/** Chooses any other member to start or open a direct conversation with. */
export function DirectMessagePicker({ members, userId, onStart, onClose }: {
  members: Pick<Member, "id" | "name">[]; userId: string; onStart: (userId: string) => void; onClose: () => void;
}) {
  const others = members.filter(member => member.id !== userId);
  return <label className="org-direct-picker"><span className="org-visually-hidden">Message a member</span>
    <select autoFocus value="" aria-label="Message a member" onChange={event => {
      if (!event.target.value) return;
      onClose(); onStart(event.target.value);
    }} onKeyDown={event => { if (event.key === "Escape") onClose(); }}>
      <option value="">Choose a person</option>
      {others.map(member => <option key={member.id} value={member.id}>{member.name}</option>)}
    </select></label>;
}
