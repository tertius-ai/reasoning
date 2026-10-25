import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { after, before, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

let server, controls, groups, BetaModelMenu, TopicCopyButton, ModelPicker, ModelRows;
let runFailureMessage;
before(async () => {
  server = await createServer({ appType: "custom", logLevel: "silent", server: { middlewareMode: true } });
  controls = await server.ssrLoadModule("/src/ResearchControls.tsx");
  groups = await server.ssrLoadModule("/src/modelRecommendations.ts");
  ({ BetaModelMenu } = await server.ssrLoadModule("/src/OrganizationApp.tsx"));
  ({ runFailureMessage } = await server.ssrLoadModule("/src/BetaConversation.tsx"));
  ({ TopicCopyButton } = await server.ssrLoadModule("/src/TopicDiagnostics.tsx"));
  ({ ModelPicker, ModelRows } = await server.ssrLoadModule("/src/ModelBrowser.tsx"));
});
after(async () => { await server?.close(); });

test("goal commands require an explicit command and preserve the objective", () => {
  assert.equal(controls.goalCommand(" /goal Prove the result\nusing the cited definitions. "), "Prove the result\nusing the cited definitions.");
  assert.equal(controls.goalCommand("/goal"), "");
  for (const text of ["Explain /goal", "/goals", "ordinary chat"]) assert.equal(controls.goalCommand(text), null);
});

test("goal capture freezes exact acceptance and budgets for lost-response retries", () => {
  const captured = controls.captureGoal({ objective: " Check the theorem ", acceptance: "Prove every claim\n\nCite the hypotheses", model: "allowed/model",
    budget: "2.500001", turns: "5", tools: "40", hours: "0.5", clientId: "stable-reviewed-goal" });
  assert.equal(captured.budget_microusd, 2500001); assert.equal(captured.duration_seconds, 1800);
  assert.deepEqual(captured.acceptance, ["Prove every claim", "Cite the hypotheses"]);
  assert.equal(captured.objective, "Check the theorem");
  assert.equal(Object.isFrozen(captured), true); assert.equal(Object.isFrozen(captured.acceptance), true);
  for (const invalid of [{ budget: "0" }, { turns: "0" }, { tools: "1001" }, { hours: "25" }, { acceptance: " " }, { model: "" }]) {
    assert.throws(() => controls.captureGoal({ objective: "Proof", acceptance: "Checked proof", model: "a/b", budget: "1", turns: "5", tools: "20", hours: "1", clientId: "reviewed", ...invalid }));
  }
});

test("goal controls distinguish read access and expose actual bounded progress", () => {
  const goal = { id: "g", objective: "Check <script>danger</script>", owned_by: "u", model: "m", status: "active", acceptance: ["Evidence"],
    budget_microusd: 2000000, used_or_held_microusd: 400000, max_turns: 10, turns: 2, max_tool_calls: 100, tool_calls: 7, events: [] };
  const reader = renderToStaticMarkup(React.createElement(controls.ResearchGoalSummary, { goal, canControl: false, busy: false, onControl: () => {} }));
  assert.match(reader, /2\/10 turns/); assert.match(reader, /7\/100 tools/); assert.match(reader, /used or held/);
  assert.doesNotMatch(reader, /<script>|>Pause<|>End goal</);
  const owner = renderToStaticMarkup(React.createElement(controls.ResearchGoalSummary, { goal, canControl: true, busy: false, onControl: () => {} }));
  assert.doesNotMatch(owner, />Pause<|>End goal<|org-transport-controls/);
  const paused = renderToStaticMarkup(React.createElement(controls.ResearchGoalSummary, { goal: { ...goal, status: "paused" }, canControl: true, busy: false, onControl: () => {} }));
  assert.match(paused, /paused/); assert.doesNotMatch(paused, /org-transport-controls/);
});

test("mathematics recommendations never expand the allowlist or hide other choices", () => {
  const catalog = { models: [{ id: "r/top", name: "Recommended" }, { id: "r/other", name: "Other" }, { id: "r/private", name: "Disallowed" }],
    recommendations: { model_ids: ["r/private", "r/top"] }, allowed_models: ["r/top", "r/other"] };
  assert.deepEqual(groups.modelOptionGroups(["r/top", "r/other"], catalog), { recommended: ["r/top"], other: ["r/other"] });
  assert.deepEqual(groups.modelOptionGroups(["r/top", "r/other"], catalog, "OTHER"), { recommended: [], other: ["r/other"] });
  const rows = renderToStaticMarkup(React.createElement(ModelRows, { ids: ["r/top", "r/other"], catalog, allowedModels: ["r/top", "r/other"],
    selected: "r/top", name: "reply", onSelect: () => {} }));
  assert.match(rows, /Best for math/); assert.match(rows, /Other models/); assert.doesNotMatch(rows, /Disallowed|r\/private/);
});

test("a closed reply menu shows its selection without mounting model options", () => {
  // Opening it, its search, rows and context summary are covered by tests/model-picker.browser.mjs.
  const menu = renderToStaticMarkup(React.createElement(BetaModelMenu, { configuration: { allowed_models: ["r/top", "r/other"] },
    catalog: { models: [{ id: "r/top", name: "Recommended", max_completion_tokens: 943718 }, { id: "r/other", name: "Other" }] },
    selected: "r/top", tools: [], disabled: false, error: "", onSelect: () => {} }));
  assert.match(menu, /aria-label="Choose reply model"/); assert.match(menu, /r\/top/);
  assert.doesNotMatch(menu, /org-model-options|org-model-card|Search models and routers|r\/other|Cited memory/);
});

test("the selected model's capabilities open from an ⓘ beside the model picker", () => {
  const readers = [["Public source reader", true], ["Literature readers (arXiv)", true], ["General web search", false]];
  const closed = renderToStaticMarkup(React.createElement(controls.ModelCapabilities, {
    model: "test/model", summary: "Research · 2 of 3 readers", date: "2026-10-23", readers }));
  assert.match(closed, /aria-label="Model details"[^>]*aria-expanded="false"[^>]*>i<\/button>/);
  assert.doesNotMatch(closed, /General web search|Research · 2 of 3/, "details stay closed until the ⓘ is clicked");
});

test("system prompt is a chat-styled disclosure and JSON copy is a quiet header icon", () => {
  const html = renderToStaticMarkup(React.createElement(controls.ResearchInstructions, {
    instructions: { system_prompt: "Exact **instruction** text <script>unsafe</script>", prompt_version: "test-version" }, loading: false,
  }));
  assert.match(html, /<summary>System prompt<\/summary>/);
  const bare = renderToStaticMarkup(React.createElement(controls.ResearchInstructions, {
    instructions: { system_prompt: "Exact text", prompt_version: "topic-agents-v9" }, loading: false, bare: true,
  }));
  assert.doesNotMatch(bare, /<summary>/, "the System prompt dialog supplies the heading");
  assert.equal(controls.shortVersion("topic-agents-v9"), "v9");
  assert.equal(controls.shortVersion("custom"), "custom");
  assert.match(html, /org-message org-system-message/);
  assert.match(html, /Exact <strong>instruction<\/strong> text/); assert.doesNotMatch(html, /<script>/);
  const copy = renderToStaticMarkup(React.createElement(TopicCopyButton, { organizationId: "org", topicId: "topic", onAuthLost: () => {} }));
  assert.match(copy, /aria-label="Copy topic JSON"/); assert.match(copy, /<svg/);
  assert.doesNotMatch(copy, /Topic JSON copied|Export includes private|Share only with trusted/);
});

test("research failures describe execution problems without implying a free call or mathematical result", () => {
  assert.match(runFailureMessage("agent_invalid_tool_arguments"), /invalid tool request/);
  assert.match(runFailureMessage("agent_empty_response"), /no visible answer/);
  assert.match(runFailureMessage("output_token_limit"), /partial answer is preserved/);
  assert.match(runFailureMessage("agent_incomplete"), /Inspect Context and the inference ledger/);
  assert.equal(runFailureMessage("provider_protocol"), "provider protocol");
  assert.match(runFailureMessage("agent_execution_error"), /internal error.*Retained work and charges are preserved/);
});

test("automatic reviewer presentation does not imply a required model configuration", () => {
  const html = renderToStaticMarkup(React.createElement(ModelPicker, { label: "Reviewer override", models: [], catalog: null,
    value: "", emptyLabel: "Automatic reviewer", onChange: () => {} }));
  assert.match(html, /Automatic reviewer/); assert.doesNotMatch(html, /Choose model/);
});

test("the narrow model popup is anchored to the whole composer instead of the offset model button", () => {
  const styles = readFileSync(new URL("../src/organization.css", import.meta.url), "utf8");
  const narrow = styles.slice(styles.indexOf("@media (max-width: 540px)"));
  assert.match(narrow, /\.org-model-menu\s*\{[^}]*position:\s*static/);
  assert.match(narrow, /\.org-model-options\s*\{[^}]*left:\s*0;\s*right:\s*0;\s*width:\s*auto/);
  assert.match(styles, /\.org-composer\s*\{[^}]*position:\s*relative/);
});
