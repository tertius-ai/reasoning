import { ReactNode, useContext, useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ApiError, Message, errorMessage, organizationPath, request, topicPath } from "./organization-api";
import { encodeMessageEdit, MessageEditDraft, readableMessageEdit, updateMessageEdit } from "./messageEditorMentions";
import { ReferenceContext, messageLink } from "./references";
import "./chat-feedback.css";

// The server's canonical order (reaction_emoji_schema.EMOJIS): the first six are the quick choices, as in Signal.
const emojis = {
  up: "👍", down: "👎", heart: "❤️", laugh: "😂", surprised: "😮", sad: "😢", upside_down: "🙃", party: "🎉", thinking: "🤔", eyes: "👀",
  check: "✅", cross: "❌", fire: "🔥", idea: "💡", pray: "🙏", clap: "👏", rocket: "🚀", hundred: "💯", star: "⭐", question: "❓",
  wave: "👋", smile: "🙂", wink: "😉", confused: "😕",
} as const;
const labels: Record<keyof typeof emojis, string> = {
  up: "Thumbs up", down: "Thumbs down", heart: "Heart", laugh: "Laughing", surprised: "Surprised", sad: "Sad", upside_down: "Upside-down smile",
  party: "Party", thinking: "Thinking", eyes: "Eyes", check: "Check mark", cross: "Cross mark", fire: "Fire", idea: "Light bulb", pray: "Folded hands",
  clap: "Clapping", rocket: "Rocket", hundred: "Hundred points", star: "Star", question: "Question mark", wave: "Waving hand", smile: "Smile",
  wink: "Wink", confused: "Confused",
};
type Emoji = keyof typeof emojis;
const ALL = Object.keys(emojis) as Emoji[], QUICK = ALL.slice(0, 6);
/** A model's reply takes only these (reaction_emoji_schema.MODEL_REPLY_EMOJIS): useful, not useful, confidently wrong. */
const MODEL_REPLY: readonly Emoji[] = ["up", "down", "upside_down"];
export type Feedback = { counts: Partial<Record<Emoji, number>>; mine: Emoji[]; reactors?: Partial<Record<Emoji, { id: string; name: string }[]>> };
const count = (feedback: Feedback, emoji: Emoji) => feedback.counts[emoji] ?? 0;
/** Signal's reaction mark: a smiling face with a small plus. */
const ReactionIcon = () => <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" aria-hidden="true">
  <path d="M20.5 11.5a8.5 8.5 0 1 1-6-8.13" /><path d="M8.5 14.5a4.5 4.5 0 0 0 7 0" /><path d="M9 9.5h.01M15 9.5h.01" /><path d="M19 2.5v5M16.5 5h5" /></svg>;

export function CopyOutputButton({ text }: { text: string | (() => string) }) {
  const [error, setError] = useState("");
  return <>
    <button type="button" className="chat-action-launcher chat-copy-launcher" aria-label="Copy message" title="Copy message"
      onClick={event => {
        event.preventDefault(); event.stopPropagation(); setError("");
        void (async () => {
          try { await navigator.clipboard.writeText(typeof text === "function" ? text() : text); }
          catch { setError("Could not copy. Select the message text and copy it manually."); }
        })();
      }}><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true"><rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3"/></svg></button>
    {error && <small role="alert" className="org-error">{error}</small>}
  </>;
}

export function OutputActions({ text, ...props }: Parameters<typeof ReactionBar>[0] & { text: string }) {
  return <div className={"chat-message-actions chat-output-actions chat-has-copy" + (props.canWrite !== false ? " chat-has-react" : "")}>
    <ReactionBar {...props} /><CopyOutputButton text={text} /></div>;
}

/** The "Message actions" mark shared by topic and direct-message toolbars. */
export const MessageMenuIcon = () => <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true"><circle cx="7" cy="5" r="2"/><circle cx="17" cy="5" r="2"/><circle cx="7" cy="19" r="2"/><path d="M7 7v10m10-10v3a4 4 0 0 1-4 4H7"/></svg>;

