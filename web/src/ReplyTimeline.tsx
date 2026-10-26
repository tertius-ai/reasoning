// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import React from "react";
import type { ModelRun, ReplyAddition } from "./organization-api";
import { StepIcon, TimelineEntry, replyTimeline } from "./ReplyAdditions";
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
    : <li key={entry.key} className="org-timeline-addition" data-waiting={entry.waiting || undefined}>
      <Icon name={entry.waiting ? "waiting" : "added"} />
      <span className="org-timeline-quote"><strong>{entry.addition.added_by === userId ? "You" : names[entry.addition.added_by] ?? "Added"}</strong>
        {" · "}<q>{entry.addition.content ?? "an added message"}</q></span>
      <span className="org-timeline-chip">{entry.waiting ? `at step ${entry.nextStep}`
        : entry.addition.read_step ? `read at step ${entry.addition.read_step}` : "read"}</span>
      {entry.waiting && onUndo && entry.addition.added_by === userId
        && <button type="button" onClick={() => onUndo(entry.addition)}>Undo</button>}
    </li>)}</ol>;
}
