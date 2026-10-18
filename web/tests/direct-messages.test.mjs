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
const person = (id, name) => ({ id, name, online: true, bots: [] });

test("People is the inbox: conversation partners first by recency, then everyone else in order", () => {
  const people = [person("user-x", "Xavier"), person("user-a", "Ada"), person("user-y", "Yara"), person("user-g", "Grace")];
  const conversations = [conversation("a", "Ada", { last_message_at: 5 }), conversation("g", "Grace", { last_message_at: 9 }),
    conversation("e", "Emmy", { last_message_at: null, created_at: 7, other_member: false })];
  const inbox = direct.peopleInbox(people, conversations, item => person(item.other_user.id, item.other_user.name));
  assert.deepEqual(inbox.map(item => item.name), ["Grace", "Emmy", "Ada", "Xavier", "Yara"]);
  assert.equal(inbox[0].conversation.id, "g");
  assert.equal(inbox[1].conversation.id, "e", "a partner missing from presence is added through the placeholder");
  assert.equal(inbox[3].conversation, undefined);
  assert.deepEqual(direct.peopleInbox(people, [], () => assert.fail("no placeholder without conversations")).map(item => item.id),
    people.map(item => item.id));
});

test("marks show unread counts except for muted conversations, and the conversation's state", () => {
  const marks = (extra) => renderToStaticMarkup(React.createElement(direct.DirectMessageMarks, { conversation: conversation("a", "Ada", extra) }));
  assert.match(marks({ unread_count: 2 }), /aria-label="2 unread direct messages"/);
  assert.doesNotMatch(marks({ unread_count: 0 }), /unread direct messages/);
  const muted = marks({ unread_count: 5, muted: true });
  assert.doesNotMatch(muted, /5 unread/); assert.match(muted, /aria-label="Muted"/);
  const gone = marks({ blocked: true, other_member: false });
  assert.match(gone, />left</); assert.match(gone, />blocked</);
  assert.equal(direct.directUnread(conversation("b", "Grace", { unread_count: 5, muted: true })), 0);
  assert.equal(direct.directUnreadTotal([conversation("a", "Ada", { unread_count: 2 }),
    conversation("b", "Grace", { unread_count: 5, muted: true })]), 2);
  assert.equal(direct.directPath("org", "a b"), "/organizations/org/direct-conversations/a%20b");
});

test("the member picker lists everyone except the signed-in member", () => {
  const markup = renderToStaticMarkup(React.createElement(direct.DirectMessagePicker, {
    members: [{ id: "me", name: "Me" }, { id: "user-a", name: "Ada" }], userId: "me", onStart: () => {}, onClose: () => {},
  }));
  assert.match(markup, /aria-label="Message a member"/);
  assert.match(markup, /<option value="user-a">Ada<\/option>/);
  assert.doesNotMatch(markup, />Me</);
  assert.match(renderToStaticMarkup(React.createElement(direct.NewDirectMessageButton, { open: false, onToggle: () => {} })),
    /aria-label="New direct message"[^>]*aria-expanded="false"/);
});
