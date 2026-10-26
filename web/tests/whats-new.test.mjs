// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { fileURLToPath } from "node:url";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";
import { notesBlock, parseHistory } from "../release-notes.mjs";

let server, notes, component, release;
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL("../", import.meta.url)),
    appType: "custom", logLevel: "silent", server: { middlewareMode: true } });
  notes = await server.ssrLoadModule("/src/whats-new.ts");
  component = await server.ssrLoadModule("/src/WhatsNew.tsx");
  release = await server.ssrLoadModule("/src/release.ts");
});
after(async () => { await server?.close(); });

const commit = digit => digit.repeat(40);
const log = [
  [commit("e"), "2026-10-26T16:34:05Z", "List the files a reply saved as chips with download and open (#75)"],
  [commit("d"), "2026-10-26T08:33:54-07:00", "Send earlier model replies as plain text (#74)"],
  [commit("c"), "2026-10-26T15:05:14Z", "Fix a typo without a pull request"],
  [commit("b"), "2026-10-26T07:29:28-07:00", "Show a cancelled compare as cancelled with cost unknown (#72)"],
  [commit("a"), "2026-10-26T01:48:37Z", "Message your own bot privately from People (#70)"],
].map(fields => fields.join("\u001f")).join("\n") + "\n";
const page = value => ({ getElementById: id => id === "reasoning-release-notes" && value !== undefined ? { textContent: value } : null });
const storage = (initial = {}) => {
  const values = { ...initial };
  return { values, getItem: key => values[key] ?? null, setItem: (key, value) => { values[key] = String(value); } };
};

test("build notes keep the build's revision and only squash-merged pull request titles", () => {
  const parsed = parseHistory(log);
  assert.equal(parsed.revision, commit("e"));
  assert.deepEqual(parsed.entries.map(entry => entry.number), [75, 74, 72, 70]);
  assert.equal(parsed.entries[0].title, "List the files a reply saved as chips with download and open");
  assert.equal(parseHistory(log, 2).entries.length, 2);
  assert.deepEqual(parseHistory("").entries, []);
  const block = notesBlock({ entries: [{ title: "</script><script>alert(1)</script>" }] });
  assert.equal(block.match(/<\/script>/g).length, 1, "a title cannot close the data block");
});

test("entries newer than a revision stop at that commit or its time, and an unknown revision yields none", () => {
  const embedded = notes.readNotes(page(JSON.stringify(parseHistory(log))));
  assert.equal(embedded.entries.length, 4);
  assert.deepEqual(notes.newerEntries(embedded, { revision: commit("b"), revision_date: "2026-10-26T07:29:28-07:00" })
    .map(entry => entry.number), [75, 74]);
  assert.deepEqual(notes.newerEntries(embedded, { revision: commit("c"), revision_date: "2026-10-26T15:05:14Z" })
    .map(entry => entry.number), [75, 74], "a revision without a pull request is placed by its time, across offsets");
  assert.deepEqual(notes.newerEntries(embedded, { revision: null, revision_date: null }), []);
  assert.deepEqual(notes.newerEntries(embedded, null), []);
  for (const value of [undefined, "", "{", "null", JSON.stringify({ entries: "none" })]) assert.equal(notes.readNotes(page(value)), null);
  const filtered = notes.readNotes(page(JSON.stringify({ revision: "x", entries: [{ commit: "zz", date: "now", number: 1, title: "t" }] })));
  assert.deepEqual(filtered, { revision: null, revision_date: null, entries: [] }, "malformed entries and revisions are dropped");
});

test("the first visit counts the page's own version as seen, and opening the notes records it", () => {
  const own = { revision: commit("e"), revision_date: "2026-10-26T16:34:05Z" };
  const first = storage();
  assert.deepEqual(notes.seenRevision(own, first), own);
  assert.ok(first.values["reasoning.whatsNewSeen"], "the first visit is remembered");
  const earlier = { revision: commit("b"), revision_date: "2026-10-26T07:29:28-07:00" };
  const returning = storage({ "reasoning.whatsNewSeen": JSON.stringify(earlier) });
  assert.deepEqual(notes.seenRevision(own, returning), earlier);
  notes.markSeen(own, returning);
  assert.deepEqual(JSON.parse(returning.values["reasoning.whatsNewSeen"]), own);
  const broken = { getItem: () => { throw new Error("blocked"); }, setItem: () => { throw new Error("blocked"); } };
  assert.deepEqual(notes.seenRevision(own, broken), own, "unavailable storage counts the current version as seen");
  notes.markSeen(own, broken);
});

test("the button is quiet when nothing is new, stays for a build without entries, and is absent without notes", () => {
  const own = notes.readNotes(page(JSON.stringify(parseHistory(log))));
  const watch = new release.ReleaseWatch("a".repeat(64));
  const quiet = renderToStaticMarkup(React.createElement(component.WhatsNew, { watch, own, load: async () => null }));
  assert.match(quiet, /aria-label="What&#x27;s new"/);
  assert.doesNotMatch(quiet, /data-attention|org-whats-new-dot/);
  const bare = renderToStaticMarkup(React.createElement(component.WhatsNew,
    { watch, own: { revision: null, revision_date: null, entries: [] }, load: async () => null }));
  assert.match(bare, /aria-label="What&#x27;s new"/, "a shallow or source-only build keeps the button");
  const empty = renderToStaticMarkup(React.createElement(component.WhatsNew, { watch, own: null, load: async () => null }));
  assert.equal(empty, "");
});
