import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { mkdir, mkdtemp, readFile, realpath, rm } from "node:fs/promises";
import { createServer } from "node:http";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { chromium, _electron as electron } from "playwright";
import {
  assertDesktopPlatform,
  assertDesktopProfile,
  desktopLaunchOptions,
} from "./helpers/desktop-launch.mjs";

const repository = fileURLToPath(new URL("../../", import.meta.url));
const password = "public inference fixture password";
const key = "sk-or-organization-fixture-only";
const models = ["test/one", "test/two", "test/slow"];
async function until(predicate, description) {
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    if (await predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error("Timed out: " + description);
}
async function visible(locator) { await locator.waitFor({ state: "visible", timeout: 20000 }); }
async function send(page, text) {
  await page.getByLabel("Message", { exact: true }).fill(text);
  await page.getByRole("button", { name: /^(Send|Send message)$/, exact: true }).click();
  await visible(page.locator(".org-message").filter({ hasText: text }));
}

for (const loss of ["network", "gateway-timeout"]) test(`shared beta responses survive ${loss}, stream, replay once, cancel, reconcile, and restart`, { timeout: 120000 }, async (context) => {
  const desktopReviewer = process.env.REASONING_BETA_TEST_DESKTOP === "1";
  if (desktopReviewer) assertDesktopPlatform();
  const temporary = await realpath(await mkdtemp(path.join(os.tmpdir(), "reasoning-inference-browser-")));
  let cleanupStarted = false;
  context.after(() => {
    if (!cleanupStarted) return rm(temporary, { recursive: true, force: true });
  });
  const state = path.join(temporary, "state");
  const listener = net.createServer(); listener.listen(0, "127.0.0.1"); await once(listener, "listening");
  const port = listener.address().port; await new Promise((resolve) => listener.close(resolve));
  const url = `http://127.0.0.1:${port}`;
  const calls = [], errors = [];
  let closedSlow = false, catalogRequests = 0;
  const provider = createServer(async (request, response) => {
    if (request.url.split("?")[0] === "/api/v1/key") {
      response.writeHead(200, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ data: { usage_daily: calls.length * 0.000008,
        limit: 50, limit_remaining: 50 - calls.length * 0.000008, limit_reset: "daily" } })); return;
    }
    if (request.url.split("?")[0] === "/api/v1/models") {
      catalogRequests++;
      response.writeHead(200, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ data: models.map((id) => ({
        id, architecture: { input_modalities: ["text"], output_modalities: ["text"] },
        pricing: { prompt: "0.000001", completion: "0.000002" }, context_length: 200000,
        supported_parameters: ["max_completion_tokens"], top_provider: { max_completion_tokens: 131072 },
      })) })); return;
    }
    // Benchmark metadata is optional; the server degrades to an empty snapshot.
    if (request.url.split("?")[0] === "/api/v1/benchmarks") { response.writeHead(404).end(); return; }
    assert.equal(request.url, "/api/v1/chat/completions");
    assert.equal(request.headers.authorization, "Bearer " + key);
    let body = ""; for await (const chunk of request) body += chunk;
    const payload = JSON.parse(body); calls.push(payload);
    assert.equal(payload.provider.allow_fallbacks, true);
    assert.equal(payload.provider.sort, undefined);
    assert.equal(payload.max_completion_tokens, undefined);
    response.writeHead(200, { "Content-Type": "text/event-stream" });
    const event = (value) => response.write("data: " + JSON.stringify(value) + "\n\n");
    const id = "gen-browser-" + calls.length;
    event({ id, choices: [{ index: 0, delta: { content: payload.model === "test/slow" ? "Partial calculation" : "Shared mathematics: " } }] });
    if (payload.model === "test/slow") {
      const heartbeat = setInterval(() => response.write(": heartbeat\n\n"), 250);
      response.on("close", () => { clearInterval(heartbeat); closedSlow = true; });
      return;
    }
    const timer = setTimeout(() => {
      event({ id, choices: [{ index: 0, delta: { content: "$1+1=2$." } }] });
      event({ id, choices: [], usage: { cost: "0.0000079", prompt_tokens: 20, completion_tokens: 5 } });
      response.end("data: [DONE]\n\n");
    }, 2500);
    response.on("close", () => clearTimeout(timer));
  });
  provider.listen(0, "127.0.0.1"); await once(provider, "listening");
  const providerUrl = `http://127.0.0.1:${provider.address().port}/api/v1`;
  let server, browser, desktop, logs = "";
  const start = async () => {
    server = spawn(path.join(repository, ".venv/bin/python"), ["-m", "reasoning_ledger.server.cli", "--data-dir", state,
      "serve", "--public-url", url, "--port", String(port), "--frontend-dir", path.join(repository, "web/dist"),
      "--openrouter-url", providerUrl], { cwd: repository });
    server.stdout.on("data", (chunk) => { logs += chunk; }); server.stderr.on("data", (chunk) => { logs += chunk; });
    await until(async () => { try { return (await fetch(url + "/api/health")).ok; } catch { return false; } }, "server readiness");
  };
  const stop = async () => {
    if (!server || server.exitCode !== null || server.signalCode !== null) return;
    server.kill("SIGTERM");
    await Promise.race([once(server, "exit"), new Promise((resolve) => setTimeout(resolve, 5000))]);
    if (server.exitCode === null && server.signalCode === null) { server.kill("SIGKILL"); await once(server, "exit"); }
  };
  try {
    await start();
    const bootstrap = await (await fetch(url + "/api/setup", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
      setup_token: await readFile(path.join(state, "setup-token"), "utf8"), email: "owner@example.org", name: "Owner",
      password, organization_name: "Research", transport: "bearer",
    }) })).json();
    const org = bootstrap.organization_id;
    const api = async (endpoint, body, method = "POST") => {
      const response = await fetch(url + "/api/organizations/" + org + endpoint, {
        method, headers: { "Content-Type": "application/json", Authorization: "Bearer " + bootstrap.access_token }, body: JSON.stringify(body),
      });
      assert.ok(response.ok, await response.clone().text());
      return response.status === 204 ? undefined : response.json();
    };
    const project = await api("/projects", { name: "Algebra" });
    const channel = await api("/channels", { name: "research", project_id: project.id });
    const topic = await api("/topics", { name: "Shared models", channel_id: channel.id, kind: "beta" });
    await api("/topics", { name: "Human notes", channel_id: channel.id, kind: "alpha" });
    browser = await chromium.launch({ headless: true });
    const pages = [];
    // The member's topic event stream stays offline until the polling-recovery check below. While a stream is live
    // the views poll only as a slow safety net, and an established stream outlives later request interception, so
    // the connection warnings are exercised on the polling fallback.
    let memberStreamOffline = true;
    for (const [email, name] of [["owner@example.org", "Owner"], ["member@example.org", "Member"], ["reviewer@example.org", "Reviewer"]]) {
      if (name !== "Owner") {
        const invitation = await api("/invitations", { email });
        const response = await fetch(url + "/api/invitations/accept", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ token: invitation.token, name, password }) });
        assert.equal(response.status, 200);
      }
      let page;
      if (desktopReviewer && name === "Reviewer") {
        const noSandbox = process.env.REASONING_ELECTRON_TEST_NO_SANDBOX === "1";
        if (noSandbox) context.diagnostic("Trusted local provider/organization fixtures without OS sandbox; this does not verify production sandbox enforcement.");
        const directory = path.join(temporary, "desktop-profile");
        await mkdir(directory, { mode: 0o700 });
        const profileDirectory = await realpath(directory);
        desktop = await electron.launch(desktopLaunchOptions({ repository, profileDirectory, serverUrl: url }));
        await assertDesktopProfile(desktop, profileDirectory, { sandbox: !noSandbox });
        await until(() => desktop.windows().some((item) => item.url().startsWith(url)), "desktop server connection");
        page = desktop.windows().find((item) => item.url().startsWith(url));
        assert.deepEqual(await page.evaluate(() => [typeof window.require, typeof window.process, typeof window.reasoningConnection]), ["undefined", "undefined", "undefined"]);
      } else page = await (await browser.newContext({ viewport: { width: 1200, height: 920 } })).newPage();
      page.on("pageerror", (error) => errors.push(error.message));
      if (name === "Member") await page.route("**/topics/*/events", (route) => memberStreamOffline ? route.abort("failed") : route.continue());
      await page.goto(url); await page.getByLabel("Email", { exact: true }).fill(email);
      await page.getByLabel("Password", { exact: true }).fill(password); await page.getByRole("button", { name: "Sign in", exact: true }).click();
      await page.getByRole("navigation", { name: "Projects and channels" }).getByRole("button", { name: /^beta topic -Shared models\b/ }).click();
      await visible(page.getByRole("heading", { name: "β-Shared models" }));
      pages.push(page);
    }
    const [owner, member, reviewer] = pages;
    await member.locator(".org-topic-menu > summary").click();
    await member.getByRole("group", { name: "Topic details", exact: true }).getByRole("button", { name: /^System prompt\b/ }).click();
    await visible(member.getByText(/Ordinary questions and discussion need a useful reply/));
    await member.getByRole("button", { name: "Close dialog", exact: true }).click();
    let pollingOffline = true;
    const pollRoutes = ["**/topics/*/inference-runs", "**/topics/*/goal"];
    const interruptPolling = async (route) => {
      if (pollingOffline && route.request().method() === "GET") await route.abort("failed");
      else await route.continue();
    };
    for (const pattern of pollRoutes) await member.route(pattern, interruptPolling);
    await visible(member.getByText(/Unable to refresh agent activity; reconnecting/));
    await visible(member.getByText(/Unable to refresh research status; reconnecting/));
    pollingOffline = false; memberStreamOffline = false;
    await until(async () => await member.getByText(/Unable to refresh .*; reconnecting/).count() === 0, "polling recovery clears connection warnings");
    for (const pattern of pollRoutes) await member.unroute(pattern, interruptPolling);
    await member.getByRole("button", { name: "Choose reply model", exact: true }).click();
    await visible(member.getByRole("radio", { name: /test\/one/ }));
    await member.getByRole("button", { name: "Choose reply model", exact: true }).click();
    await owner.getByRole("button", { name: "Account menu", exact: true }).click();
    await owner.getByRole("button", { name: "Administration" }).click();
    const settings = owner.getByRole("dialog");
    await settings.getByRole("button", { name: "Inference", exact: true }).click();
    await settings.getByRole("button", { name: "+ Add key", exact: true }).click();
    assert.equal(await settings.getByLabel("Key label").inputValue(), "");
    assert.equal(await settings.getByLabel("OpenRouter API key").inputValue(), "");
    await settings.getByLabel("Key label").fill("Shared key"); await settings.getByLabel("OpenRouter API key").fill(key);
    await settings.getByRole("button", { name: "Add OpenRouter key" }).click();
    await visible(settings.getByRole("button", { name: "+ Add key", exact: true }));
    assert.equal(await settings.getByLabel("OpenRouter API key").count(), 0);
    await visible(settings.locator("li").filter({ hasText: "Shared key" }));
    assert.equal(await settings.getByLabel("Allowed OpenRouter models").count(), 0);
    assert.equal(await settings.getByLabel("Monthly budget (USD)").count(), 0);
    await visible(settings.getByRole("heading", { name: "Daily spending limit" }));
    await settings.getByRole("button", { name: "Close dialog" }).click();
    await owner.bringToFront();
    await owner.evaluate(() => window.dispatchEvent(new Event("focus")));
    const daily = owner.locator(".org-daily-spend");
    await visible(daily.getByLabel(/^OpenRouter daily spend:/));
    assert.equal(await owner.locator(".org-daily-spend").count(), 1);
    assert.ok((await daily.locator(".org-daily-meter").boundingBox()).height <= 5);
    await daily.locator("summary").click();
    await visible(daily.getByText("Daily spending · OpenRouter", { exact: true }));
    assert.match(await daily.innerText(), /00:00 UTC/);
    await daily.locator("summary").click();
    if (process.env.REASONING_DAILY_SPEND_SCREENSHOT) await owner.screenshot({ path: process.env.REASONING_DAILY_SPEND_SCREENSHOT });
    // A failed read clears the displayed figures rather than inventing a zero balance.
    await owner.route("**/provider-daily-spend", route => route.abort("failed"));
    await owner.evaluate(() => window.dispatchEvent(new Event("focus")));
    await visible(daily.getByText("Spend unavailable", { exact: true }));
    await owner.unroute("**/provider-daily-spend");
    await owner.evaluate(() => window.dispatchEvent(new Event("focus")));
    await visible(daily.getByLabel(/^OpenRouter daily spend:/));
    await owner.getByRole("button", { name: "Account menu", exact: true }).click();
    await owner.getByRole("button", { name: "Usage and energy", exact: true }).click();
    const usagePanel = owner.getByRole("dialog", { name: "Usage and energy" });
    await usagePanel.getByText("Energy and data-center assumptions", { exact: true }).click();
    await usagePanel.getByLabel("Model identifier", { exact: true }).fill("test/one");
    await usagePanel.getByLabel("Input kWh per million tokens", { exact: true }).fill("10");
    await usagePanel.getByLabel("Output kWh per million tokens", { exact: true }).fill("100");
    await usagePanel.getByLabel("Assumed inference data-center location", { exact: true }).fill("Assumed test region");
    await usagePanel.getByLabel("Location grid intensity (g CO₂e/kWh)", { exact: true }).fill("400");
    await usagePanel.getByLabel("Source, year, and estimation assumptions", { exact: true }).fill("Browser fixture scenario, not a measured footprint");
    await usagePanel.getByRole("button", { name: "Save model assumptions", exact: true }).click();
    await until(async () => (await usagePanel.getByLabel("Model identifier", { exact: true }).inputValue()) === "", "impact assumptions saved");
    await usagePanel.getByRole("button", { name: "Close dialog" }).click();
    await visible(member.getByRole("button", { name: "Choose reply model", exact: true }).filter({ hasText: "test/one" }));
    let hidden = false, hiddenComplete = false;
    await member.route("**/topics/*/inference-runs", async (route) => {
      if (route.request().method() !== "POST" || hidden) return route.continue();
      hidden = true; await route.fetch();
      if (loss === "network") await route.abort("failed");
      else await route.fulfill({ status: 504, contentType: "application/json", body: JSON.stringify({ detail: "Gateway timeout after acceptance" }) });
      hiddenComplete = true;
    });
    const cancellations = [];
    member.on("request", (sent) => { if (sent.method() === "POST" && /\/cancel$/.test(new URL(sent.url()).pathname)) cancellations.push(sent.url()); });
    await send(member, "One question for the default model.");
    await until(() => hiddenComplete, "lost submission reply delivered");
    const pending = member.getByRole("group", { name: "Unconfirmed reply request", exact: true });
    await visible(pending);
    const explanation = await pending.innerText();
    assert.match(explanation, /Your message was saved, but the server did not confirm the reply request to test\/one/);
    assert.match(explanation, loss === "network" ? /The connection failed before the server answered/
      : /HTTP 504: Gateway timeout after acceptance/);
    assert.match(explanation, /Conversation context ends at message #\d+/);
    assert.doesNotMatch(explanation, /needs attention|context through|Abandon/);
    await member.unroute("**/topics/*/inference-runs");
    if (loss === "network") {
      // Retry beside the explanation replays the same client ID.
      await pending.getByRole("button", { name: "Retry same request", exact: true }).click();
    } else {
      // Dismissal clears only local recovery state: the accepted run continues.
      member.once("dialog", (dialog) => dialog.accept());
      await pending.getByRole("button", { name: "Dismiss pending retry", exact: true }).click();
    }
    await until(async () => (await pending.count()) === 0, "pending reply request resolved");
    await visible(reviewer.getByLabel("Streaming response from test/one"));
    await until(() => calls.length === 1, "one provider call, not a replay");
    assert.deepEqual(cancellations, []);
    await visible(member.locator(".org-message").filter({ hasText: "One question for the default model." }));
    await member.getByRole("button", { name: "Choose reply model", exact: true }).click();
    await member.getByLabel("All chat models", { exact: true }).check();
    await member.getByRole("radio", { name: /test\/two/ }).click();
    await member.getByRole("navigation", { name: "Projects and channels" }).getByRole("button", { name: /^alpha topic -Human notes\b/ }).click();
    await member.getByRole("navigation", { name: "Projects and channels" }).getByRole("button", { name: /^beta topic -Shared models\b/ }).click();
    await visible(member.getByRole("button", { name: "Choose reply model", exact: true }).filter({ hasText: "test/two" }));
    const composer = member.getByLabel("Message", { exact: true });
    await composer.fill("Ask the second model."); await composer.press("Shift+Enter");
    assert.equal(await composer.inputValue(), "Ask the second model.\n");
    assert.equal(calls.length, 1);
    await composer.press("Enter");
    await until(() => calls.length === 2, "second selected model call");
    await visible(member.locator(".org-message").filter({ hasText: "Shared mathematics:" }).nth(1));
    await member.getByRole("button", { name: "Choose reply model", exact: true }).click();
    await member.getByLabel("All chat models", { exact: true }).check();
    await member.getByRole("radio", { name: /test\/one/ }).click();
    await send(member, "Return to the first model.");
    await until(() => calls.length === 3, "third selected model call");
    for (const page of pages) {
      await until(async () => (await page.locator(".org-message").filter({ hasText: "Shared mathematics:" }).count()) === 3, "shared assistant history");
      assert.equal(await page.locator(".org-message header strong").filter({ hasText: "LLM · test/" }).count(), 3);
      assert.equal(await page.locator(".org-message .katex").count(), 3);
      await visible(page.locator(".org-message .org-message-usage").filter({ hasText: "0.7 Wh" }).first());
      assert.ok(!(await page.locator(".org-app").innerText()).includes(key));
    }
    assert.equal(calls.length, 3); assert.ok(catalogRequests >= 6);
    const reviewerCatalogReads = [];
    reviewer.on("request", (sent) => { if (/\/model-(prices|benchmarks)$/.test(new URL(sent.url()).pathname)) reviewerCatalogReads.push(new URL(sent.url()).pathname); });
    await reviewer.getByRole("button", { name: "Account menu", exact: true }).click();
    await reviewer.getByRole("button", { name: "Model capabilities", exact: true }).click();
    const prices = reviewer.getByRole("dialog", { name: "Model capabilities" });
    await prices.getByText("Filters", { exact: true }).click();
    await prices.getByLabel("All chat models", { exact: true }).check();
    await visible(prices.locator(".org-model-card").filter({ hasText: "test/one" }));
    await prices.getByLabel("Search", { exact: true }).fill("test/two");
    assert.equal(await prices.locator(".org-model-card").count(), 1);
    assert.match(await prices.locator(".org-model-card").first().innerText(), /\$1/);
    assert.ok(!reviewerCatalogReads.some((item) => item.endsWith("/model-prices")), "opening prices reuses the topic's shared catalogue");
    await prices.getByRole("button", { name: "Refresh catalog", exact: true }).click();
    await until(() => reviewerCatalogReads.filter((item) => item.endsWith("/model-prices")).length === 1, "an explicit refresh fetches the catalogue");
    await visible(prices.locator(".org-model-card").filter({ hasText: "test/two" }));
    await prices.getByRole("button", { name: "Close dialog" }).click();
    assert.equal(calls.length, 3);
    await reviewer.getByRole("button", { name: "Account menu", exact: true }).click();
    await reviewer.getByRole("button", { name: "Usage and energy", exact: true }).click();
    const memberUsage = reviewer.getByRole("dialog", { name: "Usage and energy" });
    await visible(memberUsage.getByRole("row", { name: /^Entire organization/ }));
    await visible(memberUsage.getByRole("row", { name: /^Algebra/ }));
    assert.equal(await memberUsage.getByRole("button", { name: "Save model assumptions" }).count(), 0);
    assert.match(await memberUsage.innerText(), /60/);
    assert.match(await memberUsage.innerText(), /0.0014/);
    await memberUsage.getByText("Coverage and estimation method", { exact: true }).click();
    assert.match(await memberUsage.innerText(), /Energy covers 3\/3 calls, including 1 reference scenarios/);
    await memberUsage.getByRole("button", { name: "Close dialog" }).click();
    await member.getByRole("button", { name: "Choose reply model", exact: true }).click();
    await member.getByRole("radio", { name: /test\/slow/ }).click();
    await send(member, "Start a cancellable response.");
    await visible(member.getByLabel("Streaming response from test/slow"));
    await member.evaluate(id => window.dispatchEvent(new CustomEvent("reasoning-topic-command", { detail: { topicId: id, command: "stop" } })), topic.id);
    await until(() => closedSlow, "provider transport closed on cancel");
    const cancelled = owner.locator(".org-model-run").filter({ hasText: "test/slow" });
    await visible(cancelled.getByText("cancelled", { exact: true }));
    await visible(reviewer.locator(".org-message").filter({ hasText: "Partial response: cancelled" }));
    await cancelled.locator(":scope > summary").click();
    await cancelled.getByText("Reconcile unknown charge", { exact: true }).click();
    await cancelled.getByLabel("Verified provider cost (USD)").fill("0.000009");
    await cancelled.getByLabel("I checked the provider record for this run.").check();
    await cancelled.getByRole("button", { name: "Record verified cost" }).click();
    await visible(cancelled.locator(":scope > summary").getByText("$0.000009", { exact: true }));
    await stop(); await start();
    await reviewer.reload();
    await reviewer.getByRole("navigation", { name: "Projects and channels" }).getByRole("button", { name: /^beta topic -Shared models\b/ }).click();
    await until(async () => (await reviewer.locator(".org-message").filter({ hasText: "Shared mathematics:" }).count()) === 3, "history survives restart");
    assert.equal(calls.length, 4);
    if (!desktopReviewer) {
      await reviewer.context().grantPermissions(["clipboard-read", "clipboard-write"]);
      await reviewer.getByRole("button", { name: "Copy topic JSON", exact: true }).click();
      await visible(reviewer.locator('.org-copy-topic[data-copied="true"]'));
      const exported = JSON.parse(await reviewer.evaluate(() => navigator.clipboard.readText()));
      assert.equal(exported.schema, "reasoning.topic-summary.v2");
      assert.equal(exported.runs.length, 4); assert.equal(exported.provider_calls.length, 4);
      assert.ok(exported.messages.some((message) => message.content.includes("One question for the default model.")));
      assert.equal(exported.continuation_policy.default_output_token_cap, null);
      assert.equal(JSON.stringify(exported).includes(key), false);
    }
    if (!desktopReviewer) {
      for (const width of [920, 390]) {
        await reviewer.setViewportSize({ width, height: 844 });
        if (width === 390) await until(() => reviewer.locator(".org-sidebar").evaluate((element) => element.getBoundingClientRect().right <= 0), "compact navigation finishes closing");
        assert.equal(await reviewer.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
        assert.ok(await reviewer.getByLabel("Message", { exact: true }).isVisible());
      }
    }
    if (process.env.REASONING_SCREENSHOT_DIR) await reviewer.screenshot({ path: path.join(process.env.REASONING_SCREENSHOT_DIR, desktopReviewer ? "public-beta-desktop.png" : "public-beta.png") });
    assert.deepEqual(errors, []);
  } finally {
    cleanupStarted = true;
    await desktop?.close(); await browser?.close(); await stop(); provider.closeAllConnections(); await new Promise((resolve) => provider.close(resolve));
    assert.ok(!logs.includes(password) && !logs.includes(key));
    await rm(temporary, { recursive: true, force: true });
  }
});
