// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

let server, commands, beta, activity;
before(async () => {
  server = await createServer({ appType: "custom", logLevel: "silent", server: { middlewareMode: true } });
  commands = await server.ssrLoadModule("/src/TopicCommands.tsx");
  beta = await server.ssrLoadModule("/src/BetaConversation.tsx");
  activity = await server.ssrLoadModule("/src/TopicActivity.tsx");
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

test("the composer's one button only starts or resumes, and gives way to send while composing; it never stops", () => {
  const render = changes => renderToStaticMarkup(React.createElement(commands.TransportControls, {
    canPlay: false, onPlay() {}, ...changes,
  }));
  assert.match(render({}), /aria-label="Start or resume"[^>]*disabled=""/);
  assert.doesNotMatch(render({ canPlay: true }), /disabled=""/);
  assert.equal(render({ hidePlay: true }), "", "with text in the box the composer's send button takes the place");
  assert.doesNotMatch(render({}) + render({ canPlay: true }), /Stop|Pause agents|Cancel response/);
});

test("a card's Stop stops only its own work and never toggles the card", () => {
  let stopped = 0;
  const html = renderToStaticMarkup(React.createElement(activity.ActivityCard, {
    title: "Workflow", status: "running", createdAt: 1, initiallyOpen: false,
    action: React.createElement(activity.CardStop, { label: "Stop this workflow", onStop: () => { stopped += 1; } }),
  }, null));
  assert.match(html, /<summary>.*<button type="button" class="org-card-stop" aria-label="Stop this workflow"[^>]*>.*Stop<\/button><\/summary>/);
  const button = activity.CardStop({ label: "Stop", onStop: () => { stopped += 1; } });
  const event = { prevented: false, propagation: true, preventDefault() { this.prevented = true; }, stopPropagation() { this.propagation = false; } };
  button.props.onClick(event);
  assert.deepEqual([stopped, event.prevented, event.propagation], [1, true, false]);
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
