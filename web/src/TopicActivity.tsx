import React, { useEffect, useRef, useState } from "react";

export type TopicActivity = { id: string; created_at: number; sequence?: number; after_sequence?: number; body: React.ReactNode };
export function orderedActivity(records: TopicActivity[]) {
  const unique = [...new Map(records.map(record => [record.id, record])).values()];
  const messages = unique.filter(record => record.id.startsWith("message:") && record.sequence !== undefined)
    .sort((a, b) => b.sequence! - a.sequence!);
  const positions = new Map(unique.map(record => {
    // Server timestamps have second precision. Inference requested immediately
    // after a message must stay after that message, including merged alpha rows.
    const anchor = record.after_sequence === undefined ? undefined
      : messages.find(message => message.sequence! <= record.after_sequence!);
    return [record.id, { time: Math.max(record.created_at, anchor?.created_at ?? record.created_at),
      sequence: record.id.startsWith("message:") && record.sequence !== undefined ? record.sequence
        : record.after_sequence === undefined ? Infinity : record.after_sequence + .5 }];
  }));
  return unique.sort((a, b) => {
    const first = positions.get(a.id)!, second = positions.get(b.id)!;
    const time = first.time - second.time;
    const sequence = first.sequence === second.sequence ? 0 : first.sequence - second.sequence;
    const kind = a.id.split(":")[0].localeCompare(b.id.split(":")[0]);
    return time || sequence || kind || a.id.localeCompare(b.id);
  });
}
/**
 * The Stop pill in the header of a card whose work is running. Clicking it stops that work only and never opens or
 * closes the card.
 */
export function CardStop({ label, disabled, onStop }: { label: string; disabled?: boolean; onStop: () => void }) {
  return <button type="button" className="org-card-stop" aria-label={label} title={label} disabled={disabled}
    onClick={event => { event.preventDefault(); event.stopPropagation(); onStop(); }}>
    <span className="org-card-stop-square" aria-hidden="true" />Stop</button>;
}

export function ActivityCard({ title, status, label, createdAt, initiallyOpen, action, children }: {
  title: string; status: string; label?: string; createdAt: number; initiallyOpen: boolean;
  /** A control in the card header, such as Stop while its work runs. */
  action?: React.ReactNode; children: React.ReactNode;
}) {
  const [open, setOpen] = useState(initiallyOpen);
  return <details className="org-message org-run-message" data-status={status} open={open} onToggle={event => {
    if (event.target === event.currentTarget) setOpen(event.currentTarget.open);
  }}>
    <summary><strong>{title}</strong><span>{label ?? status.replaceAll("_", " ")}</span>
      <time dateTime={new Date(createdAt * 1000).toISOString()}>{new Date(createdAt * 1000).toLocaleString()}</time>{action}</summary>
    {open && children}
  </details>;
}
export function ActivityDialog({ title, onClose, children }: { title: string; onClose: () => void; children: React.ReactNode }) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => { dialog.current?.showModal(); }, []);
  return <dialog ref={dialog} className="org-dialog org-activity-dialog" aria-label={title} onCancel={onClose}>
    <header><h2>{title}</h2><button type="button" aria-label={`Close ${title.toLowerCase()}`} onClick={onClose}>×</button></header>{children}
  </dialog>;
}
/** A topic item opened from the topic's details card, in a centered dialog like a direct conversation's files. */
export function TopicItemDialog({ title, onClose, children }: { title: string; onClose: () => void; children: React.ReactNode }) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => { dialog.current?.showModal(); }, []);
  return <dialog ref={dialog} className="org-dialog org-topic-item-dialog" aria-label={title} onCancel={event => { event.preventDefault(); onClose(); }}>
    <header><h2>{title}</h2><button type="button" aria-label="Close dialog" onClick={onClose}>×</button></header>{children}
  </dialog>;
}
