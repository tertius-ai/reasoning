// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { after, before, test } from "node:test";
import { chromium } from "playwright";
import { build, preview } from "vite";

// A catalog the size of OpenRouter's public one: 451 models, 328 of which advertise tools or reasoning and so are
// listed by default.
const providers = ["openai", "anthropic", "google", "meta-llama", "mistralai", "qwen", "deepseek", "x-ai", "cohere", "nvidia"];
const models = Array.from({ length: 451 }, (_, index) => {
  const provider = providers[index % providers.length], useful = index % 11 < 8, reasoning = useful && index % 2 === 0;
  return { id: `${provider}/model-${String(index).padStart(3, "0")}`, name: `${provider}: Model ${index}`, provider,
    description: `Model ${index} handles long-context reasoning and tool use.`, modality: "text->text",
    input_modalities: index % 3 ? ["text"] : ["text", "image"], output_modalities: ["text"], supported_parameters: ["max_tokens"],
    supports_tools: useful, supports_reasoning: reasoning, reasoning_efforts: reasoning ? ["low", "medium", "high"] : [],
    input_usd_per_million: 0.1 + index % 17, output_usd_per_million: 0.4 + index % 23, request_usd: null,
    cache_read_usd_per_million: null, cache_write_usd_per_million: null, reasoning_usd_per_million: null, image_usd: null,
    web_search_usd: null, context_length: 32000 + index * 1000, max_completion_tokens: index === 0 ? 943718 : 16384,
    weights_repository: index % 7 === 0 ? `${provider}/weights-${index}` : null };
});
const ids = models.map(model => model.id), useful = models.filter(model => model.supports_tools || model.supports_reasoning).map(model => model.id);
const user = "a".repeat(32), org = "b".repeat(32), topic = "c".repeat(32);
const transcript = Array.from({ length: 13 }, (_, index) => ({ id: `message-${index}`, sequence: index + 1, topic_id: topic,
  created_at: 1_800_000_000 + index, author_kind: index % 2 ? "assistant" : "human", author_id: user, assistant_model: ids[0],
  client_id: "fixture-" + index, content: `Step ${index}: $$\\sum_{k=1}^{n} k^${index} = \\int_0^1 x^${index}\\,dx$$.` }));

let server, browser, outDir;
before(async () => {
  const root = fileURLToPath(new URL("../", import.meta.url));
  outDir = await mkdtemp(join(tmpdir(), "reasoning-model-picker-"));
  await build({ root, logLevel: "silent", build: { outDir, emptyOutDir: true } });
  server = await preview({ root, logLevel: "silent", build: { outDir }, preview: { host: "127.0.0.1", port: 0 } });
  browser = await chromium.launch({ headless: true });
});
after(async () => {
  await browser?.close(); await server?.close();
  if (outDir) await rm(outDir, { recursive: true, force: true });
});

/** A production build of a beta topic against a mocked API serving the large catalog. */
async function open() {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } }), errors = [], favorites = new Set([ids[1]]), writes = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.route("**/api/**", async route => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname;
    let data = [];
    if (path === "/api/health") data = { initialized: true };
    else if (path === "/api/auth/me") data = { id: user, name: "Alice", email: "alice@example.org" };
    else if (path === "/api/auth/model-favorites") {
      if (request.method() === "PUT") { const body = request.postDataJSON(); writes.push(body);
        if (body.favorite) favorites.add(body.model_id); else favorites.delete(body.model_id); }
      data = { user_id: user, models: [...favorites] };
    }
    else if (path === "/api/organizations") data = [{ id: org, name: "Testing", role: "owner" }];
    else if (path.endsWith("/hierarchy")) data = { projects: [{ id: "project", name: "Testing", visibility: "organization" }],
      channels: [{ id: "channel", name: "research", project_id: "project" }],
      topics: [{ id: topic, name: "Algebra", channel_id: "channel", kind: "beta", default_model: ids[0], knowledge_tools: [] }], topic_state: [] };
    else if (path.endsWith("/members")) data = [{ id: user, name: "Alice", role: "owner" }];
    else if (path.endsWith("/presence")) data = { humans: [], bots: [] };
    else if (path.endsWith("/inference-configuration")) data = { allowed_models: ids, tools: [], routing: { strategy: "recommended", zdr: false, region: "global" } };
    else if (path.endsWith("/model-prices")) data = { models, allowed_models: ids, fetched_at: 1_800_000_000, routing: "recommended", source: "fixture",
      recommendations: { version: "fixture", reviewed_at: "2026-10-01", source: "fixture", method: "fixture", organization_override: false,
        model_ids: [ids[44]], evidence: [] } };
    else if (path.endsWith("/model-benchmarks")) data = { models: {}, status: "unavailable", meta: null };
    else if (path.endsWith("/research-instructions")) data = { system_prompt: "Research safely.", prompt_version: "fixture" };
    else if (path.endsWith("/research-settings")) data = { tools: [] };
    else if (path.endsWith("/navigation-usage")) data = { projects: {}, channels: {}, topics: {} };
    else if (path.endsWith("/messages")) data = url.searchParams.has("after") ? [] : transcript;
    else if (path.endsWith("/message-updates")) data = { after: 0, messages: [] };
    else if (path.endsWith("/reactions/messages")) data = Object.fromEntries(transcript.map(message =>
      [message.id, { counts: { up: 0, down: 0, upside_down: 0 }, mine: [] }]));
    else if (path.endsWith("/goal") || path.endsWith("/read")) data = null;
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(data) });
  });
  await page.goto(server.resolvedUrls.local[0] + "organization.html");
  await page.locator("#message-13").waitFor();
  return { page, errors, writes };
}
const rowCount = page => page.locator(".org-model-card").count();
/** Rows mount in batches after opening; wait for the complete list. */
const rowsReach = (page, count) => page.waitForFunction(count => document.querySelectorAll(".org-model-card").length === count, count);

