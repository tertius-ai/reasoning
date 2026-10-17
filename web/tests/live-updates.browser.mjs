// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
// Model output that the API already returns must reach the DOM promptly: measured from the moment the mocked
// server makes output readable, through response or event receipt and state update, to the DOM mutation.
import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { after, before, test } from "node:test";
import { chromium } from "playwright";
import { build, preview } from "vite";

const sleep = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
const user = "a".repeat(32), org = "b".repeat(32), topic = "c".repeat(32), runId = "d".repeat(32);
const model = { id: "fixture/reasoner", name: "Reasoner", supports_tools: true, supports_reasoning: true,
  reasoning_efforts: [], supported_parameters: [], input_modalities: [], output_modalities: [] };
const root = fileURLToPath(new URL("../", import.meta.url));
let outDir;
before(async () => {
  outDir = await mkdtemp(join(tmpdir(), "reasoning-live-"));
  await build({ root, logLevel: "silent", build: { outDir, emptyOutDir: true } });
});
after(async () => { if (outDir) await rm(outDir, { recursive: true, force: true }); });

const median = values => [...values].sort((a, b) => a - b)[Math.floor(values.length / 2)];

/** A mocked API with one running beta run. Agent and goal reads are slow; the event stream is optional. */
function mockServer({ stream: streaming, sideDelay }) {
  let stream = streaming;
  const message = (sequence, content, extra = {}) => ({ id: "message-" + sequence, sequence, topic_id: topic, author_id: user,
    author_kind: "human", content, client_id: "fixture-" + sequence, created_at: 1_800_000_000 + sequence, ...extra });
  const state = { messages: [message(1, "Count slowly.")], version: 0, served: [], clients: new Set(), requests: [],
    run: { id: runId, request_id: "request", requested_by: user, model: model.id, status: "running", cancel_requested: 0,
      output_text: "", error_code: null, message_id: null, context_through_sequence: 1, accounting_state: "reserved",
      actual_microusd: null, reserved_microusd: 1000, prompt_tokens: null, completion_tokens: null,
      provider_generation_id: null, created_at: 1_800_000_001, activity: [] } };
  const tokens = text => [...text.matchAll(/TOKEN(\d+)/g)].map(match => Number(match[1]));
  const json = (response, data) => { response.setHeader("Content-Type", "application/json"); response.end(JSON.stringify(data)); };
  const send = (client, name, data) => {
    client.response.write(`event: ${name}\ndata: ${JSON.stringify(data)}\n\n`);
    if (name === "runs") state.served.push({ at: Date.now(), tokens: tokens(JSON.stringify(data)) });
  };
  // Like the server's watcher: re-read every 250 ms while work is active and send only what changed.
  const watcher = setInterval(() => {
    for (const client of state.clients) {
      if (client.version === state.version) continue;
      client.version = state.version;
      const sequence = state.messages.at(-1).sequence;
      if (client.sequence !== sequence) { client.sequence = sequence; send(client, "messages", { sequence, updates: 0 }); }
      const run = state.run, previous = client.output;
      const patch = previous !== undefined && run.output_text.startsWith(previous)
        ? (({ output_text, ...rest }) => ({ ...rest, output_append: output_text.slice(previous.length) }))(run) : run;
      client.output = run.output_text;
      send(client, "runs", { order: [run.id], runs: [patch] });
    }
  }, 250);
  const plugin = { name: "mock-api", configurePreviewServer(server) {
    server.middlewares.use(async (request, response, next) => {
      const url = new URL(request.url, "http://localhost"), path = url.pathname;
      if (!path.startsWith("/api/")) return next();
      state.requests.push({ at: Date.now(), path });
      if (path.endsWith("/events")) {
        if (!stream) { response.statusCode = 503; return json(response, { detail: "topic event stream disabled" }); }
        response.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-store, no-transform" });
        const client = { response, version: state.version, sequence: state.messages.at(-1).sequence, output: undefined };
        state.clients.add(client);
        request.on("close", () => state.clients.delete(client));
        response.write(`event: ready\ndata: {"heartbeat_ms":15000}\n\n`);
        send(client, "messages", { sequence: client.sequence, updates: 0 });
        client.output = state.run.output_text;
        send(client, "runs", { order: [state.run.id], runs: [state.run] });
        for (const [name, data] of [["agents", []], ["goal", null], ["bot-runs", []]]) send(client, name, data);
        return;
      }
      if (path.endsWith("/agents") || path.endsWith("/goal")) await sleep(sideDelay);
      if (path.endsWith("/inference-runs")) {
        const body = JSON.stringify([state.run]);
        response.setHeader("Content-Type", "application/json");
        return response.end(body, () => state.served.push({ at: Date.now(), tokens: tokens(body) }));
      }
      if (path === "/api/health") return json(response, { initialized: true });
      if (path === "/api/auth/me") return json(response, { id: user, name: "Alice", email: "alice@example.org" });
      if (path === "/api/organizations") return json(response, [{ id: org, name: "Testing", role: "owner" }]);
      if (path.endsWith("/hierarchy")) return json(response, { projects: [{ id: "project", name: "Testing", visibility: "organization" }],
        channels: [{ id: "channel", name: "research", project_id: "project" }],
        topics: [{ id: topic, name: "Algebra", channel_id: "channel", kind: "beta", default_model: model.id, knowledge_tools: [] }],
        topic_state: [] });
      if (path.endsWith("/members")) return json(response, [{ id: user, name: "Alice", role: "owner" }]);
      if (path.endsWith("/presence")) return json(response, { humans: [], bots: [] });
      if (path.endsWith("/inference-configuration")) return json(response, { allowed_models: [model.id], tools: [] });
      if (path.endsWith("/model-prices")) return json(response, { models: [model], allowed_models: [model.id], fetched_at: 1_800_000_000 });
      if (path.endsWith("/model-benchmarks")) return json(response, { models: {} });
      if (path.endsWith("/research-instructions")) return json(response, { system_prompt: "Research safely.", prompt_version: "fixture" });
      if (path.endsWith("/research-settings")) return json(response, { tools: [] });
      if (path.endsWith("/navigation-usage")) return json(response, { projects: {}, channels: {}, topics: {} });
      if (path.endsWith("/messages")) {
        const afterSequence = Number(url.searchParams.get("after") ?? 0);
        return json(response, state.messages.filter(item => item.sequence > afterSequence));
      }
      if (path.endsWith("/message-updates")) return json(response, { after: 0, messages: [] });
      if (path.endsWith("/goal") || path.endsWith("/read")) return json(response, null);
      if (path.endsWith("/invitation-mail")) return json(response, { enabled: false, daily_limit: null, worker_running: false });
      if (path.endsWith("/reactions/messages")) return json(response, {});
      if (path.includes("/reactions/")) return json(response, { counts: { up: 0, down: 0, upside_down: 0 }, mine: [] });
      return json(response, []);
    });
  } };
  const publish = text => { state.run = { ...state.run, output_text: state.run.output_text + text }; state.version += 1; };
  const finish = () => {
    const reply = message(2, state.run.output_text, { author_kind: "assistant", assistant_model: model.id, inference_run_id: runId });
    state.messages = [...state.messages, reply];
    state.run = { ...state.run, status: "succeeded", message_id: reply.id, accounting_state: "settled" };
    state.version += 1;
  };
  // The stream breaks and later requests for it are refused, as when a proxy stops passing it.
  const drop = () => { stream = false; for (const client of state.clients) client.response.destroy(); state.clients.clear(); };
  return { plugin, state, publish, finish, drop, close: () => clearInterval(watcher) };
}

