// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { realpath } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

test("existing bot access is editable despite a retained pairing URL and model mentions autocomplete", { timeout: 60000 }, async () => {
  const root = fileURLToPath(new URL("../", import.meta.url));
  const server = await createServer({ root, server: { host: "127.0.0.1", port: 0, fs: { allow: [root, await realpath(root + "node_modules")] } } });
  await server.listen(); const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage(), errors = [], writes = [];
    let holdCatalog = false, releaseCatalog;
    const catalogGate = new Promise(resolve => { releaseCatalog = resolve; });
    page.on("pageerror", error => errors.push(error.message));
    const id = "b".repeat(32), user = "a".repeat(32), org = "c".repeat(32), topic = "d".repeat(32), colleague = "e".repeat(32);
    let bot = { id, name: "Math Bot", created_by: user, sharing: { policy: "owner", members: [] }, scopes: ["messages:read", "messages:write", "knowledge:read"], topic_ids: [topic], explicit_topic_ids: [], public_access: true, expires_at: 1_900_000_000, revoked_at: null, enabled: true,
      runtime: { engine: "codex", models: ["default", "test-model"], local_tools: true, online: true }, developer: { enabled: false, repositories: [], requesters: [] } };
    const hierarchy = { projects: [{ id: "project", name: "Project", visibility: "organization" }], channels: [{ id: "channel", name: "channel", project_id: "project" }], topics: [{ id: topic, name: "Research", channel_id: "channel", kind: "beta", default_model: "fixture/model", knowledge_tools: [] }], topic_state: [] };
    await page.route("**/api/**", async route => {
      const request = route.request(), path = new URL(request.url()).pathname;
      if (holdCatalog && (path.endsWith("/bots") || path.endsWith("/bot-models"))) await catalogGate;
      let data = [];
      if (request.method() !== "GET") writes.push({ path, body: request.postDataJSON() });
      if (path === "/api/health") data = { initialized: true };
      else if (path === "/api/auth/me") data = { id: user, email: "a@example.org", name: "Alice" };
      else if (path === "/api/organizations") data = [{ id: org, name: "Research", role: "owner" }];
      else if (path.endsWith("/hierarchy")) data = hierarchy;
      else if (path.endsWith("/members")) data = [{ id: user, name: "Alice", role: "owner" }, { id: colleague, name: "Bob", role: "member" }];
      else if (path.endsWith("/presence")) data = { humans: [], bots: [] };
      else if (path.includes("/bot/connect/")) data = { name: "Old pairing", engine: "codex" };
      else if (path.endsWith("/my-bots")) data = [bot];
      else if (path.endsWith("/my-bots/" + id)) { bot = { ...bot, ...request.postDataJSON() }; data = bot; }
      else if (path.endsWith("/my-bots/" + id + "/sharing")) { bot = { ...bot, sharing: request.postDataJSON() }; data = bot.sharing; }
      else if (path.endsWith("/my-bots/" + id + "/developer")) { bot = { ...bot, developer: request.postDataJSON() }; data = bot.developer; }
      else if (path.endsWith("/bots")) data = [{ id, name: "Math Bot" }];
      else if (path.endsWith("/bot-models")) data = ["default", "test-model"].map(model => ({ id: `bot/${id}/${model}`, bot_id: id, name: "Math Bot · " + model, engine: "codex", owner_user_id: user, online: true }));
      else if (path.endsWith("/inference-configuration")) data = { allowed_models: ["fixture/model"], tools: [], max_output_tokens: 8192 };
      else if (path.endsWith("/research-instructions")) data = { system_prompt: "Research safely.", prompt_version: "fixture" };
      else if (path.endsWith("/goal")) data = null;
      else if (path.endsWith("/compute-configuration")) data = { images: [], policy: { allowed_images: [] } };
      else if (path.includes("/reactions/")) data = { counts: { up: 0, down: 0, upside_down: 0 }, mine: [] };
      else if (path.endsWith("/model-prices")) data = { models: [], observations: [] };
      else if (path.endsWith("/message-updates")) data = { after: 0, messages: [] };
      else if (path.endsWith("/messages") && request.method() === "POST") data = { ...request.postDataJSON(), id: "message", sequence: 1, author_id: user, author_kind: "human", topic_id: topic, created_at: 1_800_000_000 };
      else if (path.endsWith("/navigation-usage")) data = { projects: {}, channels: {}, topics: {} };
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(data) });
    });
    await page.goto(server.resolvedUrls.local[0] + "organization.html?bot_connect=ABCDEFGHIJKL");
    const card = page.locator(".org-bot-card");
    await card.getByText("Math Bot", { exact: true }).waitFor();
    assert.equal(await card.locator("strong").getAttribute("title"), "Codex · default · test-model");
    assert.equal(await card.getByText("Research", { exact: true }).count(), 0, "effective topic lists do not clutter bot cards");
    await card.getByText("Local execution · approval required", { exact: true }).waitFor();
    await page.getByRole("button", { name: "Edit access", exact: true }).click();
    await page.getByRole("button", { name: "Save access", exact: true }).waitFor();
    const name = page.getByLabel("Bot name", { exact: true });
    assert.equal(await name.inputValue(), "Math Bot"); assert.equal(await name.getAttribute("readonly"), null);
    await name.fill("Renamed Bot"); await page.getByRole("button", { name: "Save access", exact: true }).click();
    await page.getByRole("status").filter({ hasText: "Bot access saved" }).waitFor();
    assert.equal(writes.filter(write => write.path.endsWith("/my-bots/" + id)).length, 1);
    assert.equal(writes.filter(write => write.path.includes("/connect/")).length, 0);
    await page.getByText("⬡ Developer tools · off", { exact: true }).click();
    await page.getByLabel("Enable developer tools for Renamed Bot").check();
    await page.getByLabel("Allowed GitHub repositories").fill("research/bench");
    await page.getByRole("button", { name: "Save developer access", exact: true }).click();
    await page.getByText("Developer access saved.", { exact: true }).waitFor();
    assert.ok(writes.some(write => write.path.endsWith("/developer") && write.body.enabled && write.body.repositories[0] === "research/bench"));
    await card.getByLabel("Developer bot", { exact: true }).waitFor();
    await card.getByText("Used only by you", { exact: true }).waitFor();
    const sharing = card.locator(".org-bot-sharing");
    await sharing.getByText("Who can use this bot · Only me", { exact: true }).click();
    const remark = sharing.getByRole("note").filter({ hasText: "Reasoning does not check how the bot authenticates." });
    assert.equal(await remark.count(), 0, "keeping a bot to its owner needs no sharing remark");
    await sharing.getByLabel("Specific members", { exact: true }).check();
    await remark.waitFor();
    assert.match(await remark.innerText(), /You are responsible for making sure you may share this bot/);
    assert.equal(await sharing.getByLabel("Alice", { exact: true }).count(), 0, "the owner is always included");
    assert.ok(await sharing.getByRole("button", { name: "Save who can use this bot" }).isDisabled());
    await sharing.getByLabel("Bob", { exact: true }).check();
    await sharing.getByRole("button", { name: "Save who can use this bot" }).click();
    await sharing.getByText("Who can use this bot saved.", { exact: true }).waitFor();
    assert.deepEqual(writes.find(write => write.path.endsWith("/sharing")).body, { policy: "members", members: [colleague] });
    await card.getByText("Used by you and 1 member", { exact: true }).waitFor();
    await sharing.getByLabel("Everyone who can write in its topics", { exact: true }).check();
    await remark.waitFor();
    await sharing.getByLabel("Only me", { exact: true }).check();
    assert.equal(await remark.count(), 0);
    holdCatalog = true;
    await page.goto(server.resolvedUrls.local[0] + "organization.html");
    const composer = page.getByLabel("Message", { exact: true }); await composer.fill("@Math Bot-");
    assert.equal(await page.getByRole("option", { name: "Math Bot-test-model", exact: true }).count(), 0,
      "typing before metadata arrives must not assume a bot identity or model");
    releaseCatalog();
    await page.getByRole("option", { name: "Math Bot-test-model", exact: true }).click();
    assert.equal(await composer.inputValue(), "@Math Bot-test-model ");
    await page.waitForFunction(() => document.querySelector("textarea#org-message")?.selectionEnd === document.querySelector("textarea#org-message")?.value.length);
    await composer.type("Explain"); await composer.press("Enter");
    await page.waitForTimeout(300);
    assert.ok(writes.some(write => write.body?.content === `@[${id}:test-model] Explain`), JSON.stringify(writes));
    assert.equal(writes.filter(write => write.path.endsWith("/inference-runs")).length, 0);
    assert.deepEqual(errors, []);
  } finally { await browser.close(); await server.close(); }
});
