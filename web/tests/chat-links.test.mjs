// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

let server, references, mentions, edits, app, feedback;
before(async () => {
  server = await createServer({ appType: "custom", logLevel: "silent", server: { middlewareMode: true } });
  references = await server.ssrLoadModule("/src/references.tsx");
  mentions = await server.ssrLoadModule("/src/composerMentions.ts");
  edits = await server.ssrLoadModule("/src/messageEditorMentions.ts");
  app = await server.ssrLoadModule("/src/OrganizationApp.tsx");
  feedback = await server.ssrLoadModule("/src/ChatFeedback.tsx");
});
after(async () => { await server?.close(); });

const topicId = "a".repeat(32), projectId = "b".repeat(32), hiddenId = "c".repeat(32);
const hierarchy = {
  projects: [{ id: projectId, name: "Algebra" }, { id: "d".repeat(32), name: "Research notes" }],
  channels: [{ id: "channel", project_id: projectId, name: "results" }],
  topics: [{ id: topicId, channel_id: "channel", kind: "beta", name: "Results" }, { id: "e".repeat(32), channel_id: "channel", kind: "alpha", name: "Planning" }],
  topic_state: [],
};

test("the # fragment opens only at a word boundary and before whitespace, like @ mentions", () => {
  assert.deepEqual(references.referenceQuery("See #Res", 8), { start: 4, end: 8, query: "Res" });
  assert.deepEqual(references.referenceQuery("#", 1), { start: 0, end: 1, query: "" });
  assert.deepEqual(references.referenceQuery("(#Al", 4), { start: 1, end: 4, query: "Al" });
  for (const text of ["issue#12", "## Heading", "# Heading", `#[topic:${topicId}]`, "#Results "]) assert.equal(references.referenceQuery(text, text.length), null, text);
  assert.equal(references.referenceQuery("#Res", 99), null);
});

test("completion offers the writer's topics and projects, name prefixes first", () => {
  const candidates = references.referenceCandidates(hierarchy);
  assert.deepEqual(candidates.map(item => [item.kind, item.label, item.detail]), [
    ["topic", "β-Results", "Algebra / #results"], ["topic", "α-Planning", "Algebra / #results"],
    ["project", "Algebra", "Project"], ["project", "Research notes", "Project"]]);
  assert.deepEqual(references.matchReferences(candidates, "res").map(item => item.label), ["β-Results", "Research notes"]);
  assert.deepEqual(references.matchReferences(candidates, "notes").map(item => item.label), ["Research notes"]);
  assert.equal(references.matchReferences(candidates, "", 3).length, 3);
});

test("an inserted reference is stored by identifier and survives edits around it", () => {
  const label = "#β-Results", text = `${label} shows it`;
  const binding = { start: 0, end: label.length, id: topicId, label, wire: references.referenceToken("topic", topicId) };
  assert.equal(mentions.encodeMentions(text, [binding]), `#[topic:${topicId}] shows it`);
  const edited = "As " + text;
  assert.equal(mentions.encodeMentions(edited, mentions.editMentions(text, edited, [binding])), `As #[topic:${topicId}] shows it`);
  const broken = "#β-Resul shows it";
  assert.equal(mentions.encodeMentions(broken, mentions.editMentions(text, broken, [binding])), broken, "a changed label stays plain text");
  assert.equal(mentions.encodeMentions(label, [{ ...binding, wire: "#[topic:not-an-id]" }]), label, "only identifier tokens are encoded");
  const mention = { start: 0, end: 6, id: "f".repeat(32), label: "@Alice" };
  assert.equal(mentions.encodeMentions(`@Alice ${label}`, [mention, { ...binding, start: 7, end: 7 + label.length }]),
    `@[${"f".repeat(32)}] #[topic:${topicId}]`);
});

test("editing and copying show current reference names and keep the stored token", () => {
  const content = `See #[topic:${topicId}] and #[project:${hiddenId}] with @[${"f".repeat(32)}]`;
  const names = (kind, id) => id === topicId ? "#β-Results" : "#unavailable";
  const draft = edits.readableMessageEdit(content, [{ id: "f".repeat(32), name: "Alice" }], names);
  assert.equal(draft.text, "See #β-Results and #unavailable with @Alice");
  assert.equal(edits.encodeMessageEdit(edits.updateMessageEdit(draft, draft.text + "!")), content + "!");
  assert.equal(edits.readableMessageEdit(`#[topic:${topicId}]`, []).text, "#reference");
});

function resolver(entries) {
  const value = new references.ReferenceResolver("org", async () => { throw new Error("not requested in static rendering"); });
  for (const [key, entry] of Object.entries(entries)) value.entries.set(key, { value: entry, stale: false, loading: false });
  return value;
}
const render = (markdown, value) => renderToStaticMarkup(React.createElement(references.ReferenceContext.Provider, { value },
  React.createElement(app.MessageMarkdown, { markdown })));