export function ActionMenu({ label, className, icon, menuClassName = "", onClosed, children }: {
  label: string; className: string; icon: ReactNode; menuClassName?: string; onClosed?: () => void; children: (close: () => void) => ReactNode;
}) {
  const [position, setPosition] = useState<{ left: number; top: number } | null>(null);
  const trigger = useRef<HTMLButtonElement>(null), menu = useRef<HTMLDivElement>(null);
  const open = Boolean(position);
  const close = () => { setPosition(null); trigger.current?.focus({ preventScroll: true }); };
  const reposition = () => {
    if (!menu.current || !trigger.current) return;
    const box = menu.current.getBoundingClientRect(), button = trigger.current.getBoundingClientRect();
    const left = Math.max(8, Math.min(button.right - box.width, window.innerWidth - box.width - 8));
    const top = button.bottom + box.height + 12 <= window.innerHeight
      ? button.bottom + 4 : Math.max(8, Math.min(button.top - box.height - 4, window.innerHeight - box.height - 8));
    setPosition(current => current && (Math.abs(top - current.top) > .5 || Math.abs(left - current.left) > .5)
      ? { left, top } : current);
  };
  useLayoutEffect(() => {
    if (!open) { onClosed?.(); return; }
    reposition();
  }, [open]);
  useEffect(() => {
    if (!open) return;
    menu.current?.querySelector<HTMLButtonElement>("button:not(:disabled)")?.focus({ preventScroll: true });
    const outside = (event: Event) => {
      if (!menu.current?.contains(event.target as Node) && !trigger.current?.contains(event.target as Node)) setPosition(null);
    };
    const dismiss = () => setPosition(null);
    document.addEventListener("pointerdown", outside); document.addEventListener("focusin", outside);
    // Programmatic scrolling (polling, layout, focus, accessibility tools) must
    // keep the open menu intact. Only deliberate scrolling outside it dismisses.
    const userScroll = (event: Event) => { if (!menu.current?.contains(event.target as Node)) dismiss(); };
    const scroll = () => reposition();
    const size = new ResizeObserver(reposition); if (menu.current) size.observe(menu.current);
    window.addEventListener("resize", dismiss); document.addEventListener("scroll", scroll, true);
    document.addEventListener("wheel", userScroll, { capture: true, passive: true });
    document.addEventListener("touchmove", userScroll, { capture: true, passive: true });
    return () => {
      size.disconnect();
      document.removeEventListener("pointerdown", outside); document.removeEventListener("focusin", outside);
      window.removeEventListener("resize", dismiss); document.removeEventListener("scroll", scroll, true);
      document.removeEventListener("wheel", userScroll, true); document.removeEventListener("touchmove", userScroll, true);
    };
  }, [open]);
  return <>
    <button ref={trigger} type="button" className={`chat-action-launcher ${className}`} aria-label={label}
      title={label} aria-haspopup="menu" aria-expanded={Boolean(position)} onClick={() => {
        if (position) { setPosition(null); return; }
        const box = trigger.current!.getBoundingClientRect();
        setPosition({ left: Math.max(8, Math.min(box.right - 176, window.innerWidth - 184)),
          top: box.bottom + 128 <= window.innerHeight ? box.bottom + 4 : Math.max(8, box.top - 124) });
      }}>{icon}</button>
    {position && createPortal(<div ref={menu} className={"chat-action-menu " + menuClassName} role="menu" aria-label={label}
      style={position} onKeyDown={event => {
        if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); close(); }
        if (["ArrowDown", "ArrowUp", "ArrowRight", "ArrowLeft", "Home", "End"].includes(event.key)) {
          event.preventDefault();
          const buttons = Array.from(menu.current!.querySelectorAll<HTMLButtonElement>("button:not(:disabled)"));
          const current = buttons.indexOf(document.activeElement as HTMLButtonElement);
          const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1
            : (current + (event.key === "ArrowDown" || event.key === "ArrowRight" ? 1 : -1) + buttons.length) % buttons.length;
          buttons[next]?.focus({ preventScroll: true });
        }
      }}>{children(close)}</div>, document.body)}
  </>;
}

