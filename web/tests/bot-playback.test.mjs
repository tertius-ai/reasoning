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
  const requester = renderToStaticMarkup(React.createElement(conversation.BotRunStatus, { run: { status: "waiting_approval", approval_by: "requester" } }));
  assert.match(requester, /requesting member to approve the exact GitHub issue preview/); assert.doesNotMatch(requester, /bot owner/);
});

test("a requester reviewing their own issue preview is told the owner delegated that decision", async () => {
  const approvals = await server.ssrLoadModule("/src/BotApprovals.tsx");
  const row = { id: "issue", bot_id: "bot", run_id: "run", requester_name: "Bob", topic_name: "Research", created_at: 1, kind: "issue",
    repository: "research/bench", title: "Exact", body: "Body\n\n---\n\nRequested in Reasoning by `Bob` through the bot `Math Bot`.",
    document_sha256: "f".repeat(64) };
  const render = role => renderToStaticMarkup(React.createElement(approvals.BotApprovalDialog, { organizationId: "org",
    bot: { id: "bot", name: "Math Bot", online: true }, rows: [{ ...row, approval_role: role }], onClose() {}, onDecided() {} }));
  assert.match(render("requester"), /You requested this issue/); assert.match(render("requester"), /Publish once/);
  assert.doesNotMatch(render("owner"), /You requested this issue/);
});

test("a request the owner's sharing setting excludes explains why it was not run", () => {
  const status = run => renderToStaticMarkup(React.createElement(conversation.BotRunStatus, { run }));
  const html = status({ status: "cancelled", error_code: "requester_not_permitted" });
  assert.match(html, /Not run: the bot owner(&#x27;|')s “Who can use this bot” setting does not include you/);
  assert.doesNotMatch(html, /<button|org-working/);
  assert.match(status({ status: "outcome_unknown", error_code: "requester_not_permitted" }),
    /Stopped: the bot owner(&#x27;|')s “Who can use this bot” setting no longer includes you\. The local outcome is unknown/);
});

test("stopped bot outcomes stay explicitly unknown until the runner confirms, without restart or per-run cancel buttons", () => {
  const status = (run) => renderToStaticMarkup(React.createElement(conversation.BotRunStatus, { run }));
  for (const error_code of ["stop_requested", "cancelled"]) {
    const html = status({ status: "outcome_unknown", error_code });
    assert.match(html, /Stopping: waiting for the bot&#x27;s runner/); assert.match(html, /local outcome is unknown/);
    assert.doesNotMatch(html, /<button|org-working|terminated|free/);
    assert.match(status({ status: "cancelled", error_code }), /Stopped\./);
  }
});