test("references render the target's current name for readers with access and a placeholder otherwise", () => {
  const value = resolver({
    ["topic:" + topicId]: { id: topicId, name: "Results", kind: "beta", channel_id: "channel", channel_name: "results", project_id: projectId, project_name: "Algebra" },
    ["project:" + projectId]: { id: projectId, name: "Algebra" },
    ["topic:" + hiddenId]: null,
  });
  const html = render(`Compare #[topic:${topicId}], #[project:${projectId}] and #[topic:${hiddenId}]. Code: \`#[topic:${topicId}]\``, value);
  assert.match(html, new RegExp(`<a class="org-reference" href="/organization.html\\?organization=org&amp;topic=${topicId}" title="Algebra / #results / β-Results">β-Results</a>`));
  assert.match(html, new RegExp(`href="/organization.html\\?organization=org&amp;project=${projectId}" title="Project Algebra">Algebra</a>`));
  assert.match(html, /<span class="org-reference org-reference-unavailable" title="This reference is unavailable">unavailable<\/span>/);
  assert.match(html, new RegExp(`<code>#\\[topic:${topicId}\\]</code>`), "code keeps the literal token");
  assert.doesNotMatch(html, new RegExp(hiddenId), "an unavailable target exposes neither name nor link");
  assert.match(render(`#[topic:${"9".repeat(32)}]`, value), /aria-label="Loading reference"/);
  assert.match(render(`[link #[topic:${topicId}]](https://example.org)`, value), /target="_blank"[^>]*>link #\[topic:a+\]<\/a>/,
    "references inside links stay text rather than nesting links");
  assert.match(render(`#[topic:${topicId}]`, null), /unavailable/, "without a resolver nothing is revealed");
});

test("application links navigate in place while other links open a new tab", () => {
  globalThis.window = { location: new URL("https://chat.example.org/organization.html?organization=org") };
  try {
    const html = render("[here](https://chat.example.org/organization.html?organization=org&topic=t&message=m) and [there](https://example.org/)", null);
    assert.match(html, /<a href="https:\/\/chat.example.org\/organization.html\?organization=org&amp;topic=t&amp;message=m">here<\/a>/);
    assert.match(html, /<a href="https:\/\/example.org\/" target="_blank" rel="noopener noreferrer">there<\/a>/);
  } finally { delete globalThis.window; }
  assert.equal(references.messageLink("https://chat.example.org", "o r", "t", "m"),
    "https://chat.example.org/organization.html?organization=o+r&topic=t&message=m");
});

test("resolution is batched, bounded, cached and refreshed only for watched references", async () => {
  const calls = [];
  const value = new references.ReferenceResolver("org", async path => {
    calls.push(path);
    const query = new URL(path, "https://example.org").searchParams;
    const topics = (query.get("topics") ?? "").split(",").filter(Boolean), projects = (query.get("projects") ?? "").split(",").filter(Boolean);
    return { topics: Object.fromEntries(topics.map(id => [id, id === hiddenId ? null : { id, name: "Topic " + calls.length, kind: "alpha" }])),
      projects: Object.fromEntries(projects.map(id => [id, { id, name: "Project" }])) };
  }, 5);
  const ids = Array.from({ length: 150 }, (_, index) => index.toString(16).padStart(32, "0"));
  const unwatch = ids.map(id => value.watch("topic", id));
  value.watch("topic", hiddenId); value.watch("project", projectId); value.watch("topic", ids[0]);
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.equal(calls.length, 2);
  const counts = calls.map(path => new URL(path, "https://example.org").searchParams).map(query =>
    (query.get("topics") ?? "").split(",").filter(Boolean).length + (query.get("projects") ?? "").split(",").filter(Boolean).length);
  assert.deepEqual(counts.sort((a, b) => a - b), [52, 100]);
  assert.ok(calls.every(path => path.startsWith("/organizations/org/references?")));
  assert.equal(value.text("topic", hiddenId), "#unavailable");
  assert.match(value.text("topic", ids[1]), /^#α-Topic [12]$/);
  assert.equal(value.text("topic", "f".repeat(32)), "#reference");
  value.watch("topic", ids[1]);
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.equal(calls.length, 2, "a resolved reference is reused");
  unwatch.slice(2).forEach(stop => stop());
  value.invalidate();
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.equal(calls.length, 3);
  const refreshed = new URL(calls[2], "https://example.org").searchParams;
  assert.deepEqual(refreshed.get("topics").split(",").sort(), [ids[0], ids[1], hiddenId].sort());
  assert.equal(refreshed.get("projects"), projectId);
  assert.equal(value.text("topic", ids[1]), "#α-Topic 3", "renamed targets show their current name");

  let failures = 0;
  const flaky = new references.ReferenceResolver("org", async () => {
    if (failures++ === 0) throw new Error("offline");
    return { topics: { [topicId]: { id: topicId, name: "Back", kind: "alpha" } }, projects: {} };
  }, 5);
  flaky.watch("topic", topicId);
  await new Promise(resolve => setTimeout(resolve, 40));
  assert.equal(flaky.text("topic", topicId), "#α-Back", "a failed request is retried");
});

test("every message offers its link, including to read-only readers", () => {
  const props = { organizationId: "org", topicId: "topic", messageId: "message", kind: "alpha", canWrite: false,
    message: { id: "message", content: "Hello", revision: 0 } };
  const html = renderToStaticMarkup(React.createElement(feedback.MessageActions, props));
  assert.match(html, /aria-label="Message actions"/);
  assert.match(html, /aria-label="Copy message"/);
});
