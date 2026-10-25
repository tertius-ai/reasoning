// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

let server, mentions, api, app;
before(async () => {
  server = await createServer({ appType: "custom", logLevel: "silent", server: { middlewareMode: true } });
  mentions = await server.ssrLoadModule("/src/mentions.tsx");
  api = await server.ssrLoadModule("/src/organization-api.ts");
  app = await server.ssrLoadModule("/src/OrganizationApp.tsx");
});
after(async () => { await server?.close(); });

const ada = "a".repeat(32), grace = "b".repeat(32);
const members = [{ id: ada, name: "Ada Lovelace" }, { id: grace, name: "Grace Hopper" }];

test("sent mentions render as tinted names, and mentions of the reader are marked", () => {
  const markdown = api.mentionMarkdown(`Thanks @[${ada}] and @[${grace}].`, members);
  assert.equal(markdown, `Thanks [@Ada Lovelace](#mention/${ada}) and [@Grace Hopper](#mention/${grace}).`);
  const html = renderToStaticMarkup(React.createElement(mentions.MentionSelf.Provider, { value: grace },
    React.createElement(app.MessageMarkdown, { markdown })));
  assert.match(html, new RegExp(`<span class="org-mention" data-member-id="${ada}">@Ada Lovelace</span>`));
  assert.match(html, new RegExp(`<span class="org-mention org-mention-self" data-member-id="${grace}">@Grace Hopper</span>`));
  assert.doesNotMatch(html, /<a /, "a mention is not a navigable link");
  assert.equal(mentions.mentionFromHref("#mention/" + ada), ada);
  assert.equal(mentions.mentionFromHref("https://example.org/#mention/" + ada), null);
});

test("the picker shows initials", () => {
  assert.equal(mentions.mentionInitials("Ada Lovelace"), "AL");
  assert.equal(mentions.mentionInitials("  Grace  "), "G");
  assert.equal(mentions.mentionInitials("Fixture Codex Bot"), "FB");
});

test("a chat's participants take the palette in id order, then those who spoke there, as in Signal's groups", () => {
  const tones = mentions.chatTones([grace, ada], [ada, "model:gpt", "c".repeat(32)]);
  assert.deepEqual([...tones], [[ada, 0], [grace, 1], ["c".repeat(32), 2], ["model:gpt", 3]]);
  assert.equal(mentions.speakerTone({ author_id: grace }, tones), "org-person-tone-1");
  assert.equal(mentions.speakerTone({ author_id: ada, author_kind: "assistant", assistant_model: "gpt" }, tones), "org-person-tone-3",
    "a model's reply takes the model's color, not the requester's");
  // Everyone differs until the palette's 36 colors are used up; then the order wraps.
  const many = Array.from({ length: 40 }, (_, index) => index.toString(16).padStart(32, "0"));
  const crowd = mentions.chatTones(many);
  assert.equal(mentions.TONES, 36);
  assert.equal(new Set(many.slice(0, 36).map(id => crowd.get(id))).size, 36);
  assert.equal(crowd.get(many[36]), 0);
  // Order of discovery does not matter: every browser sorts the same group the same way.
  assert.deepEqual([...mentions.chatTones([ada, grace])], [...mentions.chatTones([grace, ada])]);
});

test("the composer mirror tints exactly the bound mention ranges, not references", () => {
  const text = "Hi @Ada Lovelace and #α-Results";
  const bindings = [{ start: 3, end: 16, id: ada, label: "@Ada Lovelace" },
    { start: 21, end: 31, id: "c".repeat(32), label: "#α-Results", wire: "#[topic:" + "c".repeat(32) + "]" }];
  const html = renderToStaticMarkup(React.createElement(mentions.MentionMirror, { text, bindings, self: ada }));
  assert.match(html, /aria-hidden="true"/);
  assert.match(html, /Hi <mark class="org-mention org-mention-self">@Ada Lovelace<\/mark> and #α-Results/);
  assert.equal((html.match(/<mark/g) ?? []).length, 1);
});
