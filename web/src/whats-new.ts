// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0

/** One merged pull request in a build's first-parent history. */
export type ReleaseEntry = { commit: string; date: string; number: number; title: string };
/** The notes a build embeds in organization.html (see web/release-notes.mjs). */
export type ReleaseNotes = { revision: string | null; revision_date: string | null; entries: ReleaseEntry[] };
/** A point in history: a build's revision and its commit time. */
export type Revision = Pick<ReleaseNotes, "revision" | "revision_date">;

const NOTES_ID = "reasoning-release-notes";
const SEEN = "reasoning.whatsNewSeen";
const COMMIT = /^[a-f0-9]{40}([a-f0-9]{24})?$/;

const entry = (value: unknown): ReleaseEntry | null => {
  const item = value as Partial<ReleaseEntry> | null;
  return item && typeof item.commit === "string" && COMMIT.test(item.commit) && typeof item.date === "string"
    && !Number.isNaN(Date.parse(item.date)) && Number.isSafeInteger(item.number) && typeof item.title === "string"
    ? { commit: item.commit, date: item.date, number: item.number!, title: item.title.slice(0, 300) } : null;
};

/** The notes embedded in a page, or null when the page has none or they are malformed. */
export function readNotes(page: Pick<Document, "getElementById"> | undefined = globalThis.document): ReleaseNotes | null {
  try {
    const value = JSON.parse(page?.getElementById(NOTES_ID)?.textContent ?? "null");
    if (!value || !Array.isArray(value.entries)) return null;
    const revision = typeof value.revision === "string" && COMMIT.test(value.revision) ? value.revision : null;
    const date = typeof value.revision_date === "string" && !Number.isNaN(Date.parse(value.revision_date)) ? value.revision_date : null;
    return { revision, revision_date: revision && date, entries: value.entries.map(entry).filter(Boolean) as ReleaseEntry[] };
  } catch {
    return null;
  }
}

/**
 * The entries of `notes` that are newer than `since`: up to that commit, or to the first entry no later than its
 * time. A revision these notes cannot place yields nothing, so an unknown history is never presented as new.
 */
export function newerEntries(notes: ReleaseNotes, since: Revision | null): ReleaseEntry[] {
  const time = since?.revision_date ? Date.parse(since.revision_date) : NaN;
  if (!since?.revision || Number.isNaN(time)) return [];
  const result: ReleaseEntry[] = [];
  for (const item of notes.entries) {
    if (item.commit === since.revision || Date.parse(item.date) <= time) break;
    result.push(item);
  }
  return result;
}

/** The notes of the release the server runs now, read from its current page. */
export async function latestNotes(): Promise<ReleaseNotes | null> {
  const response = await fetch("/organization.html", { cache: "no-store", credentials: "same-origin" });
  if (!response.ok) return null;
  return readNotes(new DOMParser().parseFromString(await response.text(), "text/html"));
}

/** The revision whose notes this browser last opened; the first visit records the page's own revision instead. */
export function seenRevision(own: Revision, storage: Pick<Storage, "getItem" | "setItem"> | undefined = globalThis.localStorage): Revision {
  try {
    const stored = JSON.parse(storage?.getItem(SEEN) ?? "null");
    if (stored && typeof stored.revision === "string" && typeof stored.revision_date === "string") return stored;
    if (own.revision) storage?.setItem(SEEN, JSON.stringify(own));
  } catch {
    // Storage can be unavailable; the page's own revision then counts as seen.
  }
  return own;
}

export function markSeen(own: Revision, storage: Pick<Storage, "setItem"> | undefined = globalThis.localStorage): void {
  try { if (own.revision) storage?.setItem(SEEN, JSON.stringify({ revision: own.revision, revision_date: own.revision_date })); } catch {
    // A browser without storage shows the same notes again after the next reload.
  }
}
