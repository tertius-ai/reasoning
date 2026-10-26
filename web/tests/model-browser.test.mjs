import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

let server, models, diagnostics, recommendations, usage;
before(async () => {
  server = await createServer({ appType: "custom", logLevel: "silent", server: { middlewareMode: true } });
  models = await server.ssrLoadModule("/src/ModelBrowser.tsx");
  diagnostics = await server.ssrLoadModule("/src/TopicDiagnostics.tsx");
  recommendations = await server.ssrLoadModule("/src/modelRecommendations.ts");
  usage = await server.ssrLoadModule("/src/OrganizationUsage.tsx");
});
after(async () => { await server?.close(); });

test("sections are alphabetical, deduplicated and constrained by supplied catalogue choices", () => {
  const catalog = { models: [{ id: "z/math" }, { id: "a/math" }, { id: "b/open", weights_repository: "b/open" }, { id: "c/other" }],
    recommendations: { model_ids: ["disallowed/math", "z/math", "a/math"] } };
  assert.deepEqual(models.modelSections(["z/math", "b/open", "a/math", "c/other", "a/math"], catalog), [
    { title: "Best for math", ids: ["a/math", "z/math"] }, { title: "Best open-weights", ids: ["b/open"] }, { title: "Other models", ids: ["c/other"] },
  ]);
  assert.deepEqual(models.modelSections(["z/math", "a/math"], catalog, "z/"), [{ title: "Best for math", ids: ["z/math"] }]);
});

test("chat sections hide batch, embedding, and reranking aliases but retain new models and routers", () => {
  const ids = ["new/fresh-model", "openrouter/auto", "provider/model:batch", "provider/model-batch", "provider/embeddings-v2", "provider/reranker"];
  assert.deepEqual(models.modelSections(ids, null), [{ title: "Other models", ids: ["new/fresh-model", "openrouter/auto"] }]);
});

test("useful default choices are capability based, retaining bots, routers, and unknown catalog metadata", () => {
  assert.equal(recommendations.usefulChatModel("provider/plain", { supports_tools: false, supports_reasoning: false }), false);
  assert.equal(recommendations.usefulChatModel("provider/new", { supports_tools: true }), true);
  assert.equal(recommendations.usefulChatModel("provider/reasoning", { supports_reasoning: true }), true);
  assert.equal(recommendations.usefulChatModel("bot:local", { execution_source: "bot" }), true);
  assert.equal(recommendations.usefulChatModel("openrouter/auto", {}), true);
  assert.equal(recommendations.usefulChatModel("provider/not-yet-loaded"), true);
  assert.equal(recommendations.usefulChatModel("provider/model:batch", { supports_tools: true }), false);
});

test("main model browser has no recommendations editor or recommendation groups", () => {
  const catalog = { models: [{ id: "a/math", supported_parameters: [], input_modalities: [], output_modalities: [] }],
    recommendations: { model_ids: ["a/math"] } };
  const html = renderToStaticMarkup(React.createElement(models.ModelRows, { ids: ["a/math"], catalog, allowedModels: ["a/math"], grouped: false }));
  assert.match(html, /<h5>Models<\/h5>/);
  assert.doesNotMatch(html, /Best for math|Best open-weights/);
  const browser = renderToStaticMarkup(React.createElement(usage.OrganizationModelBrowser, { organizationId: "org", canManage: true, onAuthLost() {} }));
  assert.doesNotMatch(browser, /Recommendations|Preferred models|shortlist/);
  assert.match(browser, /All chat models/);
});

test("compact rows show one model identifier, prices, capacities and sourced availability flags", () => {
  const model = { id: "m/model", name: "Do not duplicate this name", context_length: 1048576, max_completion_tokens: 128000,
    input_usd_per_million: 0.69, output_usd_per_million: 14, supports_tools: true, supports_reasoning: true,
    routing_availability: { zdr: true, regions: ["US", "EU"] } };
  const html = renderToStaticMarkup(React.createElement(models.ModelLine, { id: model.id, model, allowed: true, math: true }));
  assert.match(html, /m\/model/); assert.doesNotMatch(html, /Do not duplicate/);
  assert.match(html, /math, reasoning, tools/); assert.match(html, /\$0.69\/14/); assert.match(html, /1M\/128k/);
  assert.match(html, /not a live provider or credit check/); assert.match(html, /enforcement is chosen in organization provider routing/);
  assert.match(html, /🇺🇸/); assert.match(html, /🇪🇺/);
  const unknown = renderToStaticMarkup(React.createElement(models.ModelLine, { id: "closed/model", allowed: false, math: false }));
  assert.doesNotMatch(unknown, /🇺🇸|🇪🇺|zdr<|org-model-available/); assert.match(unknown, /—\/—/);
});

test("capacities are compact without rounding small values to zero", () => {
  assert.equal(models.compactTokens(943718), "944k"); assert.equal(models.compactTokens(8192), "8.2k");
  assert.equal(models.compactTokens(512), "512"); assert.equal(models.compactTokens(null), "—");
});

