// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
// A member messages their own bot from People. A scripted runner stands in for the bot's computer: it claims the
// request, reports progress, asks for one approval and answers. No model or CLI runs.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { once } from "node:events";
import { mkdtemp, readFile, realpath, rm } from "node:fs/promises";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { chromium } from "playwright";

const repository = fileURLToPath(new URL("../../", import.meta.url));
const visible = locator => locator.waitFor({ state: "visible", timeout: 15000 });

test("a member messages their own bot from People and follows, approves and reacts to its answer", { timeout: 180000 }, async () => {
  const temporary = await realpath(await mkdtemp(path.join(os.tmpdir(), "reasoning-bot-direct-browser-")));
  const state = path.join(temporary, "state"), secret = "Bot direct fixture " + randomUUID();
  const listener = net.createServer(); listener.listen(0, "127.0.0.1"); await once(listener, "listening");
  const port = listener.address().port; await new Promise(resolve => listener.close(resolve));
  const url = `http://127.0.0.1:${port}`;
  const server = spawn(path.join(repository, ".venv/bin/python"), ["-m", "reasoning_ledger.server.cli", "--data-dir", state,
    "serve", "--public-url", url, "--port", String(port), "--frontend-dir", path.join(repository, "web/dist")], { cwd: repository });
  let logs = "";
  server.stdout.on("data", chunk => { logs += chunk; }); server.stderr.on("data", chunk => { logs += chunk; });
  let browser;
  try {
    for (const deadline = Date.now() + 15000; Date.now() < deadline;) {
      try { if ((await fetch(url + "/api/health")).ok) break; } catch { /* Not listening yet. */ }
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    browser = await chromium.launch({ headless: true });
    const errors = [];
    const owner = await (await browser.newContext({ viewport: { width: 1440, height: 920 } })).newPage();
    owner.on("pageerror", error => errors.push(error.message));
    await owner.goto(url);
    await owner.getByLabel("Setup credential").fill(await readFile(path.join(state, "setup-token"), "utf8"));
    await owner.getByLabel("Email", { exact: true }).fill("owner@example.org");
    await owner.getByLabel("Your name").fill("Owner");
    await owner.getByLabel("Password", { exact: true }).fill(secret);
    await owner.getByLabel("Organization name").fill("Mathematics");
    await owner.getByRole("button", { name: "Create organization", exact: true }).click();
    await visible(owner.getByRole("button", { name: "Account menu", exact: true }));

    // A personal bot, connected by a scripted runner that advertises two models, effort levels and local tools.
    const created = await owner.evaluate(async () => {
      const organizations = await (await fetch("/api/organizations", { credentials: "same-origin" })).json();
      const organization = (Array.isArray(organizations) ? organizations : organizations.organizations)[0].id;
      const cookie = document.cookie.split("; ").find(item => /^(__Host-)?reasoning_csrf=/.test(item));
      const response = await fetch(`/api/organizations/${organization}/my-bots`, { method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", ...(cookie ? { "X-CSRF-Token": decodeURIComponent(cookie.split("=")[1]) } : {}) },
        body: JSON.stringify({ name: "Laptop", topic_ids: [], public_access: true, scopes: ["messages:read", "messages:write"], lifetime_seconds: 3600 }) });
      return { organization, bot: await response.json() };
    });
    const runner = async (endpoint, body) => {
      const response = await fetch(url + "/api/bot" + endpoint, { method: "POST", body: JSON.stringify(body),
        headers: { "Content-Type": "application/json", Authorization: "Bearer " + created.bot.token } });
      assert.equal(response.status < 300, true, `${endpoint}: ${response.status}`);
      return response.json();
    };
    await runner("/runtime", { engine: "codex", models: ["gpt", "mini"], local_tools: true, capabilities: { efforts: { gpt: ["low", "high"] } } });

    // Your own bot keeps its row in People and opens a private conversation, like a person.
    const people = owner.getByRole("complementary", { name: "Online people and bots" });
    await people.getByRole("button", { name: "Laptop", exact: true }).click();
    const conversation = owner.getByRole("region", { name: "Direct messages with Laptop" });
    await visible(conversation);
    await visible(owner.getByText("your bot", { exact: true }));

    // Under the box, as in an AI chat: plus, the bot's details, its model and the effort.
    await visible(owner.getByRole("button", { name: "Attach pictures, PDFs or Markdown", exact: true }));
    await visible(owner.getByRole("button", { name: "Bot model: gpt", exact: true }));
    await owner.getByRole("button", { name: "Reasoning effort: Default effort", exact: true }).click();
    await owner.getByRole("radio", { name: "high", exact: true }).click();
    await visible(owner.getByRole("button", { name: "Reasoning effort: High", exact: true }));
    await owner.getByLabel("Message", { exact: true }).fill("Does the census still run after the refactor?");
    await owner.keyboard.press("Enter");
    const mine = conversation.locator(".org-message-group-mine .org-message").filter({ hasText: "Does the census still run" });
    await visible(mine);
    // The working card has Stop in its header; the message box keeps its Send button.
    const stop = conversation.locator(".org-bot-working").getByRole("button", { name: "Stop this bot reply", exact: true });
    await visible(stop);
    assert.equal(await owner.getByRole("button", { name: "Stop current work", exact: true }).count(), 0);
    await visible(owner.getByRole("button", { name: "Send", exact: true }));

    // The runner claims it, works, and asks once to run a command; the request appears in the conversation.
    const claim = await runner("/runs/claim", {});
    assert.equal(claim.prompt, "Does the census still run after the refactor?");
    assert.equal(claim.model, "gpt"); assert.equal(claim.effort, "high");
    await runner(`/runs/${claim.id}/progress`, { lease: claim.lease, phase: "reading", activity: "reading_workspace" });
    await visible(conversation.getByText("Reading workspace…", { exact: true }));
    const approval = await runner(`/runs/${claim.id}/executions`, { lease: claim.lease, command: ["python", "census.py", "--small"] });
    const request = conversation.getByRole("group", { name: "Laptop asks to run a command" });
    await visible(request.getByText('"python" "census.py" "--small"', { exact: true }));
    await request.getByRole("button", { name: "Approve", exact: true }).click();
    for (const stopAt = Date.now() + 10000; Date.now() < stopAt;) {
      if ((await runner(`/runs/${claim.id}/executions/${approval.id}/status`, { lease: claim.lease })).status === "approved") break;
      await new Promise(resolve => setTimeout(resolve, 200));
    }
    await runner(`/runs/${claim.id}/executions/${approval.id}/consume`, { lease: claim.lease });
    await runner(`/runs/${claim.id}/finish`, { lease: claim.lease, status: "succeeded", output: "It runs. The small census finishes in 41 s." });

    // The answer arrives on the left with its steps folded under it; the button returns to ↵.
    const reply = conversation.locator(".org-message").filter({ hasText: "The small census finishes in 41 s." });
    await visible(reply);
    await visible(reply.locator(".org-bot-steps summary"));
    assert.match(await reply.locator(".org-bot-steps").innerText(), /gpt · High/);
    await stop.waitFor({ state: "detached" });
    assert.equal(await reply.evaluate(element => element.closest(".org-message-group-mine") === null), true, "the bot's answer is not mirrored");
    await reply.hover(); await reply.getByRole("button", { name: "Add reaction", exact: true }).click();
    assert.deepEqual(await owner.getByRole("menu", { name: "Add reaction", exact: true }).getByRole("menuitemcheckbox")
      .evaluateAll(items => items.map(item => item.getAttribute("aria-label"))), ["Thumbs up", "Thumbs down", "Upside-down smile"]);
    await owner.keyboard.press("Escape");

    // The title's card describes the bot instead of offering mute and block.
    await owner.getByRole("button", { name: /^Direct messages · Laptop/ }).click();
    const card = owner.getByRole("dialog", { name: "Conversation details" });
    await visible(card.getByRole("group", { name: "Runner" }).getByText("online", { exact: true }));
    assert.equal(await card.getByRole("button", { name: /^Block/ }).count(), 0);
    if (process.env.REASONING_SCREENSHOT_DIR) await owner.screenshot({ path: path.join(process.env.REASONING_SCREENSHOT_DIR, "bot-direct.png") });
    assert.deepEqual(errors, []);
  } catch (error) {
    error.message += "\n" + logs.slice(-4000);
    throw error;
  } finally {
    await browser?.close();
    server.kill(); await once(server, "exit").catch(() => {});
    await rm(temporary, { recursive: true, force: true });
  }
});