/**
 * Reactions as one pill on the message's bottom edge, as in Signal: each emoji in use (most used first), then the total.
 * Each emoji is a button naming who reacted; pressing it sets or clears your reaction.
 */
export function ReactionChips({ feedback, canWrite, busy, onToggle }: {
  feedback: Feedback; canWrite: boolean; busy: boolean; onToggle: (emoji: Emoji) => void;
}) {
  const used = ALL.filter(emoji => count(feedback, emoji) > 0).sort((left, right) => count(feedback, right) - count(feedback, left));
  if (!used.length) return null;
  const total = used.reduce((sum, emoji) => sum + count(feedback, emoji), 0);
  return <span className="chat-reaction-pill">{used.map(emoji => {
    const people = feedback.reactors?.[emoji] ?? [], extra = Math.max(0, count(feedback, emoji) - people.length);
    const names = people.map(person => person.name).join(", ") + (extra ? ` and ${extra} others` : "");
    return <button key={emoji} type="button" className="chat-reaction-chip" aria-label={`${labels[emoji]}: ${names || count(feedback, emoji)}`}
      title={`${labels[emoji]}: ${names || count(feedback, emoji)}`} data-reactors={names} aria-pressed={feedback.mine.includes(emoji)}
      aria-disabled={!canWrite || busy} disabled={busy} onClick={() => { if (canWrite && !busy) onToggle(emoji); }}>{emojis[emoji]}</button>;
  })}{total > 1 && <span className="chat-reaction-total" aria-hidden="true">{total}</span>}</span>;
}

export function ReactionBar({ organizationId, topicId, targetKind, targetId, nodeId, canWrite = true, onAuthLost, batched = false, preloaded, modelReply = false }: {
  organizationId: string; topicId: string; targetKind: "message" | "run" | "bot" | "node"; targetId: string; nodeId?: string;
  canWrite?: boolean; onAuthLost?: () => void;
  /** A message written by a model or bot; runs, bot runs and workflow results are always model replies. */
  modelReply?: boolean;
  /** The conversation reads this target's reactions in one request for its whole message page. */
  batched?: boolean; preloaded?: Feedback;
}) {
  const path = topicPath(organizationId, topicId) + "/reactions/" + targetKind + "/" + encodeURIComponent(targetId)
    + (nodeId ? "?node_id=" + encodeURIComponent(nodeId) : "");
  return <ReactionControl {...{ path, canWrite, onAuthLost, batched, preloaded }} modelReply={modelReply || targetKind !== "message"} />;
}

