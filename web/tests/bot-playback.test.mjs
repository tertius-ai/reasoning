// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

let server, playback, conversation;
before(async () => {
  server = await createServer({ appType: "custom", logLevel: "silent", server: { middlewareMode: true } });
  playback = await server.ssrLoadModule("/src/BotPlayback.ts");
  conversation = await server.ssrLoadModule("/src/BotConversation.tsx");
});
after(async () => { await server?.close(); });

test("bot stop availability requires a live run and requester, bot-owner, or administrator write authority", () => {
  for (const status of ["queued", "running", "waiting_approval"]) {
    const run = { status, requested_by: "requester", owner_user_id: "bot-owner" };
    assert.ok(playback.canStopBot(run, "requester", true, false));
    assert.ok(playback.canStopBot(run, "bot-owner", true, false));
    assert.ok(playback.canStopBot(run, "administrator", true, true));
    assert.equal(playback.canStopBot(run, "stranger", true, false), false);
    assert.equal(playback.canStopBot(run, "administrator", false, true), false);
  }
  for (const status of ["succeeded", "failed", "cancelled", "outcome_unknown"])
    assert.equal(playback.canStopBot({ status, requested_by: "requester" }, "requester", true, false), false);
});

test("running bots pulse their real reported phase without fabricated reasoning", () => {
  const html = renderToStaticMarkup(React.createElement(conversation.BotRunStatus, { run: { status: "running", phase: "using_tools" } }));
  assert.match(html, /org-working/); assert.match(html, /using tools…/); assert.doesNotMatch(html, /thinking/);
  const waiting = renderToStaticMarkup(React.createElement(conversation.BotRunStatus, { run: { status: "waiting_approval" } }));
  assert.match(waiting, /bot owner/); assert.doesNotMatch(waiting, /org-working/);
});

test("a mention the owner's policy excludes explains why it was not run", () => {
  const html = renderToStaticMarkup(React.createElement(conversation.BotRunStatus, { run: { status: "cancelled", error_code: "requester_not_permitted" } }));
  assert.match(html, /Not run: this bot accepts requests only from its owner/); assert.doesNotMatch(html, /<button|org-working/);
});

test("stopped bot outcomes stay explicitly unknown without restart or per-run cancel buttons", () => {
  const html = renderToStaticMarkup(React.createElement(conversation.BotRunStatus, { run: { status: "outcome_unknown", error_code: "stop_requested" } }));
  assert.match(html, /local outcome is unknown/); assert.match(html, /before any new attempt/);
  assert.doesNotMatch(html, /<button|org-working|terminated|free/);
});
