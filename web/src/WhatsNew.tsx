// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import React, { useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { ReleaseWatch, releaseWatch } from "./release";
import { ReleaseEntry, ReleaseNotes, Revision, latestNotes, markSeen, newerEntries, readNotes, seenRevision } from "./whats-new";
import "./whats-new.css";

function SparkleIcon() {
  return <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" aria-hidden="true">
    <path d="M10 3.5 11.6 8a3 3 0 0 0 1.9 1.9L18 11.5l-4.5 1.6a3 3 0 0 0-1.9 1.9L10 19.5 8.4 15a3 3 0 0 0-1.9-1.9L2 11.5 6.5 10A3 3 0 0 0 8.4 8Z" />
    <path d="M18.5 2.5v4M16.5 4.5h4" strokeLinecap="round" /></svg>;
}

const day = (date: string) => new Date(date).toLocaleDateString(undefined, { month: "short", day: "numeric" });
const changes = (count: number) => `${count} ${count === 1 ? "change" : "changes"}`;

/**
 * The top bar's What's new button. It lists the merged changes of a newer release once the server reports one and
 * offers a reload; it never reloads by itself, because unsent drafts live only in this page. After a reload it keeps
 * a dot until the reader has opened the notes for the version they now run.
 */
export function WhatsNew({ watch = releaseWatch, own = readNotes(), load = latestNotes }: {
  watch?: ReleaseWatch; own?: ReleaseNotes | null; load?: () => Promise<ReleaseNotes | null>;
}) {
  const available = useSyncExternalStore(watch.subscribe, () => watch.available, () => null);
  const [latest, setLatest] = useState<{ release: string; notes: ReleaseNotes | null } | null>(null);
  const [seen, setSeen] = useState<Revision | null>(() => own ? seenRevision(own) : null);
  const [open, setOpen] = useState<{ heading: string; entries: ReleaseEntry[] } | null>(null);
  const root = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (!available || latest?.release === available) return;
    let live = true;
    void load().catch(() => null).then(notes => { if (live) setLatest({ release: available, notes }); });
    return () => { live = false; };
  }, [available, latest?.release, load]);
  useEffect(() => {
    if (!open) return;
    const outside = (event: Event) => { if (!root.current?.contains(event.target as Node)) setOpen(null); };
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") setOpen(null); };
    document.addEventListener("pointerdown", outside); document.addEventListener("keydown", escape);
    return () => { document.removeEventListener("pointerdown", outside); document.removeEventListener("keydown", escape); };
  }, [open]);
  const pending = useMemo(() => available && own && latest?.release === available && latest.notes
    ? newerEntries(latest.notes, own) : [], [available, own, latest]);
  const unseen = useMemo(() => own && seen ? newerEntries(own, seen) : [], [own, seen]);
  if (!available && !own) return null;
  const label = available ? "What's new: a new version is available"
    : unseen.length ? `What's new: ${changes(unseen.length)} since you last looked` : "What's new";
  const toggle = () => {
    if (open) { setOpen(null); return; }
    setOpen(available ? { heading: "New version available", entries: pending }
      : unseen.length ? { heading: "What's new", entries: unseen } : { heading: "Recent changes", entries: own!.entries.slice(0, 8) });
    if (!available && own) { markSeen(own); setSeen(own); }
  };
  return <span className="org-whats-new" ref={root}>
    <button type="button" className="org-icon-button" data-attention={available || unseen.length ? "true" : undefined}
      aria-label={label} title={label} aria-expanded={!!open} aria-haspopup="dialog" onClick={toggle}>
      <SparkleIcon />{(available || unseen.length > 0) && <span className="org-whats-new-dot" aria-hidden="true" />}
    </button>
    {open && <div className="org-whats-new-panel" role="dialog" aria-label="What's new">
      <header><strong>{open.heading}</strong>{open.entries.length > 0 && <small>{day(open.entries[0].date)} · {changes(open.entries.length)}</small>}</header>
      {open.entries.length ? <ul>{open.entries.map(item => <li key={item.commit}>{item.title}<small>#{item.number}</small></li>)}</ul>
        : <p>{available ? "A newer version of Reasoning is running on the server." : "No changes recorded for this version."}</p>}
      <footer>{available ? <><button type="button" className="org-primary" onClick={() => window.location.reload()}>Reload to update</button>
        <small>Reloading discards unsent drafts.</small></>
        : <small>{own?.revision ? `Version ${own.revision.slice(0, 7)}` : "Development build"}</small>}</footer>
    </div>}
  </span>;
}
