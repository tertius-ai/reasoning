// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { after, before, test } from "node:test";
import { chromium } from "playwright";
import { build, preview } from "vite";

const user = "a".repeat(32), org = "b".repeat(32), topics = ["c".repeat(32), "e".repeat(32)];
const model = { id: "fixture/reasoner", name: "Reasoner", supports_tools: true, supports_reasoning: true,
  reasoning_efforts: [], supported_parameters: [], input_modalities: [], output_modalities: [] };
const messages = topic => Array.from({ length: 3 }, (_, index) => ({ id: `${topic.slice(0, 4)}-message-${index}`, sequence: index + 1,
  topic_id: topic, created_at: 1_800_000_000 + index, author_kind: "human", author_id: user, client_id: "fixture-" + index,
  content: `Message ${index} of ${topic.slice(0, 4)}` }));
// Fifty receipts sized like the audited topic (about 520 kB decoded as complete rows); the newest is still running.
const START = 1_800_000_000;
const receipts = Array.from({ length: 50 }, (_, index) => ({ id: index.toString(16).padStart(32, "0"), organization_id: org,
  run_id: "f".repeat(32), sequence: index + 1, name: "web_search", status: index === 49 ? "requested" : "completed",
  created_at: START + index, finished_at: index === 49 ? null : START + index,
  arguments: { query: "q".repeat(2000), filters: Array(5).fill("x".repeat(200)) },
  result: index === 49 ? null : { items: Array(2).fill({ title: "t".repeat(300), body: "b".repeat(700) }) } }));
/** The server's `GET /research-tools`: inclusive `after`, newest first, at most 50, summaries on request. */
function receiptList(url) {
  const after = Number(url.searchParams.get("after") ?? 0), summary = ["1", "true"].includes(url.searchParams.get("summary") ?? "");
  return receipts.filter(receipt => receipt.created_at >= after).sort((a, b) => b.created_at - a.created_at).slice(0, 50)
    .map(({ arguments: values, result, ...row }) => summary ? { id: row.id, name: row.name, status: row.status,
      created_at: row.created_at, finished_at: row.finished_at }
      : { ...row, arguments_json: JSON.stringify(values), result_json: result && JSON.stringify(result), arguments: values, result });
}

let outDir, server, browser;
before(async () => {
  const root = fileURLToPath(new URL("../", import.meta.url));
  outDir = await mkdtemp(join(tmpdir(), "reasoning-research-requests-"));
  await build({ root, logLevel: "silent", build: { outDir, emptyOutDir: true } });
  server = await preview({ root, logLevel: "silent", build: { outDir }, preview: { host: "127.0.0.1", port: 0 } });
  browser = await chromium.launch({ headless: true });
});
after(async () => {
  await browser?.close(); await server?.close();
  if (outDir) await rm(outDir, { recursive: true, force: true });
});

/** Open a production build of the organization interface against a mocked API, recording every API request and response size. */
async function application({ hold = () => null } = {}) {
  const page = await browser.newPage(), errors = [], requests = [];
  const hierarchy = { projects: [{ id: "project", name: "Testing", visibility: "organization" }],
    channels: [{ id: "channel", name: "research", project_id: "project" }],
    topics: topics.map((id, index) => ({ id, name: index ? "Geometry" : "Algebra", channel_id: "channel", kind: "beta",
      default_model: model.id, knowledge_tools: ["ledger", "bibliography", "journal"] })), topic_state: [] };
  page.on("pageerror", error => errors.push(error.message));
  await page.route("**/api/**", async route => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname;
    const record = { method: request.method(), path, query: url.search, body: request.postDataJSON?.() ?? null, bytes: 0 };
    requests.push(record);
    await hold(path, url);
    const topic = topics.find(id => path.includes("/topics/" + id + "/"));
    let data = [];
    if (path === "/api/health") data = { initialized: true };
    else if (path === "/api/auth/me") data = { id: user, name: "Alice", email: "alice@example.org" };
    else if (path === "/api/organizations") data = [{ id: org, name: "Testing", role: "owner" }];
    else if (path.endsWith("/hierarchy")) data = hierarchy;
    else if (path.endsWith("/members")) data = [{ id: user, name: "Alice", role: "owner" }];
    else if (path.endsWith("/presence")) data = { humans: [], bots: [] };
    else if (path.endsWith("/inference-configuration")) data = { allowed_models: [model.id], tools: [] };
    else if (path.endsWith("/model-prices")) data = { models: [model], allowed_models: [model.id], fetched_at: 1_800_000_000 };
    else if (path.endsWith("/model-benchmarks")) data = { models: {}, status: "unavailable", meta: null };
    else if (path.endsWith("/research-instructions")) data = { system_prompt: "Research safely.", prompt_version: "fixture" };
    else if (path.endsWith("/research-settings")) data = { tools: [] };
    else if (path.endsWith("/research-tools")) data = topic === topics[0] ? receiptList(url) : [];
    else if (path.endsWith("/navigation-usage")) data = { projects: {}, channels: {}, topics: {} };
    else if (path.endsWith("/messages")) data = url.searchParams.has("after") ? [] : messages(topic);
    else if (path.endsWith("/message-updates")) data = { after: 0, messages: [] };
    else if (path.endsWith("/reactions/messages")) data = {};
    else if (path.endsWith("/goal") || path.endsWith("/read")) data = null;
    else if (path.endsWith("/mathematics")) data = request.method() === "POST" ? { references: [] } : { claims: {} };
    const body = JSON.stringify(data); record.bytes = Buffer.byteLength(body);
    await route.fulfill({ status: 200, contentType: "application/json", body });
  });
  await page.goto(server.resolvedUrls.local[0] + "organization.html");
  await page.locator("#message-3").waitFor();
  return { page, requests, errors, navigation: page.getByRole("navigation", { name: "Projects and channels" }) };
}

