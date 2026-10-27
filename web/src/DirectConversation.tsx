// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import React, { useEffect, useMemo, useRef, useState } from "react";
import { DirectConversation, directPath } from "./DirectMessages";
import { normalizeMathDelimiters } from "./markdownMath";
import { MessageMarkdown, alphaMessageGroups, resizeComposer } from "./OrganizationApp";
import { ReturnIcon } from "./ComposerIcons";
import { BotComposerControls, BotSteps, BotWorking, replyRun, useBotChoice, useBotRuns, useOwnBot } from "./BotDirect";
import { ApiError, Account, Member, Message, aborted, errorMessage, mentionMarkdown, mergeMessages, request } from "./organization-api";
import { startPolling } from "./polling";
import { LIVE_SAFETY_POLL_MS, subscribeEvents } from "./topicEvents";
import { ActionMenu, CopyOutputButton, MessageMenuIcon, ReactionControl } from "./ChatFeedback";
import { chatTones, speakerTone } from "./mentions";
import { ReplyBar, ReplyPointer, ThreadDialog } from "./MessageReplies";
import { NewestMessageButton, useUnseenMessages } from "./NewestMessage";
import type { MessageJump } from "./DirectDetails";
import { AttachFilesButton, DirectAttachments, MAX_DIRECT_ATTACHMENTS, PendingFile, PendingFiles, joinAttachments, pendingFile, splitAttachments, uploadDirectFile } from "./DirectMedia";

type Draft = { text: string; clientId: string; attempted: boolean; replyTo?: string; files: PendingFile[] };
const newDraft = (): Draft => ({ text: "", clientId: crypto.randomUUID(), attempted: false, files: [] });
// Unsent text survives switching between conversations for the life of the page.
const drafts = new Map<string, Draft>();

function DirectMessageEdit({ path, message, onSaved, onCancel, onAuthLost }: {
  path: string; message: Message; onSaved: (message: Message) => void; onCancel: () => void; onAuthLost: () => void;
}) {
  // Only the caption is edited; the message keeps its files.
  const parts = splitAttachments(message.content, path);
  const [text, setText] = useState(parts.caption), [busy, setBusy] = useState(false), [error, setError] = useState("");
  const content = parts.files.length ? joinAttachments(parts.files.map(file => `![${file.name}](${file.url})`), text) : text;
  return <form className="chat-edit" onSubmit={event => {
    event.preventDefault(); setBusy(true); setError("");
    void request<Message>(path + "/messages/" + encodeURIComponent(message.id), { method: "PATCH", body: { content, revision: message.revision ?? 0 } })
      .then(onSaved).catch(cause => { if (cause instanceof ApiError && cause.status === 401) onAuthLost(); else setError(errorMessage(cause)); })
      .finally(() => setBusy(false));
  }}>
    <label>{parts.files.length ? "Edit caption" : "Edit message"}<textarea aria-label="Edit message text" autoFocus rows={3} maxLength={16000} disabled={busy}
      value={text} onChange={event => setText(event.target.value)} /></label>
    <button disabled={busy || !content.trim()}>Save edit</button>
    <button type="button" disabled={busy} onClick={onCancel}>Cancel</button>
    {error && <small role="alert" className="org-error">{error}</small>}
  </form>;
}

/**
 * A one-to-one conversation: only its two participants can read it. With another person no bot or model takes part;
 * with your own bot, each message you send asks the bot for an answer, which its runner on your computer writes.
 */
