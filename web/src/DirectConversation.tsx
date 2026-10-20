// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import React, { useEffect, useMemo, useRef, useState } from "react";
import { DirectConversation, directPath } from "./DirectMessages";
import { normalizeMathDelimiters } from "./markdownMath";
import { MessageMarkdown, alphaMessageGroups, resizeComposer } from "./OrganizationApp";
import { ApiError, Account, Member, Message, aborted, errorMessage, mentionMarkdown, mergeMessages, organizationPath, request } from "./organization-api";
import { startPolling } from "./polling";
import { LIVE_SAFETY_POLL_MS, subscribeEvents } from "./topicEvents";

type Draft = { text: string; clientId: string; attempted: boolean };
const newDraft = (): Draft => ({ text: "", clientId: crypto.randomUUID(), attempted: false });
// Unsent text survives switching between conversations for the life of the page.
const drafts = new Map<string, Draft>();

function DirectMessageEdit({ path, message, onSaved, onCancel, onAuthLost }: {
  path: string; message: Message; onSaved: (message: Message) => void; onCancel: () => void; onAuthLost: () => void;
}) {
  const [text, setText] = useState(message.content), [busy, setBusy] = useState(false), [error, setError] = useState("");
  return <form className="chat-edit" onSubmit={event => {
    event.preventDefault(); setBusy(true); setError("");
    void request<Message>(path + "/messages/" + encodeURIComponent(message.id), { method: "PATCH", body: { content: text, revision: message.revision ?? 0 } })
      .then(onSaved).catch(cause => { if (cause instanceof ApiError && cause.status === 401) onAuthLost(); else setError(errorMessage(cause)); })
      .finally(() => setBusy(false));
  }}>
    <label>Edit message<textarea aria-label="Edit message text" autoFocus rows={3} maxLength={16000} disabled={busy}
      value={text} onChange={event => setText(event.target.value)} /></label>
    <button disabled={busy || !text.trim()}>Save edit</button>
    <button type="button" disabled={busy} onClick={onCancel}>Cancel</button>
    {error && <small role="alert" className="org-error">{error}</small>}
  </form>;
}

