// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
/** A model reply folded to its one-sentence summary, in a topic, the Thread view and a side chat. */
import React, { useCallback, useState } from "react";
import { bodySize, streamParts, type ReplyParts } from "./replySummary";
import "./folded-reply.css";

/** How a folded reply opens: the state the footer's "Show full answer" shares with the summary line. */
export type Fold = { open: boolean; size: string; onToggle: () => void };

/** Which replies are open, by id; every reply starts folded. */
export function useOpenReplies(): [(id: string) => boolean, (id: string) => void] {
  const [open, setOpen] = useState<ReadonlySet<string>>(() => new Set());
  const toggle = useCallback((id: string) => setOpen(current => {
    const next = new Set(current);
    if (!next.delete(id)) next.add(id);
    return next;
  }), []);
  return [useCallback((id: string) => open.has(id), [open]), toggle];
}

/** The footer control for a reply's parts, or nothing when the reply shows whole. */
export function foldFor(parts: ReplyParts | null, open: boolean, onToggle: () => void): Fold | undefined {
  return parts?.fold ? { open, size: bodySize(parts.body), onToggle } : undefined;
}

/** A click that selects text or follows a link inside the summary does not toggle it. */
const toggles = (event: React.MouseEvent) => !(event.target as Element).closest("a, button")
  && !(globalThis.getSelection?.()?.toString() ?? "");

/**
 * A reply's text: its summary line, then the answer once opened. A reply that shows whole keeps its summary as the first
 * line. A fallback summary is the answer's first sentence, so the opened reply shows the answer alone.
 */
export function ReplyBody({ parts, fold, render }: { parts: ReplyParts; fold?: Fold; render: (markdown: string) => React.ReactNode }) {
  if (!parts.summary) return <>{render(parts.body)}</>;
  if (!fold) return <><div className="org-reply-lead">{render(parts.summary)}</div>{render(parts.body)}</>;
  const lead = <div className="org-reply-lead org-reply-fold-lead" role="button" tabIndex={0} aria-expanded={fold.open}
    title={fold.open ? "Hide the full answer" : "Show the full answer"}
    onClick={event => { if (toggles(event)) fold.onToggle(); }}
    onKeyDown={event => {
      if ((event.key === "Enter" || event.key === " ") && event.target === event.currentTarget) { event.preventDefault(); fold.onToggle(); }
    }}>{render(parts.summary)}{parts.fallback && <span className="org-reply-first" title="The model wrote no summary line">first sentence</span>}</div>;
  if (!fold.open) return lead;
  return parts.fallback ? <>{render(parts.body)}</> : <>{lead}{render(parts.body)}</>;
}

/** "Show full answer · 190 words": the control in the row under a folded reply. */
export function FoldToggle({ fold }: { fold: Fold }) {
  return <>
    <button type="button" className="org-reply-fold" aria-expanded={fold.open} onClick={fold.onToggle}>
      {fold.open ? "Hide full answer" : "Show full answer"}</button>
    {!fold.open && fold.size && <span className="org-reply-size">{fold.size}</span>}
  </>;
}

/**
 * A streaming reply's text in its live card. Once the first line is a complete summary, the summary shows and the rest
 * streams folded behind "Show the answer as it streams"; text without a summary line streams as before.
 */
export function StreamedAnswer({ text, model, done, render }: {
  text: string; model: string; render?: (markdown: string) => React.ReactNode;
  /** The run has ended, so its last line is complete. */
  done?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const parts = streamParts(done ? text + "\n" : text);
  const stream = (body: string) => <pre aria-label={"Streaming response from " + model}>{body}</pre>;
  if (!render || (!parts.pending && !parts.summary)) return stream(text);
  if (parts.pending) return <p className="org-reply-pending" role="status">The summary arrives with the first line of the answer.</p>;
  const words = parts.body.match(/\S+/g)?.length ?? 0;
  return <>
    <div className="org-reply-lead">{render(parts.summary!)}</div>
    {parts.body && <div className="org-usage-row org-stream-fold">
      <button type="button" className="org-reply-fold" aria-expanded={open} onClick={() => setOpen(!open)}>
        {open ? "Hide the streaming answer" : "Show the answer as it streams"}</button>
      <span className="org-reply-size">{words} {words === 1 ? "word" : "words"} so far</span></div>}
    {open && parts.body && stream(parts.body)}
  </>;
}