export function ReactionControl({ path, canWrite = true, onAuthLost, batched = false, preloaded, modelReply = false }: {
  path: string; canWrite?: boolean; onAuthLost?: () => void; batched?: boolean; preloaded?: Feedback;
  /** A model's, bot's or workflow's reply: the picker offers only 👍 👎 🙃, without "+". */
  modelReply?: boolean;
}) {
  const [feedback, setFeedback] = useState<Feedback>(preloaded ?? { counts: {}, mine: [] });
  const [busy, setBusy] = useState(false), [error, setError] = useState(""), [more, setMore] = useState(false);
  useEffect(() => { if (preloaded) setFeedback(preloaded); }, [preloaded]);
  useEffect(() => {
    if (batched) return;
    const controller = new AbortController();
    void request<Feedback>(path, { signal: controller.signal }).then(setFeedback).catch(cause => {
      if (!controller.signal.aborted && cause instanceof ApiError && cause.status === 401) onAuthLost?.();
    });
    return () => controller.abort();
  }, [path, onAuthLost, batched]);
  const toggle = (emoji: Emoji) => {
    setBusy(true); setError("");
    void request<Feedback>(path, { method: "PUT", body: { emoji, active: !feedback.mine.includes(emoji) } })
      .then(setFeedback).catch(cause => {
        if (cause instanceof ApiError && cause.status === 401) onAuthLost?.(); else setError(errorMessage(cause));
      }).finally(() => setBusy(false));
  };
  return <span className="chat-reactions" aria-label="Human reactions">
    <ReactionChips {...{ feedback, canWrite, busy }} onToggle={toggle} />
    {canWrite && <ActionMenu label="Add reaction" className="chat-reaction-launcher" icon={<ReactionIcon />}
      menuClassName={"chat-reaction-menu" + (more ? " chat-reaction-menu-all" : "")} onClosed={() => setMore(false)}>
      {close => <>{(modelReply ? MODEL_REPLY : more ? ALL : QUICK).map(emoji => <button key={emoji} type="button" role="menuitemcheckbox"
        aria-label={labels[emoji]} title={labels[emoji]} aria-checked={feedback.mine.includes(emoji)} disabled={busy}
        onClick={() => { close(); setMore(false); toggle(emoji); }}>{emojis[emoji]}</button>)}
        {!more && !modelReply && <button type="button" role="menuitem" className="chat-reaction-more" aria-label="More reactions" title="More reactions"
          onClick={event => {
            const menu = event.currentTarget.parentElement; setMore(true);
            requestAnimationFrame(() => menu?.querySelector<HTMLButtonElement>("button:not(:disabled)")?.focus({ preventScroll: true }));
          }}>+</button>}</>}
    </ActionMenu>}
    {error && <small role="alert" className="org-error">{error}</small>}
  </span>;
}

