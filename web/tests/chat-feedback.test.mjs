// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

let server, components;
before(async () => {
  server = await createServer({ appType: "custom", logLevel: "silent", server: { middlewareMode: true } });
  components = await server.ssrLoadModule("/src/ChatFeedback.tsx");
});
after(async () => { await server?.close(); });

test("empty messages have no permanent emoji choices and readonly readers have no action launcher", () => {
  const html = renderToStaticMarkup(React.createElement(components.ReactionBar, {
    organizationId: "org", topicId: "topic", targetKind: "message", targetId: "message", canWrite: false,
  }));
  for (const emoji of ["👍", "👎", "🙃"]) assert.ok(!html.includes(emoji));
  assert.doesNotMatch(html, /chat-action-launcher/);
  const writable = renderToStaticMarkup(React.createElement(components.ReactionBar, {
    organizationId: "org", topicId: "topic", targetKind: "message", targetId: "message", canWrite: true,
  }));
  assert.match(writable, /aria-label="Add reaction"/);
  assert.match(writable, /:\)/); assert.doesNotMatch(writable, /<svg/);
  assert.doesNotMatch(writable, /👍|👎|🙃/);
});

test("existing reactions show initials and bounded overflow without exposing zero-count choices", () => {
  const html = renderToStaticMarkup(React.createElement(components.ReactionChips, {
    feedback: { counts: { up: 5, down: 0, upside_down: 0 }, mine: ["up"], reactors: { up: [
      { id: "b", name: "Benjamin Antieau" }, { id: "d", name: "Deven Manam" }, { id: "a", name: "Ada" },
    ] } }, canWrite: true, busy: false, onToggle: () => {},
  }));
  assert.match(html, /👍/); assert.doesNotMatch(html, /👎|🙃/);
  for (const text of ["BA", "DM", "+2", 'title="Benjamin Antieau"', 'aria-pressed="true"']) assert.ok(html.includes(text));
  assert.equal(components.reactorInitials("李 明"), "李明");
  assert.equal(components.reactorInitials("  Ada  "), "A");
});

test("only writable beta messages expose branching", () => {
  const props = { organizationId: "org", topicId: "topic", messageId: "message", kind: "beta", canWrite: true };
  const render = changes => renderToStaticMarkup(React.createElement(components.MessageActions, { ...props, ...changes }));
  assert.match(render({}), /aria-label="Message actions"/);
  assert.doesNotMatch(render({}), /Fork here/);
  assert.doesNotMatch(render({ kind: "alpha" }), /aria-label="Message actions"/);
  assert.doesNotMatch(render({ kind: "gamma" }), /aria-label="Message actions"/);
  assert.doesNotMatch(render({ canWrite: false }), /aria-label="Message actions"/);
});

test("single-message copy and links are available to readonly readers and follow reaction then actions", () => {
  const message = { content: "A single **answer**.", revision: 0 };
  const props = { organizationId: "org", topicId: "topic", messageId: "message", kind: "beta", canWrite: true, message };
  const writable = renderToStaticMarkup(React.createElement(components.MessageActions, props));
  assert.ok(writable.indexOf('aria-label="Add reaction"') < writable.indexOf('aria-label="Message actions"'));
  assert.ok(writable.indexOf('aria-label="Message actions"') < writable.indexOf('aria-label="Copy message"'));
  const readonly = renderToStaticMarkup(React.createElement(components.MessageActions, { ...props, canWrite: false }));
  assert.match(readonly, /aria-label="Copy message"/);
  assert.doesNotMatch(readonly, /aria-label="Add reaction"/);
  assert.match(readonly, /aria-label="Message actions"/, "read-only readers can still copy a message link");
  const output = renderToStaticMarkup(React.createElement(components.OutputActions, {
    organizationId: "org", topicId: "topic", targetKind: "node", targetId: "run", nodeId: "node", text: "Answer", canWrite: true,
  }));
  assert.ok(output.indexOf('aria-label="Add reaction"') < output.indexOf('aria-label="Copy message"'));
  assert.doesNotMatch(output, /Message actions|Fork here/);
});

test("readonly reaction chips remain focusable for name inspection without permitting toggles", () => {
  let toggles = 0;
  const props = {
    feedback: { counts: { up: 1, down: 0, upside_down: 0 }, mine: [], reactors: { up: [{ id: "b", name: "Benjamin Antieau" }] } },
    canWrite: false, busy: false, onToggle: () => { toggles++; },
  };
  const html = renderToStaticMarkup(React.createElement(components.ReactionChips, props));
  assert.match(html, /aria-disabled="true"/);
  assert.doesNotMatch(html, / disabled=/);
  assert.match(html, /Thumbs up: Benjamin Antieau/);
  components.ReactionChips(props).props.children[0].props.onClick();
  assert.equal(toggles, 0);
});

test("model feedback uses raw reaction counts without a correctness score", () => {
  const html = renderToStaticMarkup(React.createElement(components.ModelFeedbackPanel, { organizationId: "org" }));
  assert.match(html, /Human feedback by model/);
  assert.doesNotMatch(html, /sentiment|correctness|verified|score/i);
});