export function DirectConversationView({ organizationId, conversation, user, members, canWrite, jump, onChanged, onAuthLost }: {
  organizationId: string; conversation: DirectConversation; user: Account; members: Member[]; canWrite: boolean;
  jump?: MessageJump; onChanged: () => void; onAuthLost: () => void;
}) {
  const root = directPath(organizationId, conversation.id), name = conversation.other_user.name;
  const [messages, setMessages] = useState<Message[]>([]);
  const [loading, setLoading] = useState(true), [older, setOlder] = useState(false), [loadingOlder, setLoadingOlder] = useState(false);
  const [online, setOnline] = useState(true), [error, setError] = useState(""), [sending, setSending] = useState(false);
  const [draft, setDraftState] = useState<Draft>(() => drafts.get(root) ?? newDraft());
  const [editing, setEditing] = useState<string | null>(null), [history, setHistory] = useState<{ id: string; versions: { revision: number; content: string; created_at: number }[] } | null>(null);
  const [linked, setLinked] = useState<string | null>(null);
  const [atBottom, setAtBottom] = useState(true), [threadFor, setThreadFor] = useState<string | null>(null);
  const viewport = useRef<HTMLDivElement>(null), textarea = useRef<HTMLTextAreaElement>(null);
  const cursor = useRef(0), editCursor = useRef(0), acknowledged = useRef(0), bottom = useRef(true), alive = useRef(true);
  const currentDraft = useRef(draft), currentMessages = useRef(messages);
  currentDraft.current = draft; currentMessages.current = messages;
  const setDraft = (value: Draft) => { drafts.set(root, value); currentDraft.current = value; setDraftState(value); };
  const fail = (cause: unknown) => { if (cause instanceof ApiError && cause.status === 401) onAuthLost(); else setError(errorMessage(cause)); };
  // After "Mark unread from here" the open conversation stops marking itself read until you leave it; the divider shows where.
  const holdRead = useRef(false), [unreadFrom, setUnreadFrom] = useState<number | null>(null);
  const markUnread = (message: Message) => {
    holdRead.current = true; setUnreadFrom(message.sequence);
    void request(root + "/unread", { method: "PUT", body: { message_id: message.id } }).then(() => onChanged())
      .catch(cause => { holdRead.current = false; setUnreadFrom(null); fail(cause); });
  };
  const writable = canWrite && conversation.other_member && !conversation.blocked;
  const botChat = conversation.other_user.kind === "bot";
  const bot = useOwnBot(organizationId, botChat ? conversation.other_user.id : null, onAuthLost);
  const botRuns = useBotRuns(organizationId, root, botChat, messages.at(-1)?.sequence, onAuthLost);
  const choice = useBotChoice(user.id, bot);
  const activeRun = botRuns.runs.find(run => ["queued", "running", "waiting_approval"].includes(run.status));
  const botOffline = botChat && bot !== null && !bot.online;
  // Choosing or cancelling a reply target starts a fresh attempt, so a retry never changes what a sent message answers.
  const startReply = useRef((_id: string) => {});
  startReply.current = (id: string) => {
    setDraft({ ...currentDraft.current, replyTo: id, clientId: crypto.randomUUID(), attempted: false });
    requestAnimationFrame(() => textarea.current?.focus());
  };
  const cancelReply = () => setDraft({ ...currentDraft.current, replyTo: undefined, clientId: crypto.randomUUID(), attempted: false });
  // An upload can finish after its conversation was closed, so it updates the stored draft of the conversation it began in.
  const changeFiles = (target: string, change: (files: PendingFile[]) => PendingFile[]) => {
    const prior = drafts.get(target) ?? (target === root ? currentDraft.current : newDraft());
    const next = { ...prior, files: change(prior.files), ...(prior.attempted ? { clientId: crypto.randomUUID(), attempted: false } : {}) };
    drafts.set(target, next);
    if (alive.current && target === root) { currentDraft.current = next; setDraftState(next); }
  };
  const attach = (chosen: File[]) => {
    const room = MAX_DIRECT_ATTACHMENTS - currentDraft.current.files.length;
    setError(chosen.length > room ? `A message can carry at most ${MAX_DIRECT_ATTACHMENTS} files.` : "");
    const pending = chosen.slice(0, Math.max(0, room)).map(file => ({ file, item: pendingFile(file) }));
    changeFiles(root, files => [...files, ...pending.map(({ item }) => item)]);
    const settle = (key: string, value: Partial<PendingFile>) => changeFiles(root, files => files.map(file => file.key === key ? { ...file, ...value } : file));
    for (const { file, item } of pending) {
      void uploadDirectFile(file, root).then(value => settle(item.key, { markdown: value.markdown }), cause => {
        if (cause instanceof ApiError && cause.status === 401) onAuthLost();
        settle(item.key, { error: errorMessage(cause) });
      });
    }
  };
  const removeFile = (key: string) => changeFiles(root, files => files.filter(file => {
    if (file.key === key && file.preview) URL.revokeObjectURL(file.preview);
    return file.key !== key;
  }));
  const outgoing = (value: Draft) => value.files.length
    ? joinAttachments(value.files.flatMap(file => file.markdown ? [file.markdown] : []), value.text) : value.text;
  const uploading = draft.files.some(file => !file.markdown);
  const canSend = writable && !sending && online && !uploading && Boolean(outgoing(draft).trim());
  const messageBody = (message: Message) => {
    const { files, caption } = splitAttachments(message.content, root);
    return <>{files.length > 0 && <DirectAttachments files={files} />}
      {caption && <MessageMarkdown markdown={normalizeMathDelimiters(mentionMarkdown(caption, members))} />}</>;
  };
  const copyText = (message: Message) => {
    const { files, caption } = splitAttachments(message.content, root);
    return caption || files.map(file => file.name).join(", ");
  };
  const authorName = (message: Message) => message.author_id === user.id ? "You" : name;
  const unseen = useUnseenMessages(messages, atBottom, user.id);
  // A direct conversation's group is its two participants, so they always differ.
  const tones = useMemo(() => chatTones([user.id, conversation.other_user.id]), [user.id, conversation.other_user.id]);
  const replyOriginal = draft.replyTo ? messages.find(message => message.id === draft.replyTo) : undefined;
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => { if (textarea.current) resizeComposer(textarea.current); }, [draft.text]);

  useEffect(() => {
    const controller = new AbortController();
    // The conversation's event stream announces new messages and edits; polling then fetches exactly what is missing.
    let live = false, loaded = false, reactionCursor = 0;
    let watermark: { sequence: number; updates: number; reactions?: number } = { sequence: 0, updates: 0 };
    const behind = () => watermark.sequence > cursor.current || watermark.updates > editCursor.current
      || (watermark.reactions ?? 0) > reactionCursor;
    const poll = async (): Promise<number | null> => {
      const before = [cursor.current, editCursor.current, reactionCursor];
      try {
        const query = cursor.current ? `after=${cursor.current}&limit=200` : `before=${Number.MAX_SAFE_INTEGER}&limit=100`;
        const [incoming, updates] = await Promise.all([
          request<Message[]>(root + "/messages?" + query, { signal: controller.signal }),
          request<{ after: number; reaction_after?: number; messages: Message[] }>(root + "/message-updates?after=" + editCursor.current
            + "&reaction_after=" + reactionCursor, { signal: controller.signal }),
        ]);
        if (controller.signal.aborted) return null;
        if (!cursor.current) setOlder(incoming.length === 100);
        cursor.current = incoming.at(-1)?.sequence ?? cursor.current;
        editCursor.current = updates.after;
        reactionCursor = updates.reaction_after ?? reactionCursor;
        setMessages(existing => mergeMessages(mergeMessages(existing, incoming), updates.messages.filter(message =>
          existing.some(prior => prior.id === message.id) || incoming.some(prior => prior.id === message.id))));
        setOnline(true); setLoading(false); loaded = true;
        if (incoming.some(message => message.client_id === currentDraft.current.clientId && message.author_id === user.id)) setDraft(newDraft());
        if (behind() && (cursor.current !== before[0] || editCursor.current !== before[1] || reactionCursor !== before[2])) return 0;
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
      if (holdRead.current || !bottom.current || document.visibilityState !== "visible" || !document.hasFocus() || sequence <= acknowledged.current
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
    if (!canSend) return;
    const attempt = { ...draft, clientId: draft.clientId || crypto.randomUUID(), attempted: true };
    setDraft(attempt); setSending(true); setError("");
    try {
      const message = await request<Message>(root + "/messages", { method: "POST", body: {
        content: outgoing(attempt), client_id: attempt.clientId, ...(attempt.replyTo ? { reply_to: attempt.replyTo } : {}),
        ...(botChat && choice.model ? { bot_model: choice.model, ...(choice.effort ? { effort: choice.effort } : {}) } : {}) } });
      if (botChat) botRuns.reload();
      if (!alive.current) return;
      setMessages(existing => mergeMessages(existing, [message]));
      if (currentDraft.current.clientId === attempt.clientId) {
        attempt.files.forEach(file => file.preview && URL.revokeObjectURL(file.preview));
        setDraft(newDraft());
      }
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
  // "Show in chat" from the files list: older pages load until the sharing message is present, then it is scrolled to and marked.
  useEffect(() => {
    if (!jump) return;
    let cancelled = false;
    void (async () => {
      let loaded = currentMessages.current;
      for (let page = 0; page < 20 && (!loaded.length || loaded[0].sequence > jump.sequence); page++) {
        const before = loaded[0]?.sequence ?? Number.MAX_SAFE_INTEGER, earlier = await request<Message[]>(root + `/messages?before=${before}&limit=100`);
        if (cancelled || !earlier.length) break;
        loaded = mergeMessages(loaded, earlier);
        setOlder(earlier.length === 100);
        setMessages(existing => mergeMessages(existing, earlier));
      }
      if (cancelled) return;
      bottom.current = false; setAtBottom(false); setLinked(jump.id);
    })().catch(cause => { if (!cancelled) fail(cause); });
    return () => { cancelled = true; };
  }, [jump?.nonce]);
  useEffect(() => {
    if (!linked) return;
    viewport.current?.querySelector(`[data-message-id="${linked}"]`)?.scrollIntoView({ block: "center" });
    const timer = setTimeout(() => setLinked(null), 2600);
    return () => clearTimeout(timer);
  }, [linked]);

  // The same layout as topic chats: speaker groups in each person's color, a floating toolbar, a reaction pill,
  // and reply pointers that open the whole thread. Your own groups are mirrored to the right.
  const transcript = useMemo(() => {
    const groups = alphaMessageGroups(messages);
    const byId = new Map(messages.map(message => [message.id, message]));
    return groups.map((group, groupIndex) => <div key={group[0].id}>
      {(groupIndex === 0 || new Date(groups[groupIndex - 1][0].created_at * 1000).toDateString() !== new Date(group[0].created_at * 1000).toDateString())
        && <div className="org-message-day"><time dateTime={new Date(group[0].created_at * 1000).toISOString().slice(0, 10)}>
          {new Date(group[0].created_at * 1000).toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric", year: "numeric" })}</time></div>}
      <div className={"org-message-group org-speaker-human " + speakerTone(group[0], tones) + (group[0].author_id === user.id ? " org-message-group-mine" : "")}>{group.map((message, index) => {
        const own = message.author_id === user.id, original = message.reply_to ? byId.get(message.reply_to) : undefined;
        return <React.Fragment key={message.id}>{message.sequence === unreadFrom && <div className="org-unread-divider" role="separator" aria-label="New messages"><span>New</span></div>}
          <article id={"direct-message-" + message.sequence} data-sequence={message.sequence} data-message-id={message.id}
          className={"org-message " + (index ? "org-message-continuation " : "") + (own ? "org-message-own" : "") + (message.id === linked ? " org-message-linked" : "")}>
          {message.reply_to && <ReplyPointer original={original} label={original && authorName(original)} onOpen={() => setThreadFor(message.id)} />}
          <header>{!index && <strong>{own ? "You" : name}</strong>}
            <time dateTime={new Date(message.created_at * 1000).toISOString()} title={new Date(message.created_at * 1000).toLocaleString()}>
              {new Date(message.created_at * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}</time></header>
          {editing === message.id ? <DirectMessageEdit path={root} message={message} onAuthLost={onAuthLost} onCancel={() => setEditing(null)}
            onSaved={updated => { setMessages(existing => mergeMessages(existing, [updated])); setEditing(null); }} />
            : messageBody(message)}
          {botChat && message.author_kind === "bot" && (() => { const run = replyRun(botRuns.runs, message.client_id); return run ? <BotSteps run={run} /> : null; })()}
          {botChat && own && botOffline && (() => {
            const queued = botRuns.runs.find(run => run.status === "queued" && run.context_through_sequence === message.sequence);
            return queued && <small className="org-bot-waiting">waiting for {name}
              <button type="button" className="org-bot-waiting-cancel" onClick={() => void botRuns.stop(queued)}>Cancel</button></small>;
          })()}
          <div className={"chat-message-actions chat-has-copy chat-has-fork" + (writable ? " chat-has-reply chat-has-react" : "")}>
            <ReactionControl path={root + "/messages/" + encodeURIComponent(message.id) + "/reactions"}
              canWrite={writable} onAuthLost={onAuthLost} batched preloaded={message.reactions} modelReply={message.author_kind === "bot"} />
            {writable && <button type="button" className="chat-action-launcher chat-reply-launcher" aria-label="Reply" title="Reply"
              onClick={event => { event.preventDefault(); startReply.current(message.id); }}>↩</button>}
            <ActionMenu label="Message actions" className="chat-fork-launcher" icon={<MessageMenuIcon />}>
              {close => <><button type="button" role="menuitem" onClick={() => { close(); markUnread(message); }}>Mark unread from here</button>
                {own && writable && editing !== message.id && <button type="button" role="menuitem"
                onClick={() => { close(); setEditing(message.id); }}>Edit message</button>}
                {Boolean(message.revision) && <button type="button" role="menuitem" onClick={() => {
                  close();
                  void request<{ revision: number; content: string; created_at: number }[]>(root + "/messages/" + encodeURIComponent(message.id) + "/history")
                    .then(versions => setHistory({ id: message.id, versions })).catch(fail);
                }}>Version history</button>}</>}
            </ActionMenu>
            <CopyOutputButton text={copyText(message)} />
            {Boolean(message.revision) && <small className="chat-edited" title={message.edited_at ? new Date(message.edited_at * 1000).toLocaleString() : undefined}>edited</small>}
          </div>
          {history?.id === message.id && <div className="chat-edit-history" aria-label="Message version history">
            <button type="button" onClick={() => setHistory(null)}>Close history</button>
            {history.versions.map(version => <div key={version.revision}><small>{version.revision ? `Revision ${version.revision}` : "Original"} · {new Date(version.created_at * 1000).toLocaleString()}</small><pre>{version.content}</pre></div>)}
          </div>}
        </article></React.Fragment>;
      })}</div></div>);
  }, [messages, editing, history, members, user.id, name, root, writable, tones, linked, onAuthLost, unreadFrom, botChat, botRuns.runs, botOffline]);

  const attachControl = <span className="org-composer-attach"><AttachFilesButton disabled={!writable || sending || draft.files.length >= MAX_DIRECT_ATTACHMENTS} onFiles={attach} /></span>;
  const status = !conversation.other_member ? (botChat ? `${name} is no longer available. Its access was revoked or expired.` : `${name} is no longer a member of this organization.`)
    : conversation.blocked ? `You blocked ${name}. Neither of you can send messages here until you unblock them.`
      : !canWrite ? "You have read-only access." : "";
  return <section className="org-conversation org-direct-conversation" aria-label={`Direct messages with ${name}`}>
    <div className="org-stream">
      <div className="org-transcript" ref={viewport} tabIndex={0} aria-label="Message history" onScroll={() => {
        const view = viewport.current;
        if (view) { bottom.current = view.scrollHeight - view.scrollTop - view.clientHeight < 64; setAtBottom(bottom.current); }
      }}>
        {older && messages.length > 0 && <button className="org-older" disabled={loadingOlder} onClick={() => void loadOlder()}>{loadingOlder ? "Loading…" : "Load earlier messages"}</button>}
        {loading && <p role="status" className="org-empty">Loading conversation…</p>}
        {!loading && !messages.length && <div className="org-empty"><h2>Send {name} a message</h2><p>{botChat
          ? `Only you can read this conversation. ${name} answers through its runner on your computer.`
          : "New messages show as unread in their Direct messages list."}</p></div>}
        {transcript}
        {botChat && activeRun && !(activeRun.status === "queued" && botOffline) && <BotWorking run={activeRun} name={name} online={!botOffline}
          approvals={botRuns.approvals} onDecide={(approval, allow) => void botRuns.decide(approval, allow)}
          onStop={() => void botRuns.stop(activeRun)} />}
      </div>
      {!atBottom && messages.length > 0 && <NewestMessageButton unseen={unseen} onJump={() => {
        bottom.current = true; setAtBottom(true); if (viewport.current) viewport.current.scrollTop = viewport.current.scrollHeight;
      }} />}
    </div>
    {!online && <p role="status" className="org-reconnecting">Reconnecting…</p>}
    {error && <p role="alert" className="org-error org-conversation-error">{error}</p>}
    {botRuns.error && <p role="alert" className="org-error org-conversation-error">{botRuns.error}</p>}
    {botOffline && <p role="status" className="org-bot-offline">⏸ {name} is offline. Your messages wait and are sent when its runner reconnects.</p>}
    {status && <p role="status" className="org-help org-direct-status">{status}</p>}
    {threadFor && <ThreadDialog key={threadFor} messagesPath={root + "/messages"} messageId={threadFor} canWrite={writable}
      onClose={() => setThreadFor(null)} onAuthLost={onAuthLost} onPosted={message => setMessages(existing => mergeMessages(existing, [message]))}
      live={messages} attach={writable ? file => uploadDirectFile(file, root) : undefined}
      renderMessage={message => <div className={"org-message-group org-speaker-human " + speakerTone(message, tones)}><article className="org-message">
        <header><strong>{authorName(message)}</strong>
          <time dateTime={new Date(message.created_at * 1000).toISOString()}>{new Date(message.created_at * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}</time></header>
        {messageBody(message)}</article></div>} />}
    <form className="org-composer" onSubmit={event => void send(event)}>
      <div className="org-composer-box">
      {draft.replyTo && <ReplyBar original={replyOriginal} label={replyOriginal && authorName(replyOriginal)} onCancel={cancelReply} />}
      <PendingFiles files={draft.files} onRemove={removeFile} />
      <label className="org-visually-hidden" htmlFor="org-direct-message">Message</label>
      {/* With a person the plus is inside the box; with your bot it sits under the box beside the model, as in an AI chat. */}
      <div className="org-composer-row">
        {!botChat && attachControl}
        <div className="org-composer-input"><textarea id="org-direct-message" ref={textarea} rows={1} maxLength={16000} value={draft.text} disabled={!writable || sending}
          placeholder={writable ? `Message ${name}…` : "Sending is unavailable"} onChange={event => setDraft({ ...draft, text: event.target.value,
            clientId: draft.attempted || !draft.clientId ? crypto.randomUUID() : draft.clientId, attempted: false })}
          onKeyDown={event => {
            if (event.nativeEvent.isComposing) return;
            if (event.key === "Escape" && draft.replyTo) { cancelReply(); return; }
            if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void send(); }
          }} /></div>
        <div className="org-composer-send">
          <button className="org-composer-button org-send-play" aria-label={sending ? "Sending…" : uploading ? "Waiting for uploads" : draft.attempted ? "Retry send" : "Send"}
            title={uploading ? "Wait for the uploads to finish, or remove the failed ones" : draft.attempted ? "Retry send" : "Send message"} disabled={!canSend}>
            {sending ? <span aria-hidden="true">…</span> : <ReturnIcon />}</button></div>
      </div>
      </div>
      {botChat && <BotComposerControls attach={attachControl} bot={bot} choice={choice} disabled={!writable || sending} />}
    </form>
  </section>;
}
