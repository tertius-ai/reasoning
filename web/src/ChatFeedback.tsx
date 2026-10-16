import { ReactNode, useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ApiError, Message, errorMessage, organizationPath, request, topicPath } from "./organization-api";
import { encodeMessageEdit, MessageEditDraft, readableMessageEdit, updateMessageEdit } from "./messageEditorMentions";
import "./chat-feedback.css";

const emojis = { up: "👍", down: "👎", upside_down: "🙃" } as const;
const labels = { up: "Thumbs up", down: "Thumbs down", upside_down: "Upside-down smile" };
type Emoji = keyof typeof emojis;
export type Feedback = { counts: Record<Emoji, number>; mine: Emoji[]; reactors?: Record<Emoji, { id: string; name: string }[]> };

export function CopyOutputButton({ text }: { text: string }) {
  const [error, setError] = useState("");
  return <>
    <button type="button" className="chat-action-launcher chat-copy-launcher" aria-label="Copy message" title="Copy message"
      onClick={event => {
        event.preventDefault(); event.stopPropagation(); setError("");
        void (async () => {
          try { await navigator.clipboard.writeText(text); }
          catch { setError("Could not copy. Select the message text and copy it manually."); }
        })();
      }}><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true"><rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3"/></svg></button>
    {error && <small role="alert" className="org-error">{error}</small>}
  </>;
}

export function OutputActions({ text, ...props }: Parameters<typeof ReactionBar>[0] & { text: string }) {
  return <div className="chat-message-actions chat-output-actions"><ReactionBar {...props} /><CopyOutputButton text={text} /></div>;
}

export function reactorInitials(name: string): string {
  const words = name.trim().split(/\s+/).filter(Boolean);
  return (words.length > 1 ? [words[0], words.at(-1)!] : words).map(word => Array.from(word)[0]).join("").toLocaleUpperCase();
}

function ActionMenu({ label, className, icon, children }: {
  label: string; className: string; icon: ReactNode; children: (close: () => void) => ReactNode;
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
    if (!open) return;
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
    {position && createPortal(<div ref={menu} className="chat-action-menu" role="menu" aria-label={label}
      style={position} onKeyDown={event => {
        if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); close(); }
        if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
          event.preventDefault();
          const buttons = Array.from(menu.current!.querySelectorAll<HTMLButtonElement>("button:not(:disabled)"));
          const current = buttons.indexOf(document.activeElement as HTMLButtonElement);
          const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1
            : (current + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length;
          buttons[next]?.focus({ preventScroll: true });
        }
      }}>{children(close)}</div>, document.body)}
  </>;
}

export function ReactionChips({ feedback, canWrite, busy, onToggle }: {
  feedback: Feedback; canWrite: boolean; busy: boolean; onToggle: (emoji: Emoji) => void;
}) {
  return <>{(Object.keys(emojis) as Emoji[]).filter(emoji => feedback.counts[emoji] > 0).map(emoji => {
    const people = feedback.reactors?.[emoji] ?? [], extra = Math.max(0, feedback.counts[emoji] - people.length);
    const names = people.map(person => person.name).join(", ") + (extra ? ` and ${extra} others` : "");
    return <button key={emoji} type="button" className="chat-reaction-chip" aria-label={`${labels[emoji]}: ${names || feedback.counts[emoji]}`}
      title={names} data-reactors={names} aria-pressed={feedback.mine.includes(emoji)} aria-disabled={!canWrite || busy}
      disabled={busy} onClick={() => { if (canWrite && !busy) onToggle(emoji); }}>
      <span>{emojis[emoji]}</span>{people.map(person => <span className="chat-reactor-initials" key={person.id} title={person.name}>{reactorInitials(person.name)}</span>)}
      {extra > 0 && <small>+{extra}</small>}
    </button>;
  })}</>;
}

