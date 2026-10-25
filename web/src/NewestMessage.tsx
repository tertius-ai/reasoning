// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import { useEffect, useState } from "react";
import type { Message } from "./organization-api";

/** Messages from others that arrived while the reader was scrolled up; the newest-message button counts them. */
export function useUnseenMessages(messages: Message[], atBottom: boolean, userId: string) {
  const [seenSequence, setSeenSequence] = useState(0);
  const latestSequence = messages.at(-1)?.sequence ?? 0;
  useEffect(() => { if (atBottom) setSeenSequence(latestSequence); }, [atBottom, latestSequence]);
  return atBottom ? 0 : messages.filter(message => message.sequence > seenSequence && message.author_id !== userId).length;
}

/** Newest message, as in Signal: a raised round button with a down chevron and the count of new messages. */
export function NewestMessageButton({ unseen, onJump }: { unseen: number; onJump: () => void }) {
  return <button className="org-jump" aria-label={unseen ? `Jump to newest message, ${unseen} new` : "Jump to newest message"} title="Newest message" onClick={onJump}>
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6" /></svg>
    {unseen > 0 && <span className="org-jump-count" aria-hidden="true">{unseen > 99 ? "99+" : unseen}</span>}</button>;
}