export function MessageActions({ organizationId, topicId, messageId, kind, canWrite, onForked, onAuthLost, message, canEdit, onEdited, onReplyAgain, onReply, onMarkUnread, people = [], reactions, batchedReactions = false }: {
  organizationId: string; topicId: string; messageId: string; kind: string; canWrite: boolean;
  reactions?: Feedback; batchedReactions?: boolean;
  onForked?: (topicId: string) => void; onAuthLost?: () => void;
  message?: Message; canEdit?: boolean; onEdited?: (message: Message) => void;
  onReplyAgain?: () => Promise<void>;
  /** Starts a reply to this message in the composer. */
  onReply?: () => void;
  /** Moves your read point back to just before this message. */
  onMarkUnread?: () => void;
  people?: { id: string; name: string }[];
}) {
  const [open, setOpen] = useState(false), [name, setName] = useState(""), [busy, setBusy] = useState(false), [error, setError] = useState("");
  const [clientId, setClientId] = useState("");
  const [editing, setEditing] = useState(false), [editContent, setEditContent] = useState<MessageEditDraft>({ text: "", bindings: [] });
  const [editRevision, setEditRevision] = useState(0), [showHistory, setShowHistory] = useState(false);
  const [history, setHistory] = useState<{ revision: number; content: string; created_at: number }[]>([]);
  const [linkStatus, setLinkStatus] = useState("");
  useEffect(() => {
    if (!linkStatus) return;
    const timer = setTimeout(() => setLinkStatus(""), 4000);
    return () => clearTimeout(timer);
  }, [linkStatus]);
  const references = useContext(ReferenceContext);
  const reference = (kind: "topic" | "project", id: string) => references?.text(kind, id) ?? "#reference";
  const path = topicPath(organizationId, topicId) + "/messages/" + encodeURIComponent(messageId);
  const fail = (cause: unknown) => {
    if (cause instanceof ApiError && cause.status === 401) onAuthLost?.(); else setError(errorMessage(cause));
  };
  // Every persisted message offers its link; forking, editing and history depend on access and revisions.
  const hasMenu = Boolean(message) || (kind === "beta" && canWrite) || canEdit;
  return <div className={`chat-message-actions${hasMenu ? " chat-has-fork" : ""}${message ? " chat-has-copy" : ""}${onReply ? " chat-has-reply" : ""}${canWrite ? " chat-has-react" : ""}`}>
    <ReactionBar {...{ organizationId, topicId, canWrite, onAuthLost }} targetKind="message" targetId={messageId}
      batched={batchedReactions} preloaded={reactions} modelReply={message?.author_kind === "assistant" || message?.author_kind === "bot"} />
    {onReply && <button type="button" className="chat-action-launcher chat-reply-launcher" aria-label="Reply" title="Reply"
      onClick={event => { event.preventDefault(); onReply(); }}>↩</button>}
    {hasMenu && <ActionMenu label="Message actions" className="chat-fork-launcher" icon={<MessageMenuIcon />}>
      {close => <>{message && <button type="button" role="menuitem" onClick={() => {
        close(); setLinkStatus("");
        void navigator.clipboard.writeText(messageLink(window.location.origin, organizationId, topicId, messageId))
          .then(() => setLinkStatus("Link copied."))
          .catch(() => setLinkStatus("Could not copy the link."));
      }}>Copy link</button>}
      {onMarkUnread && <button type="button" role="menuitem" onClick={() => { close(); onMarkUnread(); }}>Mark unread from here</button>}
      {kind === "beta" && canWrite && <button type="button" role="menuitem" onClick={() => {
        setOpen(true); if (!clientId) setClientId(crypto.randomUUID()); close();
      }}>Fork here</button>}
      {canEdit && message && <button type="button" role="menuitem" onClick={() => {
        setEditContent(readableMessageEdit(message.content, people, reference)); setEditRevision(message.revision ?? 0); setEditing(true); setError(""); close();
      }}>Edit message</button>}
      {canEdit && onReplyAgain && <button type="button" role="menuitem" disabled={busy} onClick={() => {
        close(); setBusy(true); setError("");
        void onReplyAgain().catch(fail).finally(() => setBusy(false));
      }}>Reply again with selected model</button>}
      {Boolean(message?.revision) && <button type="button" role="menuitem" onClick={() => {
        setShowHistory(true); setError(""); close();
        void request<typeof history>(path + "/history").then(setHistory).catch(fail);
      }}>Version history</button>}</>}
    </ActionMenu>}
    {message && <CopyOutputButton text={() => readableMessageEdit(message.content, people, reference).text} />}
    {linkStatus && <small role="status" className="chat-link-status">{linkStatus}</small>}
    {Boolean(message?.revision) && <small className="chat-edited" title={message?.edited_at ? new Date(message.edited_at * 1000).toLocaleString() : undefined}>edited</small>}
    {editing && <form className="chat-edit" onSubmit={event => {
      event.preventDefault(); setBusy(true); setError("");
      void request<Message>(path, { method: "PATCH", body: { content: encodeMessageEdit(editContent), revision: editRevision } })
        .then(updated => { onEdited?.(updated); setEditing(false); }).catch(fail).finally(() => setBusy(false));
    }}>
      <label>Edit message<textarea aria-label="Edit message text" autoFocus rows={3} maxLength={16000} disabled={busy}
        value={editContent.text} onChange={event => setEditContent(updateMessageEdit(editContent, event.target.value))} /></label>
      <small>Existing responses keep their original inputs. Saving does not run a model or bot; fork or request a new reply to continue.</small>
      <button disabled={busy || !editContent.text.trim()}>Save edit</button>
      <button type="button" disabled={busy} onClick={() => setEditing(false)}>Cancel</button>
      {error && <small role="alert" className="org-error">{error}</small>}
    </form>}
    {showHistory && <div className="chat-edit-history" aria-label="Message version history">
      <button type="button" onClick={() => setShowHistory(false)}>Close history</button>
      {history.map(version => <div key={version.revision}><small>{version.revision ? `Revision ${version.revision}` : "Original"} · {new Date(version.created_at * 1000).toLocaleString()}</small><pre>{readableMessageEdit(version.content, people, reference).text}</pre></div>)}
      {error && <small role="alert" className="org-error">{error}</small>}
    </div>}
    {!editing && !showHistory && !open && error && <small role="alert" className="org-error">{error}</small>}
    {open && <form className="chat-fork" onSubmit={event => {
      event.preventDefault(); setBusy(true); setError("");
      void request<{ topic: { id: string } }>(topicPath(organizationId, topicId) + "/forks", {
        method: "POST", body: { message_id: messageId, name: name.trim(), client_id: clientId },
      }).then(result => {
        setOpen(false); setClientId(""); onForked?.(result.topic.id);
        if (!onForked) window.location.href = `/organization.html?organization=${encodeURIComponent(organizationId)}&topic=${encodeURIComponent(result.topic.id)}`;
      }).catch(cause => {
        if (cause instanceof ApiError && cause.status === 401) onAuthLost?.(); else setError(errorMessage(cause));
      }).finally(() => setBusy(false));
    }}>
      <label>New beta-topic name<input required maxLength={200} value={name} disabled={busy} onChange={event => {
        setName(event.target.value); setClientId(crypto.randomUUID());
      }} /></label>
      <small>New conversation through this message; shared project knowledge. No ledger publication, later memory, or running jobs.</small>
      <button disabled={busy || !name.trim()}>Create fork</button>
      <button type="button" disabled={busy} onClick={() => setOpen(false)}>Cancel</button>
      {error && <small role="alert" className="org-error">{error}</small>}
    </form>}
  </div>;
}

