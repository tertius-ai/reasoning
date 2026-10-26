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
const user = "a".repeat(32), org = "b".repeat(32), topics = ["c".repeat(32), "e".repeat(32)];
const model = { id: "fixture/reasoner", name: "Reasoner", supports_tools: true, supports_reasoning: true,
  reasoning_efforts: [], supported_parameters: [], input_modalities: [], output_modalities: [] };
const transcript = topic => Array.from({ length: 13 }, (_, index) => ({ id: `${topic.slice(0, 4)}-message-${index}`, sequence: index + 1,
  topic_id: topic, created_at: 1_800_000_000 + index, author_kind: index % 2 ? "assistant" : "human", author_id: user,
  assistant_model: model.id, client_id: "fixture-" + index,
  content: `Step ${index} of ${topic.slice(0, 4)}: $$\\sum_{k=1}^{n} k^${index} = \\int_0^1 x^${index}\\,dx$$ and $a_${index}$.` }));

/**
 * Run a scenario against a production build of the organization interface and a mocked API, recording every API
 * request. A production build is measured: development StrictMode deliberately runs every startup effect twice.
 */
async function withApplication(scenario, { hold = () => null, stream = true } = {}) {
  const root = fileURLToPath(new URL("../", import.meta.url)), outDir = await mkdtemp(join(tmpdir(), "reasoning-requests-"));
  await build({ root, logLevel: "silent", build: { outDir, emptyOutDir: true } });
  // Topic event streams are served by the preview server itself, because a routed response cannot stream.
  const events = { name: "topic-events", configurePreviewServer(server) {
    server.middlewares.use((request, response, next) => {
      if (!/\/topics\/[a-f0-9]{32}\/events$/.test(new URL(request.url, "http://localhost").pathname)) return next();
      if (!stream) { response.statusCode = 503; return response.end('{"detail":"topic event stream disabled"}'); }
      response.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-store, no-transform" });
      const send = (name, data) => response.write(`event: ${name}\ndata: ${JSON.stringify(data)}\n\n`);
      send("ready", { heartbeat_ms: 15000 });
      send("messages", { sequence: 13, updates: 0 });
      for (const [name, data] of [["runs", { order: [], runs: [] }], ["agents", []], ["goal", null], ["bot-runs", []]]) send(name, data);
    });
  } };
  const server = await preview({ root, logLevel: "silent", plugins: [events], build: { outDir }, preview: { host: "127.0.0.1", port: 0 } });
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage(), errors = [], requests = [];
    const hierarchy = { projects: [{ id: "project", name: "Testing", visibility: "organization" }],
      channels: [{ id: "channel", name: "research", project_id: "project" }],
      topics: topics.map((id, index) => ({ id, name: index ? "Geometry" : "Algebra", channel_id: "channel", kind: "beta",
        default_model: model.id, knowledge_tools: [] })), topic_state: [] };
    page.on("pageerror", error => errors.push(error.message));
    // Routed responses are delivered whole, so event streams bypass routing and are only recorded.
    page.on("request", request => {
      const url = new URL(request.url());
      if (url.pathname.endsWith("/events")) requests.push({ at: Date.now(), method: request.method(), path: url.pathname, query: url.search });
    });
    await page.route(url => url.pathname.startsWith("/api/") && !url.pathname.endsWith("/events"), async route => {
      const request = route.request(), url = new URL(request.url()), path = url.pathname;
      requests.push({ at: Date.now(), method: request.method(), path, query: url.search });
      await hold(path, url);
      const topic = topics.find(id => path.includes("/topics/" + id + "/"));
      let data = [];
      if (path === "/api/health") data = { initialized: true };
      else if (path === "/api/auth/me") data = { id: user, name: "Alice", email: "alice@example.org" };
      else if (path === "/api/organizations") data = [{ id: org, name: "Testing", role: "owner" }];
      else if (path.endsWith("/hierarchy")) data = hierarchy;
      else if (path.endsWith("/members")) data = [{ id: user, name: "Alice", role: "owner" }];
      else if (path.endsWith("/user-administration")) data = { members: [{ id: user, name: "Alice", email: "alice@example.org", role: "owner" }],
        invitations: [], mail: { enabled: false }, recoveries: [], events: [] };
      else if (path.endsWith("/presence")) data = { humans: [], bots: [] };
      else if (path.endsWith("/inference-configuration")) data = { allowed_models: [model.id], tools: [] };
      else if (path.endsWith("/model-prices")) data = { models: [model], allowed_models: [model.id], fetched_at: 1_800_000_000 };
      else if (path.endsWith("/model-benchmarks")) data = { models: {} };
      else if (path.endsWith("/research-instructions")) data = { system_prompt: "Research safely.", prompt_version: "fixture" };
      else if (path.endsWith("/research-settings")) data = { tools: [] };
      else if (path.endsWith("/compute-configuration")) data = { images: [], policy: { allowed_images: [] } };
      else if (path.endsWith("/navigation-usage")) data = { projects: {}, channels: {}, topics: {} };
      else if (path.endsWith("/messages")) data = url.searchParams.has("after") ? [] : transcript(topic);
      else if (path.endsWith("/message-updates")) data = { after: 0, messages: [] };
      else if (path.endsWith("/reactions/messages")) data = Object.fromEntries(transcript(topic).map(message =>
        [message.id, { counts: { up: 0, down: 0, upside_down: 0 }, mine: [] }]));
      else if (path.endsWith("/goal") || path.endsWith("/read")) data = null;
      else if (path.endsWith("/invitation-mail")) data = { enabled: false, daily_limit: null, worker_running: false };
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(data) });
    });
    const started = Date.now();
    await page.goto(server.resolvedUrls.local[0] + "organization.html");
    await page.locator("#message-13").waitFor();
    const result = await scenario({ page, requests, started });
    assert.deepEqual(errors, []);
    return result;
  } finally {
    await browser.close();
    await server.close();
    await rm(outDir, { recursive: true, force: true });
  }
}

