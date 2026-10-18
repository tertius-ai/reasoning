// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

let server, timing;
before(async () => {
  server = await createServer({ appType: "custom", logLevel: "silent", server: { middlewareMode: true } });
  timing = await server.ssrLoadModule("/src/BotRunTiming.tsx");
});
after(async () => { await server?.close(); });

const unknown = { cli_started_ms: null, cli_ready_ms: null, first_text_ms: null, final_text_ms: null, cli_exited_ms: null };
const render = run => renderToStaticMarkup(React.createElement(timing.BotRunTiming, { run }));

test("bot wait stages show retained milliseconds as seconds and the CLI-reported model", () => {
  const html = render({ status: "succeeded", timing: { enqueued_at_ms: 1000, claimed_at_ms: 1250, committed_at_ms: 13250,
    queue_wait_ms: 250, claim_to_commit_ms: 12000, resolved_model: "claude-test-1",
    runner: { cli_started_ms: 40, cli_ready_ms: 2100, first_text_ms: 11000, final_text_ms: 11500, cli_exited_ms: 11800 } } });
  assert.match(html, /Queued → claimed<\/dt><dd>0\.25 s/);
  assert.match(html, /CLI session ready<\/dt><dd>2\.10 s/);
  assert.match(html, /first text<\/dt><dd>11\.00 s/);
  assert.match(html, /reply committed<\/dt><dd>12\.00 s/);
  assert.match(html, /claude-test-1/);
});

test("unreported stages say so instead of being estimated, and runs without timing render nothing", () => {
  const html = render({ status: "running", created_at: 1, updated_at: 9, timing: { enqueued_at_ms: 1000, claimed_at_ms: null,
    committed_at_ms: null, queue_wait_ms: null, claim_to_commit_ms: null, resolved_model: null, runner: unknown } });
  assert.equal((html.match(/Not reported/g) ?? []).length, 8);
  assert.doesNotMatch(html, /\d s</);
  assert.equal(render({ status: "succeeded" }), "");
});
