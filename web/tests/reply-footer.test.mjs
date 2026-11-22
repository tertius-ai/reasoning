import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

let server, footer;
before(async () => {
  server = await createServer({ appType: "custom", logLevel: "silent", server: { middlewareMode: true } });
  footer = await server.ssrLoadModule("/src/ReplyFooter.tsx");
});
after(async () => { await server?.close(); });

const checkpoint = { id: "m1", author_kind: "assistant", inference_run_id: "r1", usage: null,
  content: "Summary: Two agents are checking the sources.\n\n*Waiting: delegated work. This is a checkpoint, not a completed answer.*" };
const waiting = { id: "r1", message_id: "m1", answer_state: { status: "waiting", reason: "delegated work", checkpoint: null } };
const continued = { id: "r1", message_id: "m1", answer_state: { status: "continued", reason: "delegated work", next_run: "r2", checkpoint: null } };
const render = (message, onShowMessage) => renderToStaticMarkup(React.createElement(footer.ReplyFooter,
  { message, userId: "u1", names: {}, onShowMessage }));

test("a reply still waiting, or never handed on, is not marked as continued", () => {
  assert.equal(footer.continuation(checkpoint, []), undefined);
  assert.equal(footer.continuation(checkpoint, [waiting]), undefined);
  assert.equal(footer.continuation({ ...checkpoint, inference_run_id: null }, [continued]), undefined);
  assert.doesNotMatch(render(checkpoint), /later reply/);
});

test("a reply handed on in this page is continued, and links its continuation once that reply is posted", () => {
  assert.deepEqual(footer.continuation(checkpoint, [continued]), { message_id: null });
  assert.deepEqual(footer.continuation(checkpoint, [continued, { id: "r2", message_id: null, answer_state: { status: "working" } }]),
    { message_id: null });
  assert.deepEqual(footer.continuation(checkpoint, [continued, { id: "r2", message_id: "m2", answer_state: { status: "response_ready" } }]),
    { message_id: "m2" });
});

test("a reply loaded as continued keeps that, and learns its continuation's message from the live run", () => {
  assert.deepEqual(footer.continuation({ ...checkpoint, continued: { message_id: "m2" } }, []), { message_id: "m2" });
  assert.deepEqual(footer.continuation({ ...checkpoint, continued: { message_id: null } },
    [continued, { id: "r2", message_id: "m2", answer_state: { status: "response_ready" } }]), { message_id: "m2" });
});

test("the footer says a later reply carries the answer, with a link once that reply is posted", () => {
  const shown = [];
  const linked = render({ ...checkpoint, continued: { message_id: "m2" } }, id => shown.push(id));
  assert.match(linked, /role="note"[^>]*>.*A later reply continued this work and carries the answer\./);
  assert.match(linked, /<button type="button">Show that reply<\/button>/);
  const pending = render({ ...checkpoint, continued: { message_id: null } }, id => shown.push(id));
  assert.match(pending, /A later reply continued this work and carries the answer\./);
  assert.doesNotMatch(pending, /Show that reply/);
  assert.doesNotMatch(render({ ...checkpoint, continued: { message_id: "m2" } }), /Show that reply/, "no link without a way to show it");
});
