// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

let server, direct;
before(async () => {
  server = await createServer({ appType: "custom", logLevel: "silent", server: { middlewareMode: true } });
  direct = await server.ssrLoadModule("/src/DirectMessages.tsx");
});
after(async () => { await server?.close(); });

const conversation = (id, name, extra = {}) => ({ id, created_at: 1, other_user: { id: "user-" + id, name }, other_member: true,
  muted: false, blocked: false, last_read_sequence: 0, last_message_sequence: 3, last_message_at: 2, unread_count: 0, ...extra });

test("direct-message navigation shows unread counts except for muted conversations", () => {
  const conversations = [conversation("a", "Ada", { unread_count: 2 }), conversation("b", "Grace", { unread_count: 5, muted: true }),
    conversation("c", "Emmy", { blocked: true, other_member: false })];
  const markup = renderToStaticMarkup(React.createElement(direct.DirectMessageNavigation, {
    conversations, members: [{ id: "me", name: "Me" }, { id: "user-a", name: "Ada" }], userId: "me", selected: "a", canStart: true,
    onSelect: () => {}, onStart: () => {},
  }));
  assert.match(markup, /aria-label="Direct messages"/);
  assert.match(markup, /aria-label="2 unread direct messages"/);
  assert.doesNotMatch(markup, /5 unread direct messages/);
  assert.match(markup, /aria-label="2 unread direct messages in total"/);
  assert.match(markup, /aria-label="Muted"/); assert.match(markup, />blocked</); assert.match(markup, />left</);
  assert.match(markup, /aria-current="page"[^>]*title="Direct messages with Ada"/);
  assert.match(markup, /aria-label="New direct message"/);
  assert.equal(direct.directUnread(conversations[1]), 0);
  assert.equal(direct.directPath("org", "a b"), "/organizations/org/direct-conversations/a%20b");
});

test("viewers see their conversations without a control to start one", () => {
  const markup = renderToStaticMarkup(React.createElement(direct.DirectMessageNavigation, {
    conversations: [], members: [], userId: "me", selected: null, canStart: false, onSelect: () => {}, onStart: () => {},
  }));
  assert.doesNotMatch(markup, /New direct message/);
  assert.match(markup, /Click a name in People to send a message/);
});
