// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import { Message, isOwnMessage } from "./organization-api";

/** What a message is for its reader: a mention of them, or a reply to them. */
export type ForYouKind = "mention" | "reply";
/** A message for the reader, with the first message of its thread. */
export type ForYouItem = { id: string; sequence: number; kind: ForYouKind; root: string };

/** Whether a message's stored text mentions this member, as `@[id]`. */
export function mentionsMember(content: string, userId: string): boolean {
  const id = userId.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`@\\[${id}(?::[^\\]]*)?\\]`).test(content);
}

/**
 * The same rule as the topic list's counts: a person's or bot's message that mentions the reader, or that replies to the
 * reader's own message or to a model answer the reader asked for. The reader's own messages and model answers are never
 * for them, and a reply that also mentions them is a mention.
 */
export function forYouKind(message: Message, answered: Message | undefined, userId: string): ForYouKind | null {
  if (message.author_kind === "assistant" || isOwnMessage(message, userId)) return null;
  if (mentionsMember(message.content, userId)) return "mention";
  return answered && answered.author_id === userId && answered.author_kind !== "bot" ? "reply" : null;
}

/**
 * Messages for the reader after `after` and through `through`, oldest first. A reply whose target is not loaded is left
 * out, so the topic list (which counts on the server) can count one that this list does not show.
 */
export function forYouItems(messages: Message[], userId: string, after: number, through: number): ForYouItem[] {
  const byId = new Map(messages.map(message => [message.id, message]));
  const rootOf = (message: Message) => {
    let root = message;
    const seen = new Set([message.id]);
    while (root.reply_to && byId.has(root.reply_to) && !seen.has(root.reply_to)) { root = byId.get(root.reply_to)!; seen.add(root.id); }
    return root.id;
  };
  return messages.filter(message => message.sequence > after && message.sequence <= through).flatMap(message => {
    const kind = forYouKind(message, message.reply_to ? byId.get(message.reply_to) : undefined, userId);
    return kind ? [{ id: message.id, sequence: message.sequence, kind, root: rootOf(message) }] : [];
  });
}

/** “2 mentions and 1 reply”: counts of each kind, for labels and the sidebar header. */
export function forYouTally(mentions: number, replies: number): string {
  const part = (count: number, one: string, many: string) => `${count} ${count === 1 ? one : many}`;
  return [mentions ? part(mentions, "mention", "mentions") : "", replies ? part(replies, "reply", "replies") : ""].filter(Boolean).join(" and ");
}
