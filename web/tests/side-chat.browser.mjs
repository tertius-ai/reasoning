// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { realpath } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

test("a private side chat answers beside the topic, and Bring to chat posts a reply with what it builds on as one folded group",
  { timeout: 90000 }, async () => {
  const root = fileURLToPath(new URL("../", import.meta.url));
  const server = await createServer({ root, server: { host: "127.0.0.1", port: 0, fs: { allow: [root, await realpath(root + "node_modules")] } } });
  await server.listen(); const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } }), errors = [], asked = [], replies = [], brought = [];
    const user = "a".repeat(32), org = "b".repeat(32), topic = "c".repeat(32);
    const model = { id: "fixture/reasoner", name: "Reasoner", supports_tools: true, supports_reasoning: true, reasoning_efforts: [],
      supported_parameters: [], input_modalities: [], output_modalities: [], max_completion_tokens: 4096 };
    let sequence = 0, sideSequence = 0;
    const main = [{ id: "1".padStart(32, "0"), sequence: ++sequence, topic_id: topic, author_id: user, author_kind: "human",
      content: "Do we know the motivic case?", client_id: "seed", created_at: 1_800_000_000, revision: 0, reply_to: null }];
    const side = [], runs = [];
    const sideMessage = (author_kind, content, extra = {}) => ({ id: "f" + String(++sideSequence).padStart(31, "0"), sequence: sideSequence,
      topic_id: topic, owner_id: user, author_kind, content, client_id: "side-" + sideSequence, created_at: 1_800_000_100 + sideSequence * 60,
      assistant_model: null, inference_run_id: null, reply_to: null, statements: [], ...extra });
    const answers = { "Is the Borel regulator injective for number fields?": "Yes, by Borel 1977.",
      "And the motivic version?": "It follows from the Borel case with Suslin rigidity." };
    page.on("pageerror", error => errors.push(error.message));
    await page.route("**/api/**", async route => {
      const request = route.request(), url = new URL(request.url()), path = url.pathname, body = request.postDataJSON();
      let data = [], status = 200;
      if (path === "/api/health") data = { initialized: true };
      else if (path === "/api/auth/me") data = { id: user, name: "Elden Ring", email: "elden@example.org" };
      else if (path === "/api/organizations") data = [{ id: org, name: "Testing", role: "owner" }];
      else if (path === "/api/auth/model-favorites") data = { user_id: user, models: [] };
      else if (path.endsWith("/hierarchy")) data = { projects: [{ id: "project", name: "Testing", visibility: "organization" }],
        channels: [{ id: "channel", name: "research", project_id: "project" }],
        topics: [{ id: topic, name: "Regulators", channel_id: "channel", kind: "beta", default_model: model.id, knowledge_tools: ["ledger"] }],
        topic_state: [] };
      else if (path.endsWith("/members")) data = [{ id: user, name: "Elden Ring", role: "owner" }];
      else if (path.endsWith("/presence")) data = { humans: [], bots: [] };
      else if (path.endsWith("/inference-configuration")) data = { allowed_models: [model.id], tools: [] };
      else if (path.endsWith("/model-prices")) data = { models: [model], allowed_models: [model.id] };
      else if (path.endsWith("/research-instructions")) data = { system_prompt: "Research safely.", prompt_version: "fixture" };
      else if (path.endsWith("/research-settings")) data = { tools: ["history"], web_search_configured: false, current_date: "2026-11-06" };
      else if (path.endsWith("/compute-configuration")) data = { images: [], policy: { allowed_images: [] } };
      else if (path.endsWith("/navigation-usage")) data = { projects: {}, channels: {}, topics: {} };
      else if (path.endsWith("/side-chat/messages")) {
        data = sideMessage("human", body.content, { client_id: body.client_id }); side.push(data); status = 201;
      } else if (path.endsWith("/side-chat/replies")) {
        replies.push(body);
        const created = { id: "run-" + body.question_id, model: body.models[0], status: "running", created_at: 1_800_000_500, requested_by: user,
          output_text: "", cancel_requested: 0, message_id: null, side_chat: { question_id: body.question_id } };
        runs.unshift(created); data = [created]; status = 202;
      } else if (path.endsWith("/side-chat/bring")) {
        brought.push(body);
        const chosen = side.filter(item => body.message_ids.includes(item.id));
        data = { id: "9".padStart(32, "0"), sequence: ++sequence, topic_id: topic, author_id: user, author_kind: "human",
          content: "From my side chat", client_id: body.client_id, created_at: 1_800_001_000, revision: 0, reply_to: null,
          side_chat: { owner_id: user, omitted: body.note ? side.filter(item => item.sequence < chosen[0].sequence).length : 0, omitted_statements: [],
            messages: chosen.map(item => ({ id: item.id, author_kind: item.author_kind, assistant_model: item.assistant_model,
              content: item.content, created_at: item.created_at, statements: item.statements })) } };
        main.push(data); status = 201;
      } else if (path.endsWith("/side-chat")) {
        // A reply at work finishes on the next read of the side chat.
        for (const run of runs.filter(item => item.status === "running")) {
          run.status = "succeeded";
          const question = side.find(item => item.id === run.side_chat.question_id);
          side.push(sideMessage("assistant", answers[question.content], { assistant_model: model.id, inference_run_id: run.id, reply_to: question.id,
            statements: [{ claim_id: "clm_" + String(side.length).padStart(24, "0"), statement_text: "Recorded: " + question.content }] }));
        }
        const after = Number(url.searchParams.get("after") ?? 0);
        data = { messages: side.filter(item => item.sequence > after), runs };
      } else if (path.endsWith("/messages") && request.method() === "GET") data = main;
      else if (path.endsWith("/message-updates")) data = { after: 0, messages: [] };
      else if (path.includes("/reactions/")) data = { counts: { up: 0, down: 0, upside_down: 0 }, mine: [] };
      else if (path.endsWith("/inference-runs") && request.method() === "POST") { asked.push(body); data = []; }
      else if (path.endsWith("/inference-runs")) data = [];
      else if (path.endsWith("/bot-runs") || path.endsWith("/agents")) data = [];
      else if (path.endsWith("/goal")) data = null;
      await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(data) });
    });
    await page.goto(server.resolvedUrls.local[0] + `organization.html?organization=${org}&topic=${topic}`);
    await page.getByText("Do we know the motivic case?", { exact: true }).waitFor();

    // The side chat opens beside the conversation, private and linked to the topic.
    await page.getByRole("button", { name: "Side chat", exact: true }).click();
    const panel = page.getByRole("complementary", { name: "Side chat", exact: true });
    await panel.getByText("Private · linked to Regulators", { exact: true }).waitFor();
    const box = panel.getByLabel("Side chat message", { exact: true });
    await box.fill("Is the Borel regulator injective for number fields?");
    await box.press("Enter");
    await panel.getByText("Yes, by Borel 1977.", { exact: true }).waitFor();
    await box.fill("And the motivic version?");
    await box.press("Enter");
    await panel.getByText("It follows from the Borel case with Suslin rigidity.", { exact: true }).waitFor();
    assert.deepEqual(replies.map(item => [item.models, item.max_completion_tokens]), [[[model.id], 4096], [[model.id], 4096]]);
    assert.equal(await panel.getByRole("list", { name: "Ledger statements" }).count(), 2, "each reply shows the statement it recorded");
    assert.equal(await page.getByLabel("Message history").getByText("Yes, by Borel 1977.").count(), 0, "the side chat stays out of the topic");

    // Bring to chat lists the earlier side messages the reply builds on; Escape closes it without posting.
    const bring = panel.getByRole("button", { name: "Bring to chat", exact: true });
    await bring.nth(1).click();
    const choice = panel.getByRole("group", { name: "Bring to chat", exact: true });
    await choice.getByText("This reply builds on 2 earlier side messages.", { exact: true }).waitFor();
    assert.equal(await choice.getByRole("checkbox").count(), 2);
    await choice.getByRole("button", { name: "Bring 2 with a context note", exact: true }).waitFor();
    await page.keyboard.press("Escape");
    await choice.waitFor({ state: "detached" });
    assert.deepEqual(brought, []);

    // Unticking an earlier message brings the rest; the topic shows one folded group from your side chat.
    await bring.nth(1).click();
    await choice.getByRole("checkbox").nth(1).uncheck();
    await choice.getByRole("button", { name: "Bring 3", exact: true }).click();
    await panel.getByText("Brought 3 messages to the topic.", { exact: true }).waitFor();
    assert.deepEqual(brought[0].message_ids, [side[0].id, side[2].id, side[3].id]);
    assert.equal(brought[0].note, false);
    const transcript = page.getByLabel("Message history");
    const folded = transcript.getByRole("button", { name: /From your side chat · 3 messages/ });
    await folded.waitFor();
    assert.equal(await folded.getAttribute("aria-expanded"), "false");
    await folded.click();
    await transcript.getByText("It follows from the Borel case with Suslin rigidity.", { exact: true }).waitFor();
    assert.equal(asked.length, 0, "bringing and asking privately never ask the topic's model");

    await panel.getByRole("button", { name: "Close side chat", exact: true }).click();
    await panel.waitFor({ state: "detached" });
    assert.equal(await page.getByRole("button", { name: "Side chat", exact: true }).getAttribute("aria-expanded"), "false");
    assert.deepEqual(errors, []);
  } finally { await browser.close(); await server.close(); }
});
