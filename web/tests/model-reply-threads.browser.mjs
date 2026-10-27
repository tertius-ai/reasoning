// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { realpath } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

test("a model's answer replies to its prompt, and a reply to the answer asks a follow-up in the same thread", { timeout: 60000 }, async () => {
  const root = fileURLToPath(new URL("../", import.meta.url));
  const server = await createServer({ root, server: { host: "127.0.0.1", port: 0,
    fs: { allow: [root, await realpath(root + "node_modules")] } } });
  await server.listen(); const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1100, height: 900 } }), errors = [], posts = [], asked = [];
    const user = "a".repeat(32), deven = "d".repeat(32), org = "b".repeat(32), topic = "c".repeat(32);
    const hierarchy = { projects: [{ id: "project", name: "Testing", visibility: "organization" }],
      channels: [{ id: "channel", name: "research", project_id: "project" }],
      topics: [{ id: topic, name: "Regulators", channel_id: "channel", kind: "beta", default_model: "fixture/reasoner", knowledge_tools: [] }], topic_state: [] };
    const model = { id: "fixture/reasoner", name: "Reasoner", supports_tools: true, supports_reasoning: true,
      reasoning_efforts: ["high"], supported_parameters: [], input_modalities: [], output_modalities: [] };
    let sequence = 0;
    const message = (author, content, reply_to = null, extra = {}) => ({ id: (++sequence).toString(16).padStart(32, "0"), sequence,
      topic_id: topic, author_id: author, author_kind: "human", content, client_id: "seed-" + sequence,
      created_at: 1_800_000_000 + sequence * 60, revision: 0, reply_to, ...extra });
    const prompt = message(user, "Survey what is known about higher regulators.");
    const aside = message(deven, "I'll look at the étale side meanwhile.");
    const answer = message(user, "Beilinson's regulator is injective in these cases.", prompt.id,
      { author_kind: "assistant", assistant_model: "fixture/reasoner", inference_run_id: "run-1",
        usage: { prompt_tokens: 128, completion_tokens: 96, actual_microusd: 2100 } });
    const messages = [prompt, aside, answer];
    const thread = id => {
      let top = messages.find(item => item.id === id);
      while (top.reply_to) top = messages.find(item => item.id === top.reply_to);
      const ids = [top.id];
      for (let index = 0; index < ids.length; index++) ids.push(...messages.filter(item => item.reply_to === ids[index]).map(item => item.id));
      return { root_id: top.id, message_id: id, messages: messages.filter(item => ids.includes(item.id)), truncated: false };
    };
    page.on("pageerror", error => errors.push(error.message));
    await page.route("**/api/**", async route => {
      const request = route.request(), path = new URL(request.url()).pathname, parts = path.split("/"), body = request.postDataJSON();
      let data = [], status = 200;
      if (path === "/api/health") data = { initialized: true };
      else if (path === "/api/auth/me") data = { id: user, name: "Elden Ring", email: "elden@example.org" };
      else if (path === "/api/organizations") data = [{ id: org, name: "Testing", role: "owner" }];
      else if (path === "/api/auth/model-favorites") data = { user_id: user, models: [] };
      else if (path.endsWith("/hierarchy")) data = hierarchy;
      else if (path.endsWith("/members")) data = [{ id: user, name: "Elden Ring", role: "owner" }, { id: deven, name: "Deven Moss", role: "member" }];
      else if (path.endsWith("/presence")) data = { humans: [], bots: [] };
      else if (path.endsWith("/inference-configuration")) data = { allowed_models: [model.id], tools: [] };
      else if (path.endsWith("/model-prices")) data = { models: [model], allowed_models: [model.id] };
      else if (path.endsWith("/research-instructions")) data = { system_prompt: "Research safely.", prompt_version: "fixture" };
      else if (path.endsWith("/research-settings")) data = { tools: ["history"], web_search_configured: false, current_date: "2026-10-26" };
      else if (path.endsWith("/compute-configuration")) data = { images: [], policy: { allowed_images: [] } };
      else if (path.endsWith("/navigation-usage")) data = { projects: {}, channels: {}, topics: {} };
      else if (parts[6] === "messages" && parts[8] === "thread") data = thread(parts[7]);
      else if (path.endsWith("/messages") && request.method() === "POST") {
        posts.push(body); data = message(user, body.content, body.reply_to ?? null, { client_id: body.client_id }); messages.push(data); status = 201;
      } else if (path.endsWith("/messages")) data = messages;
      else if (path.endsWith("/message-updates")) data = { after: 0, messages: [] };
      else if (path.includes("/reactions/")) data = { counts: { up: 0, down: 0, upside_down: 0 }, mine: [] };
      else if (path.endsWith("/inference-runs") && request.method() === "POST") { asked.push(body); data = []; }
      else if (path.endsWith("/inference-runs")) data = [];
      else if (path.endsWith("/bot-runs")) data = [];
      else if (path.endsWith("/agents")) data = [];
      else if (path.endsWith("/goal")) data = null;
      await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(data) });
    });
    await page.goto(server.resolvedUrls.local[0] + "organization.html");
    const article = id => page.locator(`article[data-message-id="${id}"]`);
    await article(answer.id).waitFor();

    // The answer points at its prompt, even with a teammate's message between them, and the prompt counts the reply.
    assert.match(await article(answer.id).locator(".org-reply-pointer").innerText(), /Elden\s+Survey what is known about higher regulators\./);
    assert.equal(await article(prompt.id).locator(".org-thread-count").textContent(), "↩1 reply");
    assert.equal(await article(aside.id).locator(".org-thread-count").count(), 0);

    // Replying to the answer sends a follow-up in the thread and asks the model to answer it.
    await article(answer.id).hover();
    await article(answer.id).getByRole("button", { name: "Reply", exact: true }).click();
    const composer = page.getByLabel("Message", { exact: true });
    await composer.fill("Does that cover the p-adic case?");
    await composer.press("Enter");
    const follow = () => messages.find(item => item.content === "Does that cover the p-adic case?");
    for (let wait = 0; wait < 50 && !asked.length; wait++) await page.waitForTimeout(100);
    assert.equal(posts.at(-1).reply_to, answer.id);
    assert.equal(asked.length, 1, "the follow-up asks the model");
    assert.equal(asked[0].through_sequence, follow().sequence, "the model answers the follow-up");
    await article(prompt.id).locator(".org-thread-count").filter({ hasText: /^↩2 replies$/ }).waitFor();
    assert.match(await article(follow().id).locator(".org-reply-pointer").innerText(), /Beilinson's regulator/);

    // A reply written in the thread asks the model too.
    await article(prompt.id).locator(".org-thread-count").click();
    const dialog = page.getByRole("dialog", { name: "Thread", exact: true });
    await dialog.getByText("3 messages", { exact: true }).waitFor();
    await dialog.getByLabel("Reply in thread", { exact: true }).fill("And for K3 surfaces?");
    await dialog.getByLabel("Reply in thread", { exact: true }).press("Enter");
    await dialog.getByText("4 messages", { exact: true }).waitFor();
    for (let wait = 0; wait < 50 && asked.length < 2; wait++) await page.waitForTimeout(100);
    assert.equal(asked.length, 2);
    assert.equal(asked[1].through_sequence, messages.at(-1).sequence);
    assert.deepEqual(errors, []);
  } finally {
    await browser.close(); await server.close();
  }
});