/** Publish output in steps and measure each step from availability to DOM; then finish the reply. */
async function measure({ stream, sideDelay = 700, samples = 8, dropAt = 0 }) {
  const mock = mockServer({ stream, sideDelay });
  const server = await preview({ root, logLevel: "silent", plugins: [mock.plugin], build: { outDir }, preview: { host: "127.0.0.1", port: 0 } });
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage(), errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.addInitScript(() => {
      window.__reasoningTiming = [];
      window.__seen = {};
      window.__gap = false;
      new MutationObserver(() => {
        for (const output of document.querySelectorAll("[data-run-id] pre"))
          for (const match of output.textContent.matchAll(/TOKEN(\d+)/g)) window.__seen[match[1]] ??= Date.now();
        // Once the final step is visible, the run card or the reply message must show it at every mutation.
        if (window.__final && window.__seen[window.__final] && !new RegExp("TOKEN" + window.__final + "\\b").test(document.body.textContent))
          window.__gap = true;
      }).observe(document, { subtree: true, childList: true, characterData: true });
    });
    await page.goto(server.resolvedUrls.local[0] + "organization.html");
    await page.locator(`[data-run-id="${runId}"]`).waitFor();
    if (stream) await page.waitForFunction(() => window.__reasoningTiming.some(entry => entry.name === "event:runs"));
    const published = [];
    for (let index = 1; index <= samples; index += 1) {
      await sleep(600 + Math.random() * 800);
      if (index === dropAt) mock.drop();
      published.push(Date.now());
      mock.publish(`TOKEN${index} `);
      if (index === samples) await page.evaluate(final => { window.__final = final; }, index);
      await page.waitForFunction(token => window.__seen[token], index, { timeout: 10000 });
    }
    mock.finish();
    const finished = Date.now();
    await page.waitForFunction(() => document.querySelector('[data-sequence="2"]') && !document.querySelector("[data-run-id]"),
      null, { timeout: 10000 });
    const reply = Date.now() - finished;
    const { seen, timing, origin, gap } = await page.evaluate(() => ({ seen: window.__seen, timing: window.__reasoningTiming,
      origin: performance.timeOrigin, gap: window.__gap }));
    const at = name => timing.filter(entry => entry.name === name).map(entry => origin + entry.at);
    const received = [...at("event:runs"), ...at("runs:response")].sort((a, b) => a - b), applied = at("runs:applied");
    const steps = published.map((start, index) => {
      const served = mock.state.served.find(entry => entry.tokens.includes(index + 1))?.at;
      const receipt = received.find(time => time >= served - 5), state = applied.find(time => time >= receipt);
      return { total: seen[index + 1] - start, available: served - start, transfer: receipt - served,
        state: state - receipt, dom: seen[index + 1] - state };
    });
    assert.deepEqual(errors, []);
    return { steps, gap, reply, requests: mock.state.requests };
  } finally {
    await browser.close();
    await server.close();
    mock.close();
  }
}

