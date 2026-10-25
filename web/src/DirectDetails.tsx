// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import React, { useCallback, useEffect, useRef, useState } from "react";
import { useOwnBot } from "./BotDirect";
import { DirectFile, fileSize } from "./DirectMedia";
import { DirectConversation, directPath } from "./DirectMessages";
import { chatTones, mentionInitials, toneClass } from "./mentions";
import { ApiError, Member, errorMessage, organizationPath, request } from "./organization-api";

/** A file a message of the conversation shares, with that message. */
export type SharedFile = DirectFile & { message_id: string; message_sequence: number; shared_by: string; shared_at: number };
/** A request to show one message in the conversation; the nonce repeats a jump to the same message. */
export type MessageJump = { conversation: string; id: string; sequence: number; nonce: number };

const isImage = (file: SharedFile) => file.mime.startsWith("image/");
const day = (at: number) => new Date(at * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });

function BellOff() {
  return <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M9.5 4.4A6 6 0 0 1 18 8c0 3.4.7 5.6 1.5 7M6.2 6.2A6 6 0 0 0 6 8c0 7-3 9-3 9h14M10.3 21a1.9 1.9 0 0 0 3.4 0M3 3l18 18" /></svg>;
}

/** Every file of the conversation, filtered by kind and name; each row returns to the message that shared it. */
function FilesDialog({ name, files, userId, onShow, onClose }: {
  name: string; files: SharedFile[]; userId: string; onShow: (file: SharedFile) => void; onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null), [kind, setKind] = useState<"all" | "pictures" | "pdfs">("all"), [query, setQuery] = useState("");
  useEffect(() => { dialog.current?.showModal(); }, []);
  const pictures = files.filter(isImage), pdfs = files.filter(file => !isImage(file));
  const shown = (kind === "pictures" ? pictures : kind === "pdfs" ? pdfs : files)
    .filter(file => file.name.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));
  return <dialog ref={dialog} className="org-dialog org-direct-files" aria-label={`Files with ${name}`} onCancel={event => { event.preventDefault(); onClose(); }}>
    <header><h2>Files with {name}</h2><button type="button" aria-label="Close dialog" onClick={onClose}>×</button></header>
    <nav className="org-direct-files-kinds" aria-label="Kind of file">
      {([["all", "All", files.length], ["pictures", "Pictures", pictures.length], ["pdfs", "PDFs", pdfs.length]] as const).map(([value, label, count]) =>
        <button key={value} type="button" aria-pressed={kind === value} onClick={() => setKind(value)}>{label} <span>{count}</span></button>)}
    </nav>
    <input type="search" aria-label="Search file names" placeholder="Search file names…" value={query} onChange={event => setQuery(event.target.value)} />
    {shown.length ? <ul className="org-direct-files-list">{shown.map(file => <li key={file.id}>
      {isImage(file) ? <img src={file.url} alt="" loading="lazy" referrerPolicy="no-referrer" /> : <span className="direct-file-icon" aria-hidden="true">PDF</span>}
      <span className="org-direct-files-text"><a href={file.url} target="_blank" rel="noreferrer">{file.name}</a>
        <small>{file.shared_by === userId ? "You" : name} · {day(file.shared_at)} · {fileSize(file.size_bytes)}</small></span>
      <button type="button" onClick={() => onShow(file)}>Show in chat</button>
    </li>)}</ul> : <p className="org-help">{files.length ? "No files match." : "No files have been shared in this conversation yet."}</p>}
  </dialog>;
}

/**
 * The top bar's title for a direct conversation. Clicking it opens the conversation's details, as in Signal: the person,
 * who can read the conversation, Mute, the shared files and Block. For your own bot: its runner, local tools, models,
 * the shared files and its settings.
 */