async function until(predicate, description) {
  for (const deadline = Date.now() + 10000; !(await predicate());) {
    assert.ok(Date.now() < deadline, "timed out: " + description);
    await new Promise(resolve => setTimeout(resolve, 50));
  }
}

test("receipt lists are summaries, and a revisit keeps them and requests only newer receipts", { timeout: 120000 }, async () => {
  let release = () => {}, holding = false;
  const { page, requests, errors, navigation } = await application({ hold: async path => {
    if (holding && path.endsWith(`/topics/${topics[0]}/research-tools`))
      await new Promise(resolve => { const previous = release; release = () => { previous(); resolve(); }; });
  } });
  const receiptRequests = () => requests.filter(request => request.path.endsWith(`/topics/${topics[0]}/research-tools`));
  const running = page.getByRole("status").filter({ hasText: "Using web search" });
  await running.waitFor();
  const first = receiptRequests()[0];
  assert.equal(first.query, "?summary=1&after=0");
  assert.ok(first.bytes < 8000, `first receipt summary list: ${first.bytes} bytes`);
  await until(() => receiptRequests().length >= 2, "a second receipt poll");
  assert.equal(receiptRequests()[1].query, `?summary=1&after=${START + 49}`, "the cursor stays at the running receipt");

  await navigation.getByRole("button", { name: /^beta topic -Geometry\b/ }).click();
  await page.locator('#message-3').filter({ hasText: "Message 2 of eeee" }).waitFor();
  // The running receipt finishes while the topic is not shown.
  Object.assign(receipts[49], { status: "completed", finished_at: START + 60, result: { items: [] } });
  holding = true;
  const revisit = receiptRequests().length;
  await navigation.getByRole("button", { name: /^beta topic -Algebra\b/ }).click();
  await page.locator('#message-3').filter({ hasText: "Message 2 of cccc" }).waitFor();
  await running.waitFor({ timeout: 2000 });
  holding = false; release();
  await running.waitFor({ state: "detached" });
  const revisited = receiptRequests().slice(revisit);
  assert.ok(revisited.length >= 1 && revisited.every(request => request.query === `?summary=1&after=${START + 49}`),
    "the revisit requests receipts from the retained cursor, not from zero");
  await until(() => receiptRequests().length >= revisit + 2, "a receipt poll after the finished receipt");
  assert.equal(receiptRequests().at(-1).query, `?summary=1&after=${START + 49}`, "a finished receipt releases the cursor to the newest");

  const total = receiptRequests().reduce((sum, request) => sum + request.bytes, 0);
  const full = Buffer.byteLength(JSON.stringify(receiptList(new URL("http://x/?after=0"))));
  if (process.env.REASONING_REQUEST_REPORT) console.log(JSON.stringify({ receiptRequests: receiptRequests().length,
    receiptBytes: total, revisitBytes: revisited.reduce((sum, request) => sum + request.bytes, 0), completeListBytes: full }));
  assert.ok(total < full / 10, `every receipt request together (${total} bytes) is far below one complete list (${full} bytes)`);
  assert.deepEqual(errors, []);
  await page.close();
});

test("the Bibliography's read-only reference listing keeps the shared catalog and benchmarks", { timeout: 120000 }, async () => {
  const { page, requests, errors, navigation } = await application();
  const capabilities = async () => {
    await page.getByRole("button", { name: "Account menu", exact: true }).click();
    await page.getByRole("button", { name: "Model capabilities", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "Model capabilities", exact: true });
    await dialog.getByText(/models · refreshed/).waitFor();
    await dialog.getByRole("button", { name: "Close dialog" }).click();
  };
  await capabilities(); await capabilities();
  await page.getByRole("link", { name: "Open bibliography", exact: true }).click();
  await page.getByRole("heading", { name: "Research bibliography", exact: true }).waitFor();
  await until(() => requests.some(request => request.method === "POST" && request.path.endsWith("/mathematics")), "the reference listing");
  assert.deepEqual(requests.find(request => request.method === "POST" && request.path.endsWith("/mathematics")).body,
    { name: "ledger_references", arguments: {} });
  await navigation.getByRole("button", { name: /^beta topic -Algebra\b/ }).click();
  await page.locator("#message-3").waitFor();
  await capabilities();
  const count = suffix => requests.filter(request => request.path.endsWith(suffix)).length;
  if (process.env.REASONING_REQUEST_REPORT) console.log(JSON.stringify({ catalogReads: count("/model-prices"), benchmarkReads: count("/model-benchmarks") }));
  assert.equal(count("/model-prices"), 1, "one catalog transfer across the Bibliography visit");
  assert.equal(count("/model-benchmarks"), 1, "one benchmark transfer across the Bibliography visit");
  assert.deepEqual(errors, []);
  await page.close();
});