async function idleRequests(options) {
  const observed = await withApplication(async ({ requests, started }) => {
    await sleep(started + 3000 - Date.now());
    const startup = requests.length;
    await sleep(20000);
    const idle = requests.filter(request => request.at >= started + 3000 && request.at < started + 23000);
    return { startup, startupPaths: requests.slice(0, startup).map(request => request.path), idle: idle.map(request => request.path) };
  }, options);
  if (process.env.REASONING_REQUEST_REPORT) console.log(JSON.stringify({ stream: options.stream, startup: observed.startup,
    idle: observed.idle.length, startupPaths: observed.startupPaths.map(path => path.replace(/[a-f0-9]{32}/g, ":id")),
    idlePaths: observed.idle.map(path => path.replace(/[a-f0-9]{32}/g, ":id")) }));
  return observed;
}
const count = (paths, suffix) => paths.filter(path => path.endsWith(suffix)).length;

test("an idle beta topic bounds startup and foreground polling requests", { timeout: 60000 }, async () => {
  const observed = await idleRequests({ stream: true });
  assert.equal(observed.startupPaths.filter(path => path === "/api/organizations").length, 1, "organizations are listed once at startup");
  // The activity view's first goal read is shared with research controls, which then refresh it on their own.
  assert.ok(count(observed.startupPaths, "/goal") <= 2, "research controls reuse the agent activity view's goal read");
  assert.equal(count(observed.startupPaths, "/compute-configuration"), 0, "compute configuration waits for the submission dialog");
  assert.equal(observed.startupPaths.filter(path => path.includes("/reactions/")).length, 1, "one batch reads every message's reactions");
  assert.equal(count(observed.startupPaths, "/events"), 1, "one event stream serves every view of the topic");
  assert.ok(observed.startup <= 40, `startup requests: ${observed.startup}`);
  // The live stream replaces message, run, agent and bot-invocation polling while the topic is idle.
  for (const suffix of ["/messages", "/message-updates", "/inference-runs", "/agents", "/bot-runs", "/events"])
    assert.equal(count(observed.idle, suffix), 0, `idle ${suffix} requests`);
  assert.ok(observed.idle.length <= 55, `idle requests in twenty seconds: ${observed.idle.length}`);
});

test("without an event stream, idle views keep their bounded polling", { timeout: 60000 }, async () => {
  const observed = await idleRequests({ stream: false });
  assert.ok(count(observed.startupPaths, "/goal") <= count(observed.startupPaths, "/agents"),
    "research controls reuse the agent activity view's goal read");
  assert.ok(count(observed.idle, "/messages") >= 8, "messages keep their two-second cadence");
  assert.ok(count(observed.idle, "/inference-runs") >= 3, "run activity keeps polling");
  // Messages keep their two-second cadence; idle activity views slow down.
  assert.ok(observed.idle.length <= 100, `idle requests in twenty seconds: ${observed.idle.length}`);
});

test("revisiting a topic shows its snapshot at once and the model catalog is fetched once", { timeout: 60000 }, async () => {
  let release = () => {}, holding = false;
  await withApplication(async ({ page, requests }) => {
    const navigation = page.getByRole("navigation", { name: "Projects and channels" });
    await navigation.getByRole("button", { name: /^beta topic -Geometry\b/ }).click();
    await page.locator('[data-sequence="13"]').filter({ hasText: "Step 12 of eeee" }).waitFor();
    // Hold every request for the first topic: its revisit must render from the retained snapshot.
    holding = true;
    const revisit = requests.length;
    await navigation.getByRole("button", { name: /^beta topic -Algebra\b/ }).click();
    await page.locator('[data-sequence="13"]').filter({ hasText: "Step 12 of cccc" }).waitFor({ timeout: 2000 });
    const messages = requests.slice(revisit).filter(request => request.path.endsWith(`/topics/${topics[0]}/messages`));
    assert.ok(messages.length >= 1 && messages.every(request => request.query.includes("after=13")), "the revisit fetches only the delta");
    holding = false; release();
    for (const _ of [0, 1]) {
      await page.getByRole("button", { name: "Account menu", exact: true }).click();
      await page.getByRole("button", { name: "Model capabilities", exact: true }).click();
      const dialog = page.getByRole("dialog", { name: "Model capabilities", exact: true });
      await dialog.getByText(/models · refreshed/).waitFor();
      await dialog.getByRole("button", { name: "Close dialog" }).click();
    }
    assert.equal(requests.filter(request => request.path.endsWith("/model-prices")).length, 1,
      "two topics and two dialog openings share one catalog transfer");
    const administration = requests.length;
    await page.getByRole("button", { name: "Account menu", exact: true }).click();
    await page.getByRole("button", { name: "Administration", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "Testing administration", exact: true });
    await dialog.locator(".org-admin-list strong").filter({ hasText: "Alice" }).waitFor();
    const opened = requests.slice(administration).map(request => request.path.split("/").at(-1));
    for (const other of ["invitations", "invitation-mail", "provider-keys", "policy"]) assert.ok(!opened.includes(other), other);
    await dialog.getByRole("button", { name: "Invitations", exact: true }).click();
    for (let waited = 0; !requests.slice(administration).some(request => request.path.endsWith("/invitation-mail")); waited += 50) {
      assert.ok(waited < 5000, "the invitations tab loads its own data"); await sleep(50);
    }
  }, { hold: async path => {
    if (holding && path.includes(`/topics/${topics[0]}/`)) await new Promise(resolve => { const previous = release; release = () => { previous(); resolve(); }; });
  } });
});