export function ModelFeedbackPanel({ organizationId, projectId }: { organizationId: string; projectId?: string }) {
  const [rows, setRows] = useState<({ model: string } & Record<keyof typeof emojis, number>)[]>([]);
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    void request<{ models: typeof rows }>(organizationPath(organizationId) + "/model-feedback"
      + (projectId ? "?project_id=" + encodeURIComponent(projectId) : ""), { signal: controller.signal })
      .then(result => { setRows(result.models); setError(""); })
      .catch(cause => { if (!controller.signal.aborted) setError(errorMessage(cause)); });
    return () => controller.abort();
  }, [organizationId, projectId]);
  return <section className="chat-feedback-table" aria-label="Human feedback by model"><h3>Human feedback by model</h3>
    <table><thead><tr><th>Model</th>{Object.values(emojis).map(emoji => <th key={emoji}>{emoji}</th>)}</tr></thead>
      <tbody>{rows.map(row => <tr key={row.model}><th>{row.model}</th><td>{row.up}</td><td>{row.down}</td><td>{row.upside_down}</td></tr>)}</tbody></table>
    {!rows.length && !error && <small>No model reactions yet.</small>}
    {error && <small role="alert" className="org-error">{error}</small>}
  </section>;
}

export function ForkOrigin({ organizationId, topicId }: { organizationId: string; topicId: string }) {
  const [origin, setOrigin] = useState<{ source_topic_id: string; through_sequence: number } | null>(null);
  useEffect(() => {
    const controller = new AbortController(); setOrigin(null);
    void request<typeof origin>(topicPath(organizationId, topicId) + "/fork-origin", { signal: controller.signal })
      .then(setOrigin).catch(() => {});
    return () => controller.abort();
  }, [organizationId, topicId]);
  return origin ? <small className="chat-fork-origin">Forked from <a href={`/organization.html?organization=${encodeURIComponent(organizationId)}&topic=${encodeURIComponent(origin.source_topic_id)}`}>source conversation</a> through message {origin.through_sequence}.</small> : null;
}