test("a closed reply picker mounts no model rows; opening it lists, searches, favorites and selects", { timeout: 90000 }, async () => {
  const { page, errors, writes } = await open();
  const picker = page.getByRole("button", { name: "Choose reply model", exact: true });
  await picker.filter({ hasText: ids[0] }).waitFor();
  assert.equal(await rowCount(page), 0); assert.equal(await page.locator(".org-model-options").count(), 0);
  await page.locator("#org-message").pressSequentially("Typing leaves the closed picker empty.");
  assert.equal(await rowCount(page), 0);

  await picker.click();
  assert.equal(await picker.getAttribute("aria-expanded"), "true");
  await rowsReach(page, useful.length);
  const options = page.locator(".org-model-options");
  assert.deepEqual(await options.locator("h5").allTextContents(), ["Favorites", "Best for math", "Best open-weights", "Other models"]);
  assert.equal(await options.locator("section").first().getByRole("radio").getAttribute("aria-label"), ids[1]);
  assert.equal(await page.getByRole("radio", { name: ids[0], exact: true }).isChecked(), true);
  assert.equal(new Set(await options.getByRole("radio").evaluateAll(radios => radios.map(radio => radio.name))).size, 1, "one native radio group");
  await options.getByText("No application output-token cap", { exact: false }).waitFor();
  assert.equal(await options.getByText("943,718 max output").count(), 0);

  const search = page.getByRole("searchbox", { name: "Search models" });
  await search.fill("model-12");
  const matching = useful.filter(id => id.includes("model-12"));
  await rowsReach(page, matching.length);
  await search.fill("no such model");
  await options.getByText("No matching models.", { exact: true }).waitFor();
  await search.fill("");
  await rowsReach(page, useful.length);
  await page.getByRole("checkbox", { name: "All chat models" }).check();
  await rowsReach(page, ids.length);
  await page.getByRole("checkbox", { name: "All chat models" }).uncheck();

  await page.getByRole("button", { name: `Favorite ${ids[3]}`, exact: true }).click();
  await page.getByRole("button", { name: `Favorite ${ids[3]}`, exact: true }).filter({ hasText: "★" }).waitFor();
  assert.deepEqual(writes, [{ model_id: ids[3], favorite: true }]);
  assert.deepEqual(await options.locator("section").first().getByRole("radio").evaluateAll(radios => radios.map(radio => radio.getAttribute("aria-label"))),
    [ids[1], ids[3]]);

  // Escape closes, unmounts the rows and returns focus to the picker; reopening keeps the search state.
  await search.fill("model-00");
  await page.keyboard.press("Escape");
  assert.equal(await picker.evaluate(node => document.activeElement === node), true);
  assert.equal(await rowCount(page), 0);
  await picker.click();
  assert.equal(await search.inputValue(), "model-00");
  await search.fill("");

  // Choosing a model closes the picker; a reasoning model's effort is chosen at the far right, beside it.
  await page.getByRole("radio", { name: ids[1], exact: true }).click();
  await picker.filter({ hasText: ids[1] }).waitFor();
  assert.equal(await page.getByRole("button", { name: /^Reasoning effort/ }).count(), 0, "a model without effort levels shows none");
  await picker.click();
  await page.getByRole("radio", { name: ids[2], exact: true }).click();
  await picker.filter({ hasText: ids[2] }).waitFor();
  assert.equal(await rowCount(page), 0);
  const effort = page.getByRole("button", { name: "Reasoning effort: Default effort", exact: true });
  assert.equal(await effort.textContent(), "Default");
  await effort.click();
  const efforts = page.getByRole("radiogroup", { name: "Reasoning effort", exact: true });
  assert.equal(await efforts.getByRole("radio", { name: "Default effort", exact: true }).isChecked(), true);
  await efforts.getByRole("radio", { name: "high", exact: true }).click();
  await page.getByRole("button", { name: "Reasoning effort: High", exact: true }).waitFor();
  assert.equal(await efforts.count(), 0, "choosing an effort closes its menu");
  assert.equal(await picker.innerText(), ids[2], "the model picker shows only the model, without an arrow");
  assert.deepEqual(errors, []);
  await page.close();
});

test("the Model capabilities browser lists, filters and expands the large catalog", { timeout: 90000 }, async () => {
  const { page, errors } = await open();
  await page.getByRole("button", { name: "Account menu", exact: true }).click();
  await page.getByRole("button", { name: "Model capabilities", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Model capabilities", exact: true });
  await dialog.getByText(`${useful.length} models · refreshed`, { exact: false }).waitFor();
  await rowsReach(page, useful.length);
  assert.equal(await dialog.getByRole("radio").count(), 0, "the browser is not a picker");
  await dialog.getByText("Filters", { exact: true }).click();
  await dialog.getByRole("checkbox", { name: "All chat models" }).check();
  await rowsReach(page, ids.length);
  await dialog.getByRole("searchbox").fill("model-45");
  await rowsReach(page, ids.filter(id => id.includes("model-45")).length);
  const row = dialog.locator(".org-model-card").first();
  await row.locator("summary").click();
  await row.getByText("Context/output figures are provider capacities", { exact: false }).waitFor();
  assert.deepEqual(errors, []);
  await page.close();
});