function report(label, { steps, reply, gap }) {
  const summary = Object.fromEntries(["total", "available", "transfer", "state", "dom"].map(key =>
    [key, Math.round(median(steps.map(step => step[key])))]));
  if (process.env.REASONING_LATENCY_REPORT) console.log(JSON.stringify({ label, median: summary,
    totals: steps.map(step => Math.round(step.total)), finished_to_reply_message: reply, output_hidden_before_reply: gap }));
  return summary;
}

test("polled run output is applied without waiting for slow agent and goal reads", { timeout: 90000 }, async () => {
  const result = await measure({ stream: false }), summary = report("polling", result);
  // Output is applied within a few milliseconds of its response, never after the 700 ms agent/goal reads.
  assert.ok(summary.state < 100, `response → state: ${summary.state} ms`);
  assert.ok(summary.total < 1300, `available → DOM: ${summary.total} ms`);
  assert.equal(result.gap, false, "the finished output stays visible until its reply message replaces it");
});

test("streamed run output reaches the DOM within one watcher interval", { timeout: 90000 }, async () => {
  const result = await measure({ stream: true }), summary = report("stream", result);
  assert.ok(summary.state < 100, `event → state: ${summary.state} ms`);
  assert.ok(summary.total < 600, `available → DOM: ${summary.total} ms`);
  assert.equal(result.gap, false, "the finished output stays visible until its reply message replaces it");
  const idle = result.requests.filter(request => /\/(inference-runs|agents|messages|message-updates|bot-runs)$/.test(request.path));
  assert.ok(idle.length <= 12, `polled activity requests while streaming: ${idle.length}`);
});

test("a stream that breaks falls back to polling without losing output", { timeout: 90000 }, async () => {
  const result = await measure({ stream: true, dropAt: 4 });
  report("stream dropped at step 4", result);
  assert.equal(result.steps.length, 8, "every step reached the DOM");
  assert.ok(median(result.steps.slice(4).map(step => step.total)) < 2500, "polling resumes at its active cadence");
  assert.equal(result.gap, false);
});
