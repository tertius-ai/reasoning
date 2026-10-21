// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import React, { useEffect, useRef, useState } from "react";
import { ApiError, Message, errorMessage, request, topicPath } from "./organization-api";

export type MessageThread = { root_id: string; message_id: string; messages: Message[]; truncated: boolean };

/** One line of a message's text for a reply pointer: mentions and references keep their visible names, markup is dropped. */
export function replyExcerpt(content: string, limit = 90): string {
  const text = content
    .replace(/@\[[a-f0-9]{32}(?::([^\]]+))?\]/g, (_, name?: string) => "@" + (name ?? "member"))
    .replace(/#\[(topic|project):[a-f0-9]{32}\]/g, (_, kind: string) => "#" + kind)
    .replace(/[*_`>#~[\]()$\\]/g, "").replace(/\s+/g, " ").trim();
  return text.length > limit ? text.slice(0, limit - 1).trimEnd() + "…" : text;
}

/** The first name of a person, or a bot's or model's whole label. */
export const replyName = (label: string, kind?: Message["author_kind"]) => kind && kind !== "human" ? label : label.split(/\s+/)[0];

/** The pointer above a reply; a curve in the gutter joins it to the speaker's dot. Opens the whole thread. */
export function ReplyPointer({ original, label, onOpen }: { original?: Message; label?: string; onOpen: () => void }) {
  return <button type="button" className="org-reply-pointer" onClick={onOpen}
    title="Show the whole thread" aria-label={original && label ? `Reply to ${label}: show the whole thread` : "Reply to an earlier message: show the whole thread"}>
    <span aria-hidden="true">↳</span>
    {original && label ? <><strong>{replyName(label, original.author_kind)}</strong><span>{replyExcerpt(original.content)}</span></>
      : <span>an earlier message</span>}
  </button>;
}

/** Shown in the composer while a reply is being written; Escape or × cancels it. */
export function ReplyBar({ original, label, onCancel }: { original?: Message; label?: string; onCancel: () => void }) {
  return <div className="org-reply-bar" role="status">
    <span aria-hidden="true">↳</span>
    <span className="org-reply-bar-text">Replying to <strong>{label ?? "an earlier message"}</strong>
      {original && <span> · {replyExcerpt(original.content, 70)}</span>}</span>
    <button type="button" aria-label="Cancel reply" title="Cancel reply · Esc" onClick={onCancel}>×</button>
  </div>;
}

/**
 * The whole thread over a dimmed conversation: the root, then every reply in order, with a box to reply in the thread.
 * A reply written here answers the thread's root. Escape or a click outside closes it.
 */
export function ThreadDialog({ organizationId, topicId, messageId, canWrite, renderMessage, onPosted, onClose, onAuthLost }: {
  organizationId: string; topicId: string; messageId: string; canWrite: boolean;
  renderMessage: (message: Message, first: boolean) => React.ReactNode;
  onPosted: (message: Message) => void; onClose: () => void; onAuthLost: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [thread, setThread] = useState<MessageThread | null>(null);
  const [error, setError] = useState("");
  const [text, setText] = useState(""), [clientId, setClientId] = useState(""), [sending, setSending] = useState(false);
  const path = topicPath(organizationId, topicId) + "/messages/";
  const fail = (cause: unknown) => { if (cause instanceof ApiError && cause.status === 401) onAuthLost(); else setError(errorMessage(cause)); };
  const load = (id: string) => request<MessageThread>(path + encodeURIComponent(id) + "/thread").then(setThread);
  useEffect(() => { dialog.current?.showModal(); }, []);
  useEffect(() => { setThread(null); setError(""); void load(messageId).catch(fail); }, [organizationId, topicId, messageId]);
  const send = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!thread || !text.trim() || sending) return;
    const attempt = clientId || crypto.randomUUID();
    setClientId(attempt); setSending(true); setError("");
    try {
      const message = await request<Message>(topicPath(organizationId, topicId) + "/messages",
        { method: "POST", body: { content: text, client_id: attempt, reply_to: thread.root_id } });
      onPosted(message); setText(""); setClientId("");
      await load(message.id);
    } catch (cause) { fail(cause); } finally { setSending(false); }
  };
  return <dialog ref={dialog} className="org-thread-dialog" aria-label="Thread" onCancel={onClose} onClose={onClose}
    onClick={event => { if (event.target === event.currentTarget) dialog.current?.close(); }}>
    <header><strong>Thread</strong>{thread && <small>{thread.messages.length} {thread.messages.length === 1 ? "message" : "messages"}</small>}
      <button type="button" aria-label="Close thread" onClick={() => dialog.current?.close()}>×</button></header>
    <div className="org-thread-messages">
      {!thread && !error && <p role="status">Loading the thread…</p>}
      {thread?.messages.map((message, index) => <React.Fragment key={message.id}>{renderMessage(message, index === 0)}</React.Fragment>)}
      {thread?.truncated && <p className="org-help">Only the first 200 messages of this thread are shown.</p>}
    </div>
    {error && <p className="org-error" role="alert">{error}</p>}
    {canWrite && thread && <form className="org-thread-composer" onSubmit={event => void send(event)}>
      <label className="org-visually-hidden" htmlFor="org-thread-reply">Reply in thread</label>
      <textarea id="org-thread-reply" rows={1} maxLength={16000} placeholder="Reply in thread…" value={text} disabled={sending}
        onChange={event => { setText(event.target.value); setClientId(""); }}
        onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void send(event); } }} />
      <button className="org-primary" disabled={sending || !text.trim()} aria-label={sending ? "Sending…" : "Send reply"}>{sending ? "…" : "▶"}</button>
    </form>}
  </dialog>;
}
