// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import React from "react";
import type { ModelRun, ReplyAddition } from "./organization-api";
import { StepIcon, TimelineEntry, replyTimeline } from "./ReplyAdditions";
import { CardStop } from "./TopicActivity";
import "./reply-timeline.css";

const PATHS: Record<StepIcon | "added" | "waiting", React.ReactNode> = {
  think: <><circle cx="12" cy="12" r="3" /><circle cx="12" cy="12" r="8" /></>,
  search: <><circle cx="11" cy="11" r="6" /><path d="m20 20-4.5-4.5" /></>,
  read: <path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H20v15H6.5A2.5 2.5 0 0 0 4 20.5zM4 20.5A2.5 2.5 0 0 0 6.5 23H20" />,
  write: <path d="M4 20h4L19 9a2.8 2.8 0 0 0-4-4L4 16zM13.5 6.5l4 4" />,
  compute: <><rect x="6" y="6" width="12" height="12" rx="2" /><path d="M10 2v4M14 2v4M10 18v4M14 18v4M2 10h4M2 14h4M18 10h4M18 14h4" /></>,
  answer: <path d="M4 5h16v11H9l-5 4zM8 9h8M8 12h5" />,
  summary: <path d="m12 3 9 5-9 5-9-5zM3 13l9 5 9-5" />,
  delegate: <><circle cx="9" cy="8" r="3" /><path d="M3 20v-1a6 6 0 0 1 12 0v1M17 5a3 3 0 0 1 0 6M21 20v-1a5 5 0 0 0-3-4.6" /></>,
  added: <path d="M4 5h16v11H9l-5 4zM12 8v5M9.5 10.5h5" />,
  waiting: <><circle cx="12" cy="12" r="8" /><path d="M12 8v4l3 2" /></>,
};

function Icon({ name, current }: { name: StepIcon | "added" | "waiting"; current?: boolean }) {
  return <span className="org-timeline-icon" data-current={current || undefined} aria-hidden="true">
    {current ? <span className="org-timeline-spinner" />
      : <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9"
        strokeLinecap="round" strokeLinejoin="round">{PATHS[name]}</svg>}
  </span>;
}

/** How long a running reply has worked, as the card's header says it ("working · 3 min"). */
export function workingFor(run: Pick<ModelRun, "started_at" | "created_at">, now = Date.now()): string | null {
  const started = run.started_at ?? run.created_at;
  if (!started) return null;
  const seconds = Math.max(0, Math.round(now / 1000 - started));
  return seconds < 60 ? `${seconds} s` : `${Math.floor(seconds / 60)} min`;
}

/**
 * A reply's steps with each later message where the reply read it. While the reply runs, the current step spins and
 * unread additions wait for the next step, with Undo for their author.
 */
export function ReplyTimeline({ activity, additions, active, userId, names = {}, onUndo, unplaced, label }: {
  activity: NonNullable<ModelRun["activity"]>; additions: ReplyAddition[]; active: boolean; userId: string;
  names?: Record<string, string>; onUndo?: (addition: ReplyAddition) => void; unplaced?: boolean; label?: string;
}) {
  const entries: TimelineEntry[] = replyTimeline(activity, additions, active, unplaced);
  return <ol className="org-reply-timeline" aria-label={label ?? "Reply steps and additions"}>{entries.map(entry => entry.kind === "step"
    ? <li key={entry.key} className="org-timeline-step" data-current={entry.current || undefined}
      role={entry.current ? "status" : undefined} aria-label={entry.current ? "Research progress" : undefined}>
      <Icon name={entry.icon} current={entry.current} /><span>{entry.label}{entry.current ? "…" : ""}</span></li>
    : <AdditionLine key={entry.key} entry={entry} userId={userId} names={names} onUndo={onUndo} />)}</ol>;
}

/** A later message in a reply's timeline: who added it, a quote, and when the reply reads or read it; Undo while it waits. */
function AdditionLine({ entry, userId, names, onUndo }: { entry: Extract<TimelineEntry, { kind: "addition" }>; userId: string;
  names: Record<string, string>; onUndo?: (addition: ReplyAddition) => void }) {
  return <li className="org-timeline-addition" data-waiting={entry.waiting || undefined}>
    <Icon name={entry.waiting ? "waiting" : "added"} />
    <span className="org-timeline-quote"><strong>{entry.addition.added_by === userId ? "You" : names[entry.addition.added_by] ?? "Added"}</strong>
      {" · "}<q>{entry.addition.content ?? "an added message"}</q></span>
    <span className="org-timeline-chip">{entry.waiting ? `at step ${entry.nextStep}`
      : entry.addition.read_step ? `read at step ${entry.addition.read_step}` : "read"}</span>
    {entry.waiting && onUndo && entry.addition.added_by === userId
      && <button type="button" onClick={() => onUndo(entry.addition)}>Undo</button>}
  </li>;
}

/**
 * A reply at work, trimmed for the Thread view: who answers, for how long, Stop, the one step it is on, and the later
 * messages waiting for its next step. "+ Add" beside Stop puts the Thread view's box into adding mode. The finished
 * answer takes its place.
 */
export function WorkingCard({ run, onStop, stopping, additions = [], userId, names = {}, onUndo, adding, onAdd }: {
  run: ModelRun; onStop?: () => void; stopping?: boolean;
  additions?: ReplyAddition[]; userId: string; names?: Record<string, string>; onUndo?: (addition: ReplyAddition) => void;
  /** Whether the Thread view's box adds to this reply; `onAdd` turns that on or off. */
  adding?: boolean; onAdd?: () => void;
}) {
  const entries = replyTimeline(run.activity ?? [], additions, true);
  const step = entries.filter(entry => entry.kind === "step").at(-1);
  const waiting = entries.flatMap(entry => entry.kind === "addition" && entry.waiting ? [entry] : []);
  const elapsed = run.status === "running" ? workingFor(run) : null;
  return <div className="org-thread-working" data-run-id={run.id}>
    <header><strong>{run.agent ? `${run.agent.role.replaceAll("_", " ")} · ` : ""}{run.model}</strong>
      <small>{run.cancel_requested ? "stopping…" : run.status === "queued" ? "queued" : "working" + (elapsed ? ` · ${elapsed}` : "")}</small>
      {onAdd && <button type="button" className="org-card-add" aria-pressed={!!adding}
        aria-label={adding ? "Adding to this reply" : "Add to this reply"}
        title={adding ? "Your next message joins this reply; click to send a new question instead" : "Add your next message to this reply"}
        onClick={onAdd}>{adding ? "Adding…" : "+ Add"}</button>}
      {onStop && !run.cancel_requested && <CardStop label="Stop this reply" disabled={stopping} onStop={onStop} />}</header>
    <p role="status" aria-label="Reply progress">{step?.kind === "step"
      ? <><Icon name={step.icon} current={step.current} /><span>{step.label}{step.current ? "…" : ""}</span></>
      : <span>Starting…</span>}</p>
    {waiting.length > 0 && <ol className="org-reply-timeline" aria-label="Messages waiting for this reply">
      {waiting.map(entry => <AdditionLine key={entry.key} entry={entry} userId={userId} names={names} onUndo={onUndo} />)}</ol>}
  </div>;
}