export function DirectTitle({ organizationId, conversation, members, userId, onChanged, onShowMessage, onAuthLost, onBotSettings }: {
  organizationId: string; conversation: DirectConversation; members: Member[]; userId: string;
  onChanged: () => void; onShowMessage: (jump: Omit<MessageJump, "nonce">) => void; onAuthLost: () => void;
  onBotSettings?: () => void;
}) {
  const isBot = conversation.other_user.kind === "bot";
  const bot = useOwnBot(organizationId, isBot ? conversation.other_user.id : null, onAuthLost);
  const name = conversation.other_user.name, root = directPath(organizationId, conversation.id);
  const blockPath = organizationPath(organizationId) + "/direct-blocks/" + encodeURIComponent(conversation.other_user.id);
  const [open, setOpen] = useState(false), [listing, setListing] = useState(false), [confirmBlock, setConfirmBlock] = useState(false);
  const [files, setFiles] = useState<SharedFile[] | null>(null), [busy, setBusy] = useState(false), [error, setError] = useState("");
  const card = useRef<HTMLDivElement>(null), button = useRef<HTMLButtonElement>(null);
  const fail = useCallback((cause: unknown) => { if (cause instanceof ApiError && cause.status === 401) onAuthLost(); else setError(errorMessage(cause)); }, [onAuthLost]);
  const close = useCallback((focus = false) => { setOpen(false); setListing(false); setConfirmBlock(false); setError(""); if (focus) button.current?.focus(); }, []);
  useEffect(() => {
    if (!open || listing) return;
    const outside = (event: PointerEvent) => { if (!card.current?.contains(event.target as Node)) close(); };
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") close(true); };
    document.addEventListener("pointerdown", outside); document.addEventListener("keydown", escape);
    return () => { document.removeEventListener("pointerdown", outside); document.removeEventListener("keydown", escape); };
  }, [open, listing, close]);
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    void request<SharedFile[]>(root + "/media", { signal: controller.signal }).then(setFiles).catch(cause => { if (cause?.name !== "AbortError") fail(cause); });
    return () => controller.abort();
  }, [open, root, fail]);
  const act = (work: () => Promise<unknown>) => {
    setBusy(true); setError("");
    void work().then(() => { setConfirmBlock(false); onChanged(); }).catch(fail).finally(() => setBusy(false));
  };
  const member = members.find(item => item.id === conversation.other_user.id);
  const role = isBot ? `Your bot · ${bot?.engine === "claude" ? "Claude Code" : bot?.engine === "codex" ? "Codex" : "Bot"}${conversation.other_member ? "" : " · no longer available"}`
    : !conversation.other_member ? "No longer a member" : member ? member.role[0].toUpperCase() + member.role.slice(1) : "Member";
  const pictures = (files ?? []).filter(isImage).slice(0, 4);
  return <div className="org-direct-title" ref={card}>
    <h1 className="org-workspace-title"><button type="button" ref={button} className="org-direct-title-button" aria-haspopup="dialog" aria-expanded={open}
      onClick={() => open ? close() : setOpen(true)}>Direct messages <span className="org-direct-title-dot">·</span> {name}
      {isBot && <small className="org-direct-title-kind">your bot{bot && !bot.online ? " · offline" : ""}</small>}
      {conversation.muted && <span className="org-direct-muted" title="Muted" aria-label="muted"><BellOff /></span>}
      <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6" /></svg>
    </button></h1>
    {open && <div className="org-direct-card" role="dialog" aria-label="Conversation details">
      <header><span className={"org-mention-avatar " + toneClass(chatTones([userId, conversation.other_user.id]), conversation.other_user.id)} aria-hidden="true">{mentionInitials(name)}</span>
        <span><strong>{name}</strong><small>{role}</small></span></header>
      {isBot ? <>
        <p className="org-direct-card-note">Only you can read this conversation. {name} answers through its runner on your computer.</p>
        <div className="org-direct-card-row" role="group" aria-label="Runner"><i className="org-direct-card-mark" aria-hidden="true">●</i><span>Runner</span>
          <small className={bot?.online ? "org-capability-on" : undefined}>{bot ? (bot.online ? "online" : "offline") : "…"}</small></div>
        <div className="org-direct-card-row" role="group" aria-label="Local tools"><i className="org-direct-card-mark" aria-hidden="true">⚙</i><span>Local tools</span>
          <small>{bot?.local_tools ? "allowed" : "off"}</small></div>
        <div className="org-direct-card-row" role="group" aria-label="Models"><i className="org-direct-card-mark" aria-hidden="true">◇</i><span>Models</span>
          <small>{bot?.models?.join(", ") || "none yet"}</small></div>
      </> : <><p className="org-direct-card-note">Only you and {name} can read this conversation. Bots and models never take part.</p>
      <button type="button" role="switch" aria-checked={conversation.muted} className="org-direct-card-row" disabled={busy}
        title={conversation.muted ? "Show unread counts again" : "Hide unread counts for this conversation"}
        onClick={() => act(() => request(root + "/mute", { method: "PUT", body: { muted: !conversation.muted } }))}>
        <BellOff /><span>Mute</span><span className="org-direct-switch" aria-hidden="true" /></button></>}
      <button type="button" className="org-direct-card-row" aria-label={files ? `Files, ${files.length}` : "Files"} onClick={() => setListing(true)}>
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M21 12.5 12.6 20.9a5.2 5.2 0 0 1-7.4-7.4l8.5-8.5a3.5 3.5 0 0 1 4.9 4.9l-8.5 8.5a1.7 1.7 0 0 1-2.4-2.4l7.8-7.8" /></svg>
        <span>Files</span><small>{files ? files.length : "…"} ›</small></button>
      {pictures.length > 0 && <div className="org-direct-card-thumbs">{pictures.map(file =>
        <img key={file.id} src={file.url} alt="" loading="lazy" referrerPolicy="no-referrer" />)}</div>}
      {isBot ? onBotSettings && <div className="org-direct-card-block"><button type="button" className="org-direct-card-row"
        onClick={() => { close(); onBotSettings(); }}>Bot settings</button></div>
        : <div className="org-direct-card-block">
        {conversation.blocked ? <button type="button" className="org-direct-card-row" disabled={busy} onClick={() => act(() => request(blockPath, { method: "DELETE" }))}>Unblock {name}</button>
          : !confirmBlock ? <button type="button" className="org-direct-card-row org-danger" disabled={busy || !conversation.other_member} onClick={() => setConfirmBlock(true)}>Block {name}</button>
            : <div className="org-direct-confirm" role="group" aria-label={`Block ${name}?`}>Block {name}? They will not be able to message you.
              <span><button type="button" className="org-danger" disabled={busy} onClick={() => act(() => request(blockPath, { method: "PUT", body: {} }))}>Block</button>
                <button type="button" disabled={busy} onClick={() => setConfirmBlock(false)}>Cancel</button></span></div>}
      </div>}
      {error && <p role="alert" className="org-error">{error}</p>}
      {listing && <FilesDialog name={name} files={files ?? []} userId={userId} onClose={() => setListing(false)} onShow={file => {
        close(); onShowMessage({ conversation: conversation.id, id: file.message_id, sequence: file.message_sequence });
      }} />}
    </div>}
  </div>;
}