export function ReactionBar({ organizationId, topicId, targetKind, targetId, nodeId, canWrite = true, onAuthLost, batched = false, preloaded }: {
  organizationId: string; topicId: string; targetKind: "message" | "run" | "bot" | "node"; targetId: string; nodeId?: string;
  canWrite?: boolean; onAuthLost?: () => void;
  /** The conversation reads this target's reactions in one request for its whole message page. */
  batched?: boolean; preloaded?: Feedback;
}) {
  const [feedback, setFeedback] = useState<Feedback>(preloaded ?? { counts: { up: 0, down: 0, upside_down: 0 }, mine: [] });
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const path = topicPath(organizationId, topicId) + "/reactions/" + targetKind + "/" + encodeURIComponent(targetId)
    + (nodeId ? "?node_id=" + encodeURIComponent(nodeId) : "");
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
    {canWrite && <ActionMenu label="Add reaction" className="chat-reaction-launcher" icon={<span aria-hidden="true">:)</span>}>
      {close => (Object.keys(emojis) as Emoji[]).map(emoji => <button key={emoji} type="button" role="menuitemcheckbox"
        aria-label={labels[emoji]} aria-checked={feedback.mine.includes(emoji)} disabled={busy}
        onClick={() => { close(); toggle(emoji); }}>{emojis[emoji]} <span>{labels[emoji]}</span></button>)}
    </ActionMenu>}
    {error && <small role="alert" className="org-error">{error}</small>}
  </span>;
}

export function MessageActions({ organizationId, topicId, messageId, kind, canWrite, onForked, onAuthLost, message, canEdit, onEdited, onReplyAgain, people = [], reactions, batchedReactions = false }: {
  organizationId: string; topicId: string; messageId: string; kind: string; canWrite: boolean;
  reactions?: Feedback; batchedReactions?: boolean;
  onForked?: (topicId: string) => void; onAuthLost?: () => void;
  message?: Message; canEdit?: boolean; onEdited?: (message: Message) => void;
  onReplyAgain?: () => Promise<void>;
  people?: { id: string; name: string }[];
}) {
  const [open, setOpen] = useState(false), [name, setName] = useState(""), [busy, setBusy] = useState(false), [error, setError] = useState("");
  const [clientId, setClientId] = useState("");
  const [editing, setEditing] = useState(false), [editContent, setEditContent] = useState<MessageEditDraft>({ text: "", bindings: [] });
  const [editRevision, setEditRevision] = useState(0), [showHistory, setShowHistory] = useState(false);
  const [history, setHistory] = useState<{ revision: number; content: string; created_at: number }[]>([]);
  const path = topicPath(organizationId, topicId) + "/messages/" + encodeURIComponent(messageId);
  const fail = (cause: unknown) => {
    if (cause instanceof ApiError && cause.status === 401) onAuthLost?.(); else setError(errorMessage(cause));
  };
  const hasMenu = (kind === "beta" && canWrite) || canEdit || Boolean(message?.revision);
  return <div className={`chat-message-actions${hasMenu ? " chat-has-fork" : ""}${message ? " chat-has-copy" : ""}`}>
    <ReactionBar {...{ organizationId, topicId, canWrite, onAuthLost }} targetKind="message" targetId={messageId}
      batched={batchedReactions} preloaded={reactions} />
    {hasMenu && <ActionMenu label="Message actions" className="chat-fork-launcher" icon={<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true"><circle cx="7" cy="5" r="2"/><circle cx="17" cy="5" r="2"/><circle cx="7" cy="19" r="2"/><path d="M7 7v10m10-10v3a4 4 0 0 1-4 4H7"/></svg>}>
      {close => <>{kind === "beta" && canWrite && <button type="button" role="menuitem" onClick={() => {
        setOpen(true); if (!clientId) setClientId(crypto.randomUUID()); close();
      }}>Fork here</button>}
      {canEdit && message && <button type="button" role="menuitem" onClick={() => {
        setEditContent(readableMessageEdit(message.content, people)); setEditRevision(message.revision ?? 0); setEditing(true); setError(""); close();
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
    {message && <CopyOutputButton text={readableMessageEdit(message.content, people).text} />}
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
      {history.map(version => <div key={version.revision}><small>{version.revision ? `Revision ${version.revision}` : "Original"} · {new Date(version.created_at * 1000).toLocaleString()}</small><pre>{readableMessageEdit(version.content, people).text}</pre></div>)}
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
