// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import React, { useEffect, useMemo, useRef, useState } from "react";
import { ApiError, Message, errorMessage, request } from "./organization-api";
import { ReturnIcon } from "./ComposerIcons";
import { AttachFilesButton, MAX_DIRECT_ATTACHMENTS, PendingFile, PendingFiles, joinAttachments, pendingFile } from "./DirectMedia";
import "./direct-messages.css";

export type MessageThread = { root_id: string; message_id: string; messages: Message[]; truncated: boolean };

/** One line of a message's text for a reply pointer: mentions and references keep their visible names, markup is dropped. */
export function replyExcerpt(content: string, limit = 90): string {
  const text = content
    .replace(/!\[([^\]\n]*)\]\([^)\s]*\)/g, (_, name: string) => "📎 " + (name || "attachment"))
    .replace(/@\[[a-f0-9]{32}(?::([^\]]+))?\]/g, (_, name?: string) => "@" + (name ?? "member"))
    .replace(/#\[(topic|project):[a-f0-9]{32}\]/g, (_, kind: string) => "#" + kind)
    .replace(/[*_`>#~[\]()$\\]/g, "").replace(/\s+/g, " ").trim();
  return text.length > limit ? text.slice(0, limit - 1).trimEnd() + "…" : text;
}

/** The first name of a person, or a bot's or model's whole label. */
export const replyName = (label: string, kind?: Message["author_kind"]) => kind && kind !== "human" ? label : label.split(/\s+/)[0];

/**
 * How many loaded messages reply, directly or further down, to each thread root. Replies come after their root, so
 * with the root loaded every reply to it is loaded too.
 */
export function threadReplyCounts(messages: Pick<Message, "id" | "reply_to">[]): Map<string, number> {
  const parent = new Map(messages.map(message => [message.id, message.reply_to ?? null]));
  const counts = new Map<string, number>();
  for (const message of messages) {
    if (!message.reply_to) continue;
    let root = message.reply_to;
    const seen = new Set([message.id]);
    while (parent.get(root) && !seen.has(root)) { seen.add(root); root = parent.get(root)!; }
    counts.set(root, (counts.get(root) ?? 0) + 1);
  }
  return counts;
}

/** Under a thread's first message: how many replies it has. Opens the whole thread. */
export function ThreadCount({ count, onOpen }: { count: number; onOpen: () => void }) {
  const label = `${count} ${count === 1 ? "reply" : "replies"}`;
  return <button type="button" className="org-thread-count" onClick={onOpen} title="Show the whole thread"
    aria-label={`${label}: show the whole thread`}><span aria-hidden="true">↩</span>{label}</button>;
}

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
 * A loaded thread brought up to date from the conversation's live messages: newer versions of its messages, and every
 * live message that replies into it, oldest first.
 */
export function joinThread(messages: Message[], live: Message[]): Message[] {
  const byId = new Map(messages.map(message => [message.id, message]));
  for (const message of live) if (byId.has(message.id)) byId.set(message.id, message);
  for (let grew = true; grew;) {
    grew = false;
    for (const message of live) {
      if (!byId.has(message.id) && message.reply_to && byId.has(message.reply_to)) { byId.set(message.id, message); grew = true; }
    }
  }
  return [...byId.values()].sort((a, b) => a.sequence - b.sequence);
}

/**
 * The whole thread over a dimmed conversation: the root, then every reply in order, with a box to reply in the thread.
 * A reply written here answers the thread's root. Escape or a click outside closes it.
 */
export function ThreadDialog({ messagesPath, messageId, canWrite, renderMessage, onPosted, onClose, onAuthLost, live, working, attach, addTo, onStopAdding }: {
  /** The conversation's messages endpoint: a topic's or a direct conversation's. */
  messagesPath: string; messageId: string; canWrite: boolean;
  renderMessage: (message: Message, first: boolean) => React.ReactNode;
  onPosted: (message: Message) => void; onClose: () => void; onAuthLost: () => void;
  /** The conversation's current messages: replies that arrive while the thread is open join it. */
  live?: Message[];
  /** Work in progress on the thread's messages, such as a model reply, shown after them. */
  working?: (messages: Message[]) => React.ReactNode;
  /** Uploads a chosen file; its Markdown reference is sent with the reply. */
  attach?: (file: File) => Promise<{ markdown: string }>;
  /** A running reply the next message joins instead of starting its own, named for the box. */
  addTo?: { id: string; label: string };
  onStopAdding?: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [thread, setThread] = useState<MessageThread | null>(null);
  const [error, setError] = useState("");
  const [text, setText] = useState(""), [clientId, setClientId] = useState(""), [sending, setSending] = useState(false);
  const [files, setFiles] = useState<PendingFile[]>([]), [notice, setNotice] = useState("");
  const list = useRef<HTMLDivElement>(null), box = useRef<HTMLTextAreaElement>(null);
  // Choosing to add to a reply moves you to the box, where the next message is written.
  useEffect(() => { if (addTo) box.current?.focus(); }, [addTo?.id]);
  const shown = useMemo(() => thread ? joinThread(thread.messages, live ?? []) : [], [thread, live]);
  const uploading = files.some(file => !file.markdown);
  const path = messagesPath + "/";
  const fail = (cause: unknown) => { if (cause instanceof ApiError && cause.status === 401) onAuthLost(); else setError(errorMessage(cause)); };
  const load = (id: string) => request<MessageThread>(path + encodeURIComponent(id) + "/thread").then(setThread);
  useEffect(() => { dialog.current?.showModal(); }, []);
  useEffect(() => { setThread(null); setError(""); void load(messageId).catch(fail); }, [messagesPath, messageId]);
  // New replies and work in progress appear at the bottom: when the list grows while you were at its end, it follows.
  const height = useRef(0);
  useEffect(() => {
    const view = list.current;
    if (!view) return;
    const before = height.current;
    height.current = view.scrollHeight;
    if (before && view.scrollHeight > before && before - view.scrollTop - view.clientHeight < 48) view.scrollTo({ top: view.scrollHeight });
  });
  const addFiles = (chosen: File[]) => {
    if (!attach) return;
    const room = MAX_DIRECT_ATTACHMENTS - files.length;
    setError(chosen.length > room ? `A message can carry at most ${MAX_DIRECT_ATTACHMENTS} files.` : "");
    const pending = chosen.slice(0, Math.max(0, room)).map(file => ({ file, item: pendingFile(file) }));
    setFiles(current => [...current, ...pending.map(({ item }) => item)]);
    const settle = (key: string, value: Partial<PendingFile>) => setFiles(current => current.map(file => file.key === key ? { ...file, ...value } : file));
    for (const { file, item } of pending) {
      void attach(file).then(value => settle(item.key, { markdown: value.markdown }), cause => {
        if (cause instanceof ApiError && cause.status === 401) onAuthLost();
        settle(item.key, { error: errorMessage(cause) });
      });
    }
    setClientId("");
  };
  const removeFile = (key: string) => setFiles(current => current.filter(file => {
    if (file.key === key && file.preview) URL.revokeObjectURL(file.preview);
    return file.key !== key;
  }));
  const content = files.length ? joinAttachments(files.flatMap(file => file.markdown ? [file.markdown] : []), text) : text;
  const send = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!thread || !content.trim() || uploading || sending) return;
    const attempt = clientId || crypto.randomUUID();
    setClientId(attempt); setSending(true); setError(""); setNotice("");
    try {
      const reply = { content, client_id: attempt, reply_to: thread.root_id };
      let message: Message;
      if (addTo) {
        // An addition joins the running reply and shows in its card rather than as a reply of its own.
        try {
          message = await request<Message>(messagesPath, { method: "POST", body: { content, client_id: attempt, adds_to: addTo.id } });
        } catch (cause) {
          // The reply finished first: the server posted nothing, so the same message becomes a new reply in the thread.
          if (!(cause instanceof ApiError) || cause.status !== 409) throw cause;
          setNotice(`${addTo.label}'s reply finished first, so this was sent as a new question.`);
          message = await request<Message>(messagesPath, { method: "POST", body: reply });
        }
        onStopAdding?.();
      } else message = await request<Message>(messagesPath, { method: "POST", body: reply });
      onPosted(message); setText(""); setClientId("");
      for (const file of files) if (file.preview) URL.revokeObjectURL(file.preview);
      setFiles([]);
      // An addition replies to nothing, so the thread reloads from its root rather than from the posted message.
      await load(thread.root_id);
    } catch (cause) { fail(cause); } finally { setSending(false); }
  };
  // Escape leaves adding mode first, wherever focus is in the thread; a second Escape closes it.
  return <dialog ref={dialog} className="org-thread-dialog" aria-label="Thread" onClose={onClose}
    onCancel={event => { if (addTo) { event.preventDefault(); onStopAdding?.(); } else onClose(); }}
    onClick={event => { if (event.target === event.currentTarget) dialog.current?.close(); }}>
    <header><strong>Thread</strong>{thread && <small>{shown.length} {shown.length === 1 ? "message" : "messages"}</small>}
      <button type="button" aria-label="Close thread" onClick={() => dialog.current?.close()}>×</button></header>
    <div className="org-thread-messages" ref={list}>
      {!thread && !error && <p role="status">Loading the thread…</p>}
      {shown.map((message, index) => <React.Fragment key={message.id}>{renderMessage(message, index === 0)}</React.Fragment>)}
      {thread && working?.(shown)}
      {thread?.truncated && <p className="org-help">Only the first 200 messages of this thread are shown.</p>}
    </div>
    {error && <p className="org-error" role="alert">{error}</p>}
    {notice && <p className="org-help" role="status">{notice}</p>}
    {canWrite && thread && <form className={"org-thread-composer" + (addTo ? " org-thread-composer-adding" : "")} onSubmit={event => void send(event)}>
      {addTo && <span className="org-visually-hidden" role="status">Adding to {addTo.label}'s reply. Escape sends a new question instead.</span>}
      {attach && <PendingFiles files={files} onRemove={key => { removeFile(key); setClientId(""); }} />}
      <div className="org-thread-composer-row">
        {attach && <AttachFilesButton disabled={sending} onFiles={addFiles} />}
        <label className="org-visually-hidden" htmlFor="org-thread-reply">Reply in thread</label>
        <textarea id="org-thread-reply" ref={box} rows={1} maxLength={16000} placeholder={addTo ? `Add to ${addTo.label}'s reply…` : "Reply in thread…"}
          value={text} disabled={sending} onChange={event => { setText(event.target.value); setClientId(""); }}
          onKeyDown={event => {
            if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void send(event); }
            else if (event.key === "Escape" && addTo) { event.preventDefault(); event.stopPropagation(); onStopAdding?.(); }
          }} />
        <button className="org-composer-button" disabled={sending || uploading || !content.trim()}
          aria-label={sending ? "Sending…" : uploading ? "Waiting for uploads" : "Send reply"}
          title={uploading ? "Wait for the uploads to finish, or remove the failed ones" : "Send reply"}><ReturnIcon /></button>
      </div>
    </form>}
  </dialog>;
}
