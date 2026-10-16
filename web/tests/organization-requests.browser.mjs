// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { chromium } from "playwright";
import { build, preview } from "vite";

const sleep = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));

/**
 * Count API requests while an idle beta topic with a math transcript opens, then while it stays open. A production
 * build is measured: development StrictMode deliberately runs every startup effect twice.
 */
async function observeIdleBetaTopic() {
  const root = fileURLToPath(new URL("../", import.meta.url)), outDir = await mkdtemp(join(tmpdir(), "reasoning-requests-"));
  await build({ root, logLevel: "silent", build: { outDir, emptyOutDir: true } });
  const server = await preview({ root, logLevel: "silent", build: { outDir }, preview: { host: "127.0.0.1", port: 0 } });
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage(), errors = [], requests = [];
    const user = "a".repeat(32), org = "b".repeat(32), topic = "c".repeat(32);
    const hierarchy = { projects: [{ id: "project", name: "Testing", visibility: "organization" }],
      channels: [{ id: "channel", name: "research", project_id: "project" }],
      topics: [{ id: topic, name: "Algebra", channel_id: "channel", kind: "beta", default_model: "fixture/reasoner", knowledge_tools: [] }], topic_state: [] };
    const model = { id: "fixture/reasoner", name: "Reasoner", supports_tools: true, supports_reasoning: true,
      reasoning_efforts: [], supported_parameters: [], input_modalities: [], output_modalities: [] };
    const messages = Array.from({ length: 13 }, (_, index) => ({ id: "message-" + index, sequence: index + 1, topic_id: topic,
      created_at: 1_800_000_000 + index, author_kind: index % 2 ? "assistant" : "human", author_id: user, assistant_model: model.id,
      client_id: "fixture-" + index, content: `Step ${index}: $$\\sum_{k=1}^{n} k^${index} = \\int_0^1 x^${index}\\,dx$$ and $a_${index}$.` }));
    page.on("pageerror", error => errors.push(error.message));
    await page.route("**/api/**", async route => {
      const request = route.request(), path = new URL(request.url()).pathname;
      requests.push({ at: Date.now(), method: request.method(), path });
      let data = [];
      if (path === "/api/health") data = { initialized: true };
      else if (path === "/api/auth/me") data = { id: user, name: "Alice", email: "alice@example.org" };
      else if (path === "/api/organizations") data = [{ id: org, name: "Testing", role: "owner" }];
      else if (path.endsWith("/hierarchy")) data = hierarchy;
      else if (path.endsWith("/members")) data = [{ id: user, name: "Alice", role: "owner" }];
      else if (path.endsWith("/presence")) data = { humans: [], bots: [] };
      else if (path.endsWith("/inference-configuration")) data = { allowed_models: [model.id], tools: [] };
      else if (path.endsWith("/model-prices")) data = { models: [model], allowed_models: [model.id] };
      else if (path.endsWith("/research-instructions")) data = { system_prompt: "Research safely.", prompt_version: "fixture" };
      else if (path.endsWith("/research-settings")) data = { tools: [] };
      else if (path.endsWith("/compute-configuration")) data = { images: [], policy: { allowed_images: [] } };
      else if (path.endsWith("/navigation-usage")) data = { projects: {}, channels: {}, topics: {} };
      else if (path.endsWith("/messages")) data = new URL(request.url()).searchParams.has("after") ? [] : messages;
      else if (path.endsWith("/message-updates")) data = { after: 0, messages: [] };
      else if (path.endsWith("/reactions")) data = Object.fromEntries(messages.map(message => [message.id, { counts: { up: 0, down: 0, upside_down: 0 }, mine: [] }]));
      else if (path.includes("/reactions/")) data = { counts: { up: 0, down: 0, upside_down: 0 }, mine: [] };
      else if (path.endsWith("/goal")) data = null;
      else if (path.endsWith("/read")) data = null;
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(data) });
    });
    const started = Date.now();
    await page.goto(server.resolvedUrls.local[0] + "organization.html");
    await page.locator("#message-13").waitFor();
    await sleep(started + 3000 - Date.now());
    const startup = requests.length;
    await sleep(20000);
    const idle = requests.filter(request => request.at >= started + 3000 && request.at < started + 23000);
    assert.deepEqual(errors, []);
    return { startup, startupPaths: requests.slice(0, startup).map(request => request.path), idle: idle.map(request => request.path) };
  } finally {
    await browser.close();
    await server.close();
    await rm(outDir, { recursive: true, force: true });
  }
}

test("an idle beta topic bounds startup and foreground polling requests", { timeout: 60000 }, async () => {
  const observed = await observeIdleBetaTopic();
  const count = (paths, suffix) => paths.filter(path => path.endsWith(suffix)).length;
  if (process.env.REASONING_REQUEST_REPORT) console.log(JSON.stringify({ startup: observed.startup, idle: observed.idle.length,
    startupPaths: observed.startupPaths.map(path => path.replace(/[a-f0-9]{32}/g, ":id")),
    idlePaths: observed.idle.map(path => path.replace(/[a-f0-9]{32}/g, ":id")) }));
  assert.equal(observed.startupPaths.filter(path => path === "/api/organizations").length, 1, "organizations are listed once at startup");
  assert.ok(count(observed.startupPaths, "/goal") <= count(observed.startupPaths, "/agents"),
    "research controls reuse the agent activity view's goal read");
  assert.equal(count(observed.startupPaths, "/compute-configuration"), 0, "compute configuration waits for the submission dialog");
  assert.equal(observed.startupPaths.filter(path => path.includes("/reactions/")).length, 1, "one batch reads every message's reactions");
  assert.ok(observed.startup <= 46, `startup requests: ${observed.startup}`);
  // Messages keep their two-second cadence; idle activity views slow down.
  assert.ok(observed.idle.length <= 100, `idle requests in twenty seconds: ${observed.idle.length}`);
});