test("one selector label combines only advertised reasoning effort and a bot's model identity", () => {
  const catalog = { models: [{ id: "openai/example", reasoning_efforts: ["high", "xhigh"] },
    { id: "bot/abc/example", name: "My bot · example", execution_source: "bot", reasoning_efforts: ["high"] }] };
  assert.equal(models.modelSelectionLabel(catalog, "openai/example", "xhigh"), "openai/example/xhigh");
  assert.equal(models.modelSelectionLabel(catalog, "openai/example", "max"), "openai/example");
  assert.equal(models.modelSelectionLabel(catalog, "bot/abc/example", "high"), "My bot · example/high");
  const html = renderToStaticMarkup(React.createElement(models.ModelPicker, { label: "Model", models: ["openai/example"],
    catalog, value: "openai/example", effort: "high", onChange() {}, onEffortChange() {} }));
  assert.match(html, /openai\/example\/high/);
  assert.doesNotMatch(html, /effort-select/);
});

test("effort menu uses compact native radio choices and only advertised levels", () => {
  const render = effort => renderToStaticMarkup(React.createElement(models.EffortMenu, {
    model: "provider/reasoner", catalog: { models: [{ id: "provider/reasoner", reasoning_efforts: ["high", "xhigh"] }] },
    effort, disabled: false, onBack() {}, onSelect() {},
  }));
  const html = render("xhigh");
  assert.match(html, /role="radiogroup" aria-label="Reasoning effort"/);
  assert.match(html, /aria-label="Back to models"/);
  assert.match(html, /aria-label="Default effort"/);
  assert.match(html, /aria-label="xhigh"[^>]*checked=""/);
  assert.match(html, />Extra high</);
  assert.doesNotMatch(html, /aria-label="low"|aria-pressed|<button[^>]*>high/);
  assert.match(render("unsupported"), /aria-label="Default effort"[^>]*checked=""/);
});

test("the effort menu beside the model picker shows only the levels, with a check mark on the current one", () => {
  const html = renderToStaticMarkup(React.createElement(models.EffortMenu, {
    model: "provider/reasoner", catalog: { models: [{ id: "provider/reasoner", reasoning_efforts: ["high", "xhigh"] }] },
    effort: "high", disabled: false, onSelect() {}, checks: true,
  }));
  assert.doesNotMatch(html, /provider\/reasoner|<header|Back to models/, "no model name above the levels");
  assert.match(html, /role="radiogroup" aria-label="Reasoning effort"/, "native radios stay for the keyboard");
  assert.deepEqual([...html.matchAll(/class="org-effort-check" aria-hidden="true">(✓?)<\/span><span>([^<]+)</g)].map(match => match[1] + match[2]),
    ["Default", "✓High", "Extra high"]);
});

test("benchmark indices use exact identifiers, show zero, and leave missing scores blank", () => {
  const benchmarks = { models: { "m/model": { intelligence_index: 71.2, agentic_index: 0, coding_index: 65.8 } },
    meta: { as_of: "2026-10-09T12:00:00Z" }, status: "cached" };
  const html = renderToStaticMarkup(React.createElement(models.ModelLine, { id: "m/model", allowed: true, math: false, scores: benchmarks }));
  assert.match(html, /I 71.2/); assert.match(html, /A 0.0/); assert.match(html, /2026-10-09/); assert.match(html, /cached/);
  assert.doesNotMatch(html, /65.8/);
  const details = renderToStaticMarkup(React.createElement(models.ModelDetails, { id: "m/model", catalog: { benchmarks } }));
  assert.match(details, /coding 65.8/); assert.match(details, /not research correctness guarantees/);
  for (const id of ["m/model:free", "m/model-20260101", "missing/model"]) {
    const missing = renderToStaticMarkup(React.createElement(models.ModelLine, { id, allowed: true, math: false, scores: benchmarks }));
    assert.doesNotMatch(missing, /org-model-scores|NaN/);
  }
  assert.equal(models.benchmarkScores("m/alias", { canonical_slug: "m/model" }, benchmarks), benchmarks.models["m/model"]);
  assert.equal(models.benchmarkScores("m/alias", { canonical_slug: "m/not-listed" }, benchmarks), undefined);
});

test("summary JSON has one record per line without changing values; full traces stay readable", () => {
  const value = { schema: "reasoning.topic-summary.v2", messages: [{ content: "Unicode α\nQuoted \"text\"", metadata: { nested: [null, false, 0] } }],
    runs: Array.from({ length: 5 }, (_, i) => ({ id: i, diagnostics: { nested: { status: "failed" } } })),
    empty: [], current_instructions: { system_prompt: "Complete prompt\nSecond line" } };
  const serialized = diagnostics.serializeTopicJSON(value);
  assert.deepEqual(JSON.parse(serialized), value);
  assert.ok(serialized.split("\n").length < 20);
  const full = { schema: "reasoning.topic-trace.v2", data: { messages: value.messages }, blobs: {} };
  assert.equal(diagnostics.serializeTopicJSON(full), JSON.stringify(full, null, 2));
});
