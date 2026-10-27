// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { realpath } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

test("progress follows its human message, retained failures stay inspectable, and playback is compact", { timeout: 60000 }, async () => {
  const root = fileURLToPath(new URL("../", import.meta.url));
  const server = await createServer({ root, server: { host: "127.0.0.1", port: 0,
    fs: { allow: [root, await realpath(root + "node_modules")] } } });
  await server.listen(); const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage(), errors = [], writes = [];
    const user = "a".repeat(32), org = "b".repeat(32), topic = "c".repeat(32), bot = "d".repeat(32);
    const hierarchy = { projects: [{ id: "project", name: "Testing", visibility: "organization" }],
      channels: [{ id: "channel", name: "research", project_id: "project" }],
      topics: [{ id: topic, name: "Progress", channel_id: "channel", kind: "beta", default_model: "fixture/reasoner", knowledge_tools: [] }], topic_state: [] };
    const model = { id: "fixture/reasoner", name: "Reasoner", supports_tools: true, supports_reasoning: true,
      reasoning_efforts: ["high", "xhigh"], supported_parameters: [], input_modalities: [], output_modalities: [] };
    const base = { requested_by: user, model: model.id, context_through_sequence: 1, created_at: 1_800_000_000,
      cancel_requested: 0, message_id: null, output_text: "", accounting_state: "settled", actual_microusd: 0, reserved_microusd: 0 };
    let threads = [{ root_run: "failed", requested_by: user, status: "paused", reason: "agent_response_failed", tasks: 0 }];
    page.on("pageerror", error => errors.push(error.message));
    await page.route("**/api/**", async route => {
      const request = route.request(), path = new URL(request.url()).pathname;
      if (request.method() !== "GET") writes.push({ path, body: request.postDataJSON() });
      let data = [];
      if (path === "/api/health") data = { initialized: true };
      else if (path === "/api/auth/me") data = { id: user, name: "Alice", email: "alice@example.org" };
      else if (path === "/api/organizations") data = [{ id: org, name: "Testing", role: "owner" }];
      else if (path === "/api/auth/model-favorites") data = { user_id: user, models: [] };
      else if (path.endsWith("/hierarchy")) data = hierarchy;
      else if (path.endsWith("/members")) data = [{ id: user, name: "Alice", role: "owner" }];
      else if (path.endsWith("/presence")) data = { humans: [], bots: [] };
      else if (path.endsWith("/inference-configuration")) data = { allowed_models: [model.id], tools: [] };
      else if (path.endsWith("/model-prices")) data = { models: [model], allowed_models: [model.id] };
      else if (path.endsWith("/research-instructions")) data = { system_prompt: "Research safely.", prompt_version: "fixture" };
      else if (path.endsWith("/research-settings")) data = { tools: ["history", "web", "arxiv"],
        web_search_configured: false, current_date: "2026-10-23" };
      else if (path.endsWith("/compute-configuration")) data = { images: [], policy: { allowed_images: [] } };
      else if (path.endsWith("/navigation-usage")) data = { projects: {}, channels: {}, topics: {} };
      else if (path.endsWith("/messages")) data = [{ id: "human", sequence: 1, created_at: base.created_at,
        content: "Explain the result.", author_kind: "human", author_id: user, topic_id: topic }];
      else if (path.endsWith("/message-updates")) data = { after: 0, messages: [] };
      else if (path.includes("/reactions/")) data = { counts: { up: 0, down: 0, upside_down: 0 }, mine: [] };
      else if (path.endsWith("/inference-runs")) data = [
        { ...base, id: "working", status: "running", activity: [{ sequence: 1, phase: "round", round: 1 }] },
        { ...base, id: "failed", status: "failed", error_code: "provider_credit",
          diagnostics: { message: "Check provider credit.", action: "Inspect the provider account.", http_status: 402 } },
        { ...base, id: "waiting-review", status: "succeeded", answer_state: { status: "waiting", reason: "independent review",
          checkpoint: { summary: "The finite case is settled.", remaining: "Check the infinite case.", sources: ["Lemma 2"] } } },
        { ...base, id: "continued-coordinator", status: "succeeded", agent: { role: "coordinator", root_run: "continued-coordinator" },
          answer_state: { status: "continued", reason: "delegated work", next_run: "handoff", checkpoint: null } },
      ];
      else if (path.endsWith("/bot-runs")) data = [{ id: "working", bot_id: bot, model: "default", status: "running", phase: "thinking",
        created_at: base.created_at, context_through_sequence: 1, requested_by: user, activity: [] }];
      else if (path.endsWith("/agents")) data = threads;
      else if (path.endsWith("/agents/failed/control")) { threads = [{ ...threads[0], status: request.postDataJSON().status }]; data = threads[0]; }
      else if (path.endsWith("/goal")) data = null;
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(data) });
    });
    await page.goto(server.resolvedUrls.local[0] + "organization.html");
    await page.getByLabel("Research progress", { exact: true }).waitFor();
    await page.locator('[data-activity-id="bot:working"]').waitFor();
    const ids = await page.locator("[data-activity-id]").evaluateAll(nodes => nodes.map(node => node.dataset.activityId));
    assert.equal(ids[0], "message:human");
    assert.ok(ids.indexOf("run:working") > 0); assert.ok(ids.indexOf("bot:working") > 0);
    assert.equal(await page.locator(".org-agent-controls").count(), 0);
    assert.equal(await page.getByText("Agent · paused", { exact: true }).count(), 0);
    const failed = page.locator('[data-run-id="failed"]');
    await failed.locator("summary").click(); await failed.getByText("Check provider credit.", { exact: true }).waitFor();
    await failed.getByText("Inspect the provider account.", { exact: true }).waitFor();
    // The ⓘ beside the model under the message box opens the selected model's capabilities.
    await page.getByRole("button", { name: "Model details", exact: true }).click();
    const capabilities = page.getByRole("group", { name: "Effective model capabilities", exact: true });
    await capabilities.filter({ hasText: "Research · 2 of 3 readers" }).waitFor();
    assert.match(await capabilities.textContent(), /Public source reader\s*on.*General web search\s*off.*2026-10-23 UTC/);
    await page.keyboard.press("Escape");
    await capabilities.waitFor({ state: "detached", timeout: 5000 });
    const waiting = page.locator('[data-run-id="waiting-review"]');
    await waiting.getByText("Waiting · answer incomplete", { exact: true }).waitFor();
    await waiting.locator("summary").first().click();
    await waiting.getByText("The finite case is settled.", { exact: true }).waitFor();
    await waiting.getByText("Remaining: Check the infinite case.", { exact: true }).waitFor();
    // A coordinator that waited for delegates stops saying so once a later reply continued its work.
    const continued = page.locator('[data-run-id="continued-coordinator"]');
    await continued.getByText("Continued in a later reply", { exact: true }).waitFor();
    assert.equal(await continued.getByText("Waiting · answer incomplete", { exact: true }).count(), 0);
    await continued.locator("summary").first().click();
    await continued.getByText("Waited for delegated work. A later reply continued this work and carries the answer.", { exact: true }).waitFor();
    // The box has one button at its right, and it never stops: the running reply has Stop in its own card header,
    // so beside a paused request the box's button resumes.
    const resume = page.getByRole("button", { name: "Start or resume", exact: true });
    await resume.waitFor({ timeout: 5000 });
    assert.equal(await page.getByRole("button", { name: "Stop current work", exact: true }).count(), 0);
    await page.locator('[data-run-id="working"] summary').getByRole("button", { name: "Stop this reply", exact: true }).waitFor();
    const box = await page.locator(".org-composer-box").boundingBox(), button = await resume.boundingBox();
    assert.equal(button.width, 28); assert.equal(button.height, 28);
    assert.ok(button.x + button.width <= box.x + box.width && button.x + button.width >= box.x + box.width - 12, "the button sits at the box's right edge");
    // Under the box: the 28px plus, then the ⓘ and the model at the ⓘ square's 24px height, all centred on one line.
    const row = await Promise.all(["Attach picture or PDF", "Model details", "Choose reply model"]
      .map(name => page.getByRole("button", { name, exact: true }).boundingBox()));
    assert.deepEqual(row.map(item => item.height), [28, 24, 24]);
    for (const item of row) { assert.equal(item.y + item.height / 2, row[0].y + row[0].height / 2); assert.ok(item.y >= box.y + box.height); }
    assert.ok(row[1].x < row[2].x, "the ⓘ is left of the model");
    // The paused request still resumes with /play.
    const message = page.getByLabel("Message", { exact: true });
    await message.fill("/play"); await message.press("Enter");
    const resumed = () => writes.some(write => write.path.endsWith("/agents/failed/control") && write.body.status === "active");
    for (let wait = 0; wait < 50 && !resumed(); wait++) await page.waitForTimeout(100);
    assert.ok(resumed(), "/play resumes the paused request");
    await message.fill("A follow-up");
    // With text in the box its button sends, while the running reply keeps Stop in its card.
    await page.getByRole("button", { name: "Send", exact: true }).waitFor({ timeout: 5000 });
    assert.equal(await page.locator('[data-run-id="working"] summary').getByRole("button", { name: "Stop this reply" }).count(), 1);
    assert.equal(await page.locator(".org-composer-send button:visible").count(), 1, "one button in the box");
    assert.deepEqual(errors, []);
  } finally { await browser.close(); await server.close(); }
});
