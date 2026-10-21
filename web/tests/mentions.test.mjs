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

test("the picker shows initials on a stable color per member", () => {
  assert.equal(mentions.mentionInitials("Ada Lovelace"), "AL");
  assert.equal(mentions.mentionInitials("  Grace  "), "G");
  assert.equal(mentions.mentionInitials("Fixture Codex Bot"), "FB");
  assert.equal(mentions.mentionTone(ada), mentions.mentionTone(ada));
  assert.ok(mentions.mentionTone(grace) >= 0 && mentions.mentionTone(grace) < 6);
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