/** A one-to-one conversation: only its two participants can read it, and no bot or model takes part. */
export function DirectConversationView({ organizationId, conversation, user, members, canWrite, onChanged, onAuthLost }: {
  organizationId: string; conversation: DirectConversation; user: Account; members: Member[]; canWrite: boolean;
  onChanged: () => void; onAuthLost: () => void;
}) {
  const root = directPath(organizationId, conversation.id), name = conversation.other_user.name;
  const [messages, setMessages] = useState<Message[]>([]);
  const [loading, setLoading] = useState(true), [older, setOlder] = useState(false), [loadingOlder, setLoadingOlder] = useState(false);
  const [online, setOnline] = useState(true), [error, setError] = useState(""), [sending, setSending] = useState(false);
  const [draft, setDraftState] = useState<Draft>(() => drafts.get(root) ?? newDraft());
  const [editing, setEditing] = useState<string | null>(null), [history, setHistory] = useState<{ id: string; versions: { revision: number; content: string; created_at: number }[] } | null>(null);
  const [confirmBlock, setConfirmBlock] = useState(false), [busy, setBusy] = useState(false);
  const [atBottom, setAtBottom] = useState(true);
  const viewport = useRef<HTMLDivElement>(null), textarea = useRef<HTMLTextAreaElement>(null);
  const cursor = useRef(0), editCursor = useRef(0), acknowledged = useRef(0), bottom = useRef(true), alive = useRef(true);
  const currentDraft = useRef(draft);
  currentDraft.current = draft;
  const setDraft = (value: Draft) => { drafts.set(root, value); setDraftState(value); };
  const fail = (cause: unknown) => { if (cause instanceof ApiError && cause.status === 401) onAuthLost(); else setError(errorMessage(cause)); };
  const writable = canWrite && conversation.other_member && !conversation.blocked;
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => { if (textarea.current) resizeComposer(textarea.current); }, [draft.text]);

  useEffect(() => {
    const controller = new AbortController();
    // The conversation's event stream announces new messages and edits; polling then fetches exactly what is missing.
    let live = false, loaded = false, watermark = { sequence: 0, updates: 0 };
    const behind = () => watermark.sequence > cursor.current || watermark.updates > editCursor.current;
    const poll = async (): Promise<number | null> => {
      const before = [cursor.current, editCursor.current];
      try {
        const query = cursor.current ? `after=${cursor.current}&limit=200` : `before=${Number.MAX_SAFE_INTEGER}&limit=100`;
        const [incoming, updates] = await Promise.all([
          request<Message[]>(root + "/messages?" + query, { signal: controller.signal }),
          request<{ after: number; messages: Message[] }>(root + "/message-updates?after=" + editCursor.current, { signal: controller.signal }),
        ]);
        if (controller.signal.aborted) return null;
        if (!cursor.current) setOlder(incoming.length === 100);
        cursor.current = incoming.at(-1)?.sequence ?? cursor.current;
        editCursor.current = updates.after;
        setMessages(existing => mergeMessages(mergeMessages(existing, incoming), updates.messages.filter(message =>
          existing.some(prior => prior.id === message.id) || incoming.some(prior => prior.id === message.id))));
        setOnline(true); setLoading(false); loaded = true;
        if (incoming.some(message => message.client_id === currentDraft.current.clientId && message.author_id === user.id)) setDraft(newDraft());
        if (behind() && (cursor.current !== before[0] || editCursor.current !== before[1])) return 0;
      } catch (cause) {
        if (aborted(cause)) return null;
        if (cause instanceof ApiError && cause.status === 401) { onAuthLost(); return null; }
        if (cause instanceof ApiError && cause.status === 404) setMessages([]);
        setOnline(false); setError(errorMessage(cause)); setLoading(false);
        return 2000;
      }
      return live ? LIVE_SAFETY_POLL_MS : 2000;
    };
    const poller = startPolling(poll, controller.signal);
    const unsubscribe = subscribeEvents(root + "/events", {
      status: status => { const next = status === "live"; if (live && !next && loaded) poller.wake(); live = next; },
      messages: value => { watermark = value; if (loaded && behind()) poller.wake(); },
    });
    return () => { unsubscribe(); controller.abort(); };
  }, [root, user.id, onAuthLost]);

  useEffect(() => {
    if (bottom.current && viewport.current) viewport.current.scrollTop = viewport.current.scrollHeight;
  }, [messages]);

  useEffect(() => {
    // Reading the newest message clears its unread count in navigation.
    const mark = () => {
      const sequence = messages.at(-1)?.sequence ?? 0;
      if (!bottom.current || document.visibilityState !== "visible" || !document.hasFocus() || sequence <= acknowledged.current
        || sequence <= conversation.last_read_sequence) return;
      void request(root + "/read", { method: "PUT", body: { sequence } }).then(() => {
        acknowledged.current = Math.max(acknowledged.current, sequence); onChanged();
      }).catch(cause => { if (cause instanceof ApiError && cause.status === 401) onAuthLost(); });
    };
    mark(); window.addEventListener("focus", mark); document.addEventListener("visibilitychange", mark);
    return () => { window.removeEventListener("focus", mark); document.removeEventListener("visibilitychange", mark); };
  }, [messages, atBottom, root, conversation.last_read_sequence, onAuthLost, onChanged]);

  const send = async (event?: React.FormEvent) => {
    event?.preventDefault();
    if (!writable || sending || !online || !draft.text.trim()) return;
    const attempt = { ...draft, clientId: draft.clientId || crypto.randomUUID(), attempted: true };
    setDraft(attempt); setSending(true); setError("");
    try {
      const message = await request<Message>(root + "/messages", { method: "POST", body: { content: attempt.text, client_id: attempt.clientId } });
      if (!alive.current) return;
      setMessages(existing => mergeMessages(existing, [message]));
      if (currentDraft.current.clientId === attempt.clientId) setDraft(newDraft());
      bottom.current = true; setAtBottom(true); onChanged();
    } catch (cause) { if (alive.current) fail(cause); }
    finally { if (alive.current) setSending(false); }
  };
  const loadOlder = async () => {
    if (!messages.length || loadingOlder) return;
    setLoadingOlder(true);
    const height = viewport.current?.scrollHeight ?? 0, scroll = viewport.current?.scrollTop ?? 0;
    try {
      const page = await request<Message[]>(root + `/messages?before=${messages[0].sequence}&limit=100`);
      if (!alive.current) return;
      bottom.current = false; setAtBottom(false); setOlder(page.length === 100);
      setMessages(existing => mergeMessages(existing, page));
      requestAnimationFrame(() => { if (viewport.current) viewport.current.scrollTop = scroll + viewport.current.scrollHeight - height; });
    } catch (cause) { if (alive.current) fail(cause); }
    finally { if (alive.current) setLoadingOlder(false); }
  };
  const preference = (work: () => Promise<unknown>) => {
    setBusy(true); setError("");
    void work().then(onChanged).catch(fail).finally(() => { if (alive.current) { setBusy(false); setConfirmBlock(false); } });
  };
  const blockPath = organizationPath(organizationId) + "/direct-blocks/" + encodeURIComponent(conversation.other_user.id);

  const transcript = useMemo(() => {
    const groups = alphaMessageGroups(messages);
    return groups.map((group, groupIndex) => <div key={group[0].id}>
      {(groupIndex === 0 || new Date(groups[groupIndex - 1][0].created_at * 1000).toDateString() !== new Date(group[0].created_at * 1000).toDateString())
        && <div className="org-message-day"><time dateTime={new Date(group[0].created_at * 1000).toISOString().slice(0, 10)}>
          {new Date(group[0].created_at * 1000).toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric", year: "numeric" })}</time></div>}
      <div className="org-message-group org-speaker-human">{group.map((message, index) => {
        const own = message.author_id === user.id;
        return <article key={message.id} id={"direct-message-" + message.sequence} data-sequence={message.sequence}
          className={"org-message " + (index ? "org-message-continuation " : "") + (own ? "org-message-own" : "")}>
          <header>{!index && <strong>{own ? user.name : name}</strong>}
            <time dateTime={new Date(message.created_at * 1000).toISOString()} title={new Date(message.created_at * 1000).toLocaleString()}>
              {new Date(message.created_at * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}</time></header>
          {editing === message.id ? <DirectMessageEdit path={root} message={message} onAuthLost={onAuthLost} onCancel={() => setEditing(null)}
            onSaved={updated => { setMessages(existing => mergeMessages(existing, [updated])); setEditing(null); }} />
            : <MessageMarkdown markdown={normalizeMathDelimiters(mentionMarkdown(message.content, members))} />}
          <div className="org-direct-actions">
            {Boolean(message.revision) && <button type="button" className="chat-edited" title={message.edited_at ? new Date(message.edited_at * 1000).toLocaleString() : undefined}
              onClick={() => void request<{ revision: number; content: string; created_at: number }[]>(root + "/messages/" + encodeURIComponent(message.id) + "/history")
                .then(versions => setHistory({ id: message.id, versions })).catch(fail)}>edited</button>}
            {own && writable && editing !== message.id && <button type="button" aria-label="Edit message" title="Edit message"
              onClick={() => setEditing(message.id)}>Edit</button>}
          </div>
          {history?.id === message.id && <div className="chat-edit-history" aria-label="Message version history">
            <button type="button" onClick={() => setHistory(null)}>Close history</button>
            {history.versions.map(version => <div key={version.revision}><small>{version.revision ? `Revision ${version.revision}` : "Original"} · {new Date(version.created_at * 1000).toLocaleString()}</small><pre>{version.content}</pre></div>)}
          </div>}
        </article>;
      })}</div></div>);
  }, [messages, editing, history, members, user.id, user.name, name, root, writable, onAuthLost]);

  const status = !conversation.other_member ? `${name} is no longer a member of this organization.`
    : conversation.blocked ? `You blocked ${name}. Neither of you can send messages here until you unblock them.`
      : !canWrite ? "You have read-only access." : "";
  return <section className="org-conversation org-direct-conversation" aria-label={`Direct messages with ${name}`}>
    <header className="org-direct-header">
      <div><h2>{name}</h2><small>Only you and {name} can read this conversation. Bots and models never take part.</small></div>
      <div className="org-direct-controls">
        <button type="button" disabled={busy} aria-pressed={conversation.muted} title={conversation.muted ? "Show unread counts again" : "Hide unread counts for this conversation"}
          onClick={() => preference(() => request(root + "/mute", { method: "PUT", body: { muted: !conversation.muted } }))}>{conversation.muted ? "Unmute" : "Mute"}</button>
        {conversation.blocked ? <button type="button" disabled={busy} onClick={() => preference(() => request(blockPath, { method: "DELETE" }))}>Unblock</button>
          : !confirmBlock ? <button type="button" disabled={busy || !conversation.other_member} onClick={() => setConfirmBlock(true)}>Block</button>
            : <span className="org-direct-confirm" role="group" aria-label={`Block ${name}?`}>Block {name}? They will not be able to message you.
              <button type="button" className="org-danger" disabled={busy} onClick={() => preference(() => request(blockPath, { method: "PUT", body: {} }))}>Block</button>
              <button type="button" disabled={busy} onClick={() => setConfirmBlock(false)}>Cancel</button></span>}
      </div>
    </header>
    <div className="org-stream">
      <div className="org-transcript" ref={viewport} tabIndex={0} aria-label="Message history" onScroll={() => {
        const view = viewport.current;
        if (view) { bottom.current = view.scrollHeight - view.scrollTop - view.clientHeight < 64; setAtBottom(bottom.current); }
      }}>
        {older && messages.length > 0 && <button className="org-older" disabled={loadingOlder} onClick={() => void loadOlder()}>{loadingOlder ? "Loading…" : "Load earlier messages"}</button>}
        {loading && <p role="status" className="org-empty">Loading conversation…</p>}
        {!loading && !messages.length && <div className="org-empty"><h2>Send {name} a message</h2><p>New messages show as unread in their Direct messages list.</p></div>}
        {transcript}
      </div>
    </div>
    {!online && <p role="status" className="org-reconnecting">Reconnecting…</p>}
    {error && <p role="alert" className="org-error org-conversation-error">{error}</p>}
    {status && <p role="status" className="org-help org-direct-status">{status}</p>}
    <form className="org-composer" onSubmit={event => void send(event)}>
      <label className="org-visually-hidden" htmlFor="org-direct-message">Message</label>
      <textarea id="org-direct-message" ref={textarea} rows={1} maxLength={16000} value={draft.text} disabled={!writable || sending}
        placeholder={writable ? `Message ${name}…` : "Sending is unavailable"} onChange={event => setDraft({ text: event.target.value,
          clientId: draft.attempted || !draft.clientId ? crypto.randomUUID() : draft.clientId, attempted: false })}
        onKeyDown={event => {
          if (event.nativeEvent.isComposing) return;
          if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void send(); }
        }} />
      <div className="org-composer-controls"><div className="org-composer-actions">
        <button className="org-primary org-send-play" aria-label={sending ? "Sending…" : draft.attempted ? "Retry send" : "Send"}
          title={draft.attempted ? "Retry send" : "Send message"} disabled={!writable || sending || !online || !draft.text.trim()}>
          <span aria-hidden="true">{sending ? "…" : "▶"}</span></button></div></div>
    </form>
  </section>;
}
