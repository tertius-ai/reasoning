// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { realpath } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

test("beta creation selects a bot model then effort, persists the choice, and favorites follow the person", { timeout: 60000 }, async () => {
  const root = fileURLToPath(new URL("../", import.meta.url));
  const server = await createServer({ root, server: { host: "127.0.0.1", port: 0,
    fs: { allow: [root, await realpath(root + "node_modules")] } } });
  await server.listen();
  const browser = await chromium.launch({ headless: true });
  try {
    const org = "c".repeat(32), user = "a".repeat(32), bot = "b".repeat(32), topic = "d".repeat(32);
    const model = `bot/${bot}/example`;
    const errors = [], writes = [], favoriteSets = new Map();
    let holdConfiguration = false, failConfiguration = false, releaseConfiguration;
    const configurationGate = new Promise(resolve => { releaseConfiguration = resolve; });
    const hierarchy = { projects: [{ id: "project", name: "Project", visibility: "organization" }],
      channels: [{ id: "channel", name: "research", project_id: "project" }],
      topics: [{ id: topic, name: "Existing", channel_id: "channel", kind: "beta", default_model: model, knowledge_tools: [] }], topic_state: [] };
    const makePage = async principal => {
      const page = await browser.newPage(); page.on("pageerror", error => errors.push(error.message));
      await page.route("**/api/**", async route => {
        const request = route.request(), path = new URL(request.url()).pathname, body = request.postDataJSON();
        if (holdConfiguration && path.endsWith("/inference-configuration")) await configurationGate;
        if (failConfiguration && path.endsWith("/inference-configuration")) {
          await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "Provider catalog temporarily unavailable" }) }); return;
        }
        if (request.method() !== "GET") writes.push({ path, body });
        let data = [];
        if (path === "/api/health") data = { initialized: true };
        else if (path === "/api/auth/me") data = { id: principal, name: "Alice", email: "alice@example.org" };
        else if (path === "/api/auth/model-favorites") {
          let values = favoriteSets.get(principal) ?? [];
          if (request.method() === "PUT") { values = body.favorite ? [...new Set([...values, body.model_id])] : values.filter(id => id !== body.model_id); favoriteSets.set(principal, values); }
          data = { user_id: principal, models: values };
        }
        else if (path === "/api/organizations") data = [{ id: org, name: "Research", role: "owner" }];
        else if (path.endsWith("/hierarchy")) data = hierarchy;
        else if (path.endsWith("/members")) data = [{ id: principal, name: "Alice", role: "owner" }];
        else if (path.endsWith("/presence")) data = { humans: [], bots: [] };
        else if (path.endsWith("/bots")) data = [{ id: bot, name: "My Codex" }];
        else if (path.endsWith("/bot-models")) data = [{ id: model, bot_id: bot, name: "My Codex · example", engine: "codex", owner_user_id: principal, efforts: ["high", "xhigh"], online: true }];
        else if (path.endsWith("/inference-configuration")) data = { allowed_models: ["fixture/openrouter"], tools: [] };
        else if (path.endsWith("/model-prices")) data = { models: [], allowed_models: [] };
        else if (path.endsWith("/research-instructions")) data = { system_prompt: "Research safely.", prompt_version: "fixture" };
        else if (path.endsWith("/compute-configuration")) data = { images: [], policy: { allowed_images: [] } };
        else if (path.endsWith("/navigation-usage")) data = { projects: {}, channels: {}, topics: {} };
        else if (path.endsWith("/message-updates")) data = { after: 0, messages: [] };
        else if (path.endsWith("/goal")) data = null;
        else if (path.endsWith("/topics") && request.method() === "POST") {
          data = { ...body, id: "e".repeat(32), knowledge_tools: body.knowledge_tools ?? [] }; hierarchy.topics.push(data);
        }
        await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(data) });
      });
      await page.goto(server.resolvedUrls.local[0] + "organization.html");
      return page;
    };
    const page = await makePage(user);
    holdConfiguration = true;
    await page.getByRole("button", { name: "New topic in #research", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "New topic" });
    await dialog.getByLabel("Topic name", { exact: true }).fill("Bot-led work");
    await dialog.getByRole("radio", { name: /AI chat/ }).check();
    await dialog.getByRole("button", { name: "Model", exact: true }).click();
    await dialog.getByRole("radio", { name: model, exact: true }).click();
    const efforts = dialog.getByRole("radiogroup", { name: "Reasoning effort", exact: true });
    await efforts.getByRole("radio", { name: "Default effort", exact: true }).waitFor();
    assert.equal(await efforts.getByRole("radio", { name: "Default effort", exact: true }).evaluate(node => document.activeElement === node), true);
    assert.ok((await dialog.locator(".org-model-options").boundingBox()).width <= 320);
    await efforts.getByRole("radio", { name: "xhigh", exact: true }).click();
    // A slower OpenRouter configuration response must not reset a chosen bot route.
    releaseConfiguration(); holdConfiguration = false;
    await dialog.getByRole("button", { name: "Model", exact: true }).filter({ hasText: "My Codex · example/xhigh" }).waitFor();
    await dialog.getByRole("button", { name: "Create topic", exact: true }).click();
    await page.waitForFunction(() => !document.querySelector('[role="dialog"]'));
    const creation = writes.find(write => write.path.endsWith("/topics"));
    assert.equal(creation.body.default_model, model); assert.equal(creation.body.default_effort, "xhigh");
    const picker = page.getByRole("button", { name: "Choose reply model", exact: true });
    await picker.filter({ hasText: "My Codex · example" }).waitFor();
    // The effort is its own picker beside the model. Its choices share native radio keyboard behavior and restore focus to it.
    await page.getByRole("button", { name: "Reasoning effort: Extra high", exact: true }).click();
    const selectedEffort = page.getByRole("radio", { name: "xhigh", exact: true });
    await selectedEffort.waitFor(); assert.equal(await selectedEffort.evaluate(node => document.activeElement === node), true);
    await selectedEffort.press("ArrowUp");
    const effort = page.getByRole("button", { name: "Reasoning effort: High", exact: true });
    await effort.waitFor();
    assert.equal(await effort.evaluate(node => document.activeElement === node), true);
    await picker.click(); await page.getByRole("button", { name: `Favorite ${model}`, exact: true }).click();
    await page.getByRole("button", { name: `Favorite ${model}`, exact: true }).filter({ hasText: "★" }).waitFor();
    // Bot routes and their effort must be usable independently of provider catalogue availability.
    failConfiguration = true;
    const secondDevice = await makePage(user);
    await secondDevice.getByRole("button", { name: "Choose reply model", exact: true }).click();
    await secondDevice.getByRole("button", { name: `Favorite ${model}`, exact: true }).filter({ hasText: "★" }).waitFor();
    const anotherPerson = await makePage("f".repeat(32));
    await anotherPerson.getByRole("button", { name: "Choose reply model", exact: true }).click();
    await anotherPerson.getByRole("button", { name: `Favorite ${model}`, exact: true }).filter({ hasText: "☆" }).waitFor();
    assert.deepEqual(errors, []);
  } finally { await browser.close(); await server.close(); }
});
