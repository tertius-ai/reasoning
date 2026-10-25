// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

let server, commands, beta;
before(async () => {
  server = await createServer({ appType: "custom", logLevel: "silent", server: { middlewareMode: true } });
  commands = await server.ssrLoadModule("/src/TopicCommands.tsx");
  beta = await server.ssrLoadModule("/src/BetaConversation.tsx");
});
after(async () => { await server?.close(); });

test("slash choices are topic-specific, narrowly filtered, and absent for ordinary prose", () => {
  const render = (kind, text) => renderToStaticMarkup(React.createElement(commands.SlashCommandPalette, { kind, text, onSelect() {} }));
  assert.equal(render("beta", "Please /files"), "");
  assert.equal(render("beta", "/goal An explicit objective"), "");
  assert.match(render("beta", "/"), /\/ledger|\/files|\/goal|\/context|\/tools/);
  assert.doesNotMatch(render("beta", "/"), /\/workflow|\/compare|\/json/);
  assert.match(render("gamma", "/co"), /\/compare|\/compute/);
  assert.doesNotMatch(render("gamma", "/co"), /\/goal|\/ledger/);
  assert.doesNotMatch(render("alpha", "/"), /\/stop|\/goal|\/compute/);
  assert.match(render("alpha", "/"), /\/attach/);
  assert.doesNotMatch(render("alpha", "/"), /\/tex/);
});

test("keyboard help exposes send, mentions, slash commands and topic restrictions", () => {
  const html = renderToStaticMarkup(React.createElement(commands.ShortcutsHelp, { onClose() {} }));
  for (const label of ["Toggle help outside text fields", "Shift+Enter", "Ctrl/⌘+K", "Ctrl/⌘+Shift+P", "@", "/goal", "/compute", "/compare"]) assert.ok(html.includes(label));
  assert.match(html, /aria-label="Keyboard shortcuts and commands"/);
});

test("the composer's one button stops running work, otherwise starts or resumes, and gives way to send while composing", () => {
  const render = changes => renderToStaticMarkup(React.createElement(commands.TransportControls, {
    canPlay: false, canStop: false, onPlay() {}, onStop() {}, ...changes,
  }));
  assert.match(render({}), /aria-label="Start or resume"[^>]*disabled=""/);
  assert.doesNotMatch(render({}), /Stop current work/);
  assert.doesNotMatch(render({ canPlay: true }), /disabled=""/);
  assert.match(render({ canStop: true }), /aria-label="Stop current work"/); assert.doesNotMatch(render({ canStop: true }), /Start or resume/);
  assert.match(render({ hidePlay: true, canStop: true }), /Stop current work/, "Stop stays while work runs, even while composing");
  assert.equal(render({ hidePlay: true }), "", "with text in the box the composer's send button takes the place");
  assert.doesNotMatch(render({}), /Pause agents|Stop agents|Cancel response/);
});

test("working indicator uses only live recorded round activity", () => {
  assert.equal(renderToStaticMarkup(React.createElement(beta.RunProgress, { run: { status: "succeeded" } })), "");
  const running = renderToStaticMarkup(React.createElement(beta.RunProgress, { run: { status: "running", activity: [] } }));
  assert.match(running, /org-working|Working…/);
  const activity = renderToStaticMarkup(React.createElement(beta.RunProgress, { run: { status: "running", activity: [{ sequence: 1, phase: "round", round: 2 }] } }));
  assert.match(activity, /Round 2 · working/); assert.doesNotMatch(activity, /thinking|verified|proof found/);
  const latest = renderToStaticMarkup(React.createElement(beta.RunProgress, { run: { status: "running", activity: [
    { sequence: 1, phase: "round", round: 2 }, { sequence: 2, phase: "tool_started", tool: "source_read" },
  ] } }));
  assert.match(latest, /Using source read/);
  assert.doesNotMatch(latest, /Round 2|<ol|<li/);
  // A queued request in a paused agent conversation is not working: it says so, without the live indicator.
  const paused = renderToStaticMarkup(React.createElement(beta.RunProgress, { run: { status: "queued", activity: [],
    agent: { role: "coordinator", root_run: "r", thread_status: "paused" } } }));
  assert.match(paused, /Paused · starts only when resumed/); assert.doesNotMatch(paused, /org-working|Working/);
  assert.equal(beta.pausedRun({ status: "running", agent: { root_run: "r", thread_status: "paused" } }), false, "a running reply finishes its step");
});
