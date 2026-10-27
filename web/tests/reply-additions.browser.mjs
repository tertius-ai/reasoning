// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { realpath } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

test("thread mode adds to a running reply's timeline, Esc returns to the usual box, and a posted message moves in", { timeout: 60000 }, async () => {
  const root = fileURLToPath(new URL("../", import.meta.url));
  const server = await createServer({ root, server: { host: "127.0.0.1", port: 0,
    fs: { allow: [root, await realpath(root + "node_modules")] } } });
  await server.listen(); const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1100, height: 900 } }), errors = [], writes = [];
    const user = "a".repeat(32), org = "b".repeat(32), topic = "c".repeat(32);
    const hierarchy = { projects: [{ id: "project", name: "Testing", visibility: "organization" }],
      channels: [{ id: "channel", name: "research", project_id: "project" }],
      topics: [{ id: topic, name: "Motives", channel_id: "channel", kind: "beta", default_model: "fixture/reasoner", knowledge_tools: [] }], topic_state: [] };
    const model = { id: "fixture/reasoner", name: "Reasoner", supports_tools: true, supports_reasoning: true,
      reasoning_efforts: ["high"], supported_parameters: [], input_modalities: [], output_modalities: [] };
    const base = { requested_by: user, model: model.id, created_at: 1_800_000_000, cancel_requested: 0, message_id: null, output_text: "",
      accounting_state: "reserved", actual_microusd: null, reserved_microusd: 10 };
    const message = (id, sequence, content) => ({ id, sequence, created_at: base.created_at + sequence * 60, content,
      author_kind: "human", author_id: user, topic_id: topic });
    let messages = [message("question", 1, "Which steps prove DM(k, C) from the literature?"),
      message("later", 2, "Skip French-language sources."), message("started", 3, "Also the étale realization.")];
    // An earlier finished answer, with its steps and usage.
    const answer = { ...message("answer", 0, "An earlier answer."), author_kind: "assistant", author_id: null,
      steps: [{ sequence: 1, phase: "round", round: 1 }, { sequence: 2, phase: "answer_ready" }],
      usage: { prompt_tokens: 128, completion_tokens: 96, actual_microusd: 0, estimated_kwh: 0.0000622, estimate_kind: "reference" } };
    let additions = [];
    const runs = () => [
      { ...base, id: "working", status: "running", context_through_sequence: 1, additions,
        cost_progress: { confirmed_microusd: 0, next_call_estimate_microusd: 0 },
        activity: [{ sequence: 1, phase: "round", round: 1 },
          { sequence: 2, phase: "tool_requested", tool: "arxiv_search", detail: "searched arXiv for “DM(k, C)”" }] },
      { ...base, id: "own-later", context_through_sequence: 2, ...(additions.some(item => item.message_id === "later")
        ? { status: "cancelled", error_code: "moved_into_reply", accounting_state: "released" } : { status: "queued" }) },
      { ...base, id: "own-started", status: "running", context_through_sequence: 3 },
    ];
    const addition = (messageId, moved, content) => ({ message_id: messageId, run_id: "working", added_by: user, moved,
      cancelled_runs: moved ? ["own-later"] : [], created_at: base.created_at + 600, read_at: null, read_step: null, content });
    page.on("pageerror", error => errors.push(error.message));
    await page.route("**/api/**", async route => {
      const request = route.request(), path = new URL(request.url()).pathname, body = request.postDataJSON();
      if (request.method() !== "GET") writes.push({ method: request.method(), path, body });
      let data = [];
      if (path === "/api/health") data = { initialized: true };
      else if (path === "/api/auth/me") data = { id: user, name: "Elden", email: "elden@example.org" };
      else if (path === "/api/organizations") data = [{ id: org, name: "Testing", role: "owner" }];
      else if (path === "/api/auth/model-favorites") data = { user_id: user, models: [] };
      else if (path.endsWith("/hierarchy")) data = hierarchy;
      else if (path.endsWith("/members")) data = [{ id: user, name: "Elden", role: "owner" }];
      else if (path.endsWith("/presence")) data = { humans: [], bots: [] };
      else if (path.endsWith("/inference-configuration")) data = { allowed_models: [model.id], tools: [] };
      else if (path.endsWith("/model-prices")) data = { models: [model], allowed_models: [model.id] };
      else if (path.endsWith("/research-instructions")) data = { system_prompt: "Research safely.", prompt_version: "fixture" };
      else if (path.endsWith("/research-settings")) data = { tools: ["history"], web_search_configured: false, current_date: "2026-10-26" };
      else if (path.endsWith("/compute-configuration")) data = { images: [], policy: { allowed_images: [] } };
      else if (path.endsWith("/navigation-usage")) data = { projects: {}, channels: {}, topics: {} };
      else if (path.endsWith("/messages") && request.method() === "POST") {
        const posted = { ...message("sent-" + messages.length, messages.length + 1, body.content) };
        if (body.adds_to) { posted.addition = addition(posted.id, false, body.content); additions = [...additions, posted.addition]; }
        messages = [...messages, posted]; data = posted;
      } else if (path.endsWith("/messages")) data = [answer, ...messages];
      else if (path.endsWith("/message-updates")) data = { after: 0, messages: [] };
      else if (path.includes("/reactions/")) data = { counts: { up: 0, down: 0, upside_down: 0 }, mine: [] };
      else if (path.endsWith("/inference-runs/working/additions")) {
        data = addition(body.message_id, true, messages.find(item => item.id === body.message_id).content); additions = [...additions, data];
        messages = messages.map(item => item.id === body.message_id ? { ...item, addition: data } : item);
      } else if (request.method() === "DELETE" && path.includes("/inference-runs/working/additions/")) {
        const id = path.split("/").at(-1);
        additions = additions.filter(item => item.message_id !== id);
        messages = messages.map(item => item.id === id ? { ...item, addition: undefined } : item);
        data = { message_id: id, moved: false, cancelled_runs: [] };
      }
      else if (path.endsWith("/inference-runs")) data = runs();
      else if (path.endsWith("/bot-runs")) data = [];
      else if (path.endsWith("/agents")) data = [];
      else if (path.endsWith("/goal")) data = null;
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(data) });
    });
    await page.goto(server.resolvedUrls.local[0] + "organization.html");
    const card = page.locator('[data-run-id="working"]');
    const line = card.locator(".org-thread-line");
    await line.waitFor();
    assert.equal(await line.textContent(), "Add to this reply");

    // Each footer is one row: the first part on the left and the last at the right edge. While the reply runs, that is
    // its usage and its costs; under a finished answer, its folded steps and its usage, with the steps opening below.
    const sameLine = async (left, right, row) => {
      const [a, b, r] = await Promise.all([left.boundingBox(), right.boundingBox(), row.boundingBox()]);
      assert.ok(Math.abs(a.y + a.height / 2 - (b.y + b.height / 2)) < 3, "one line");
      assert.ok(Math.abs(a.x - r.x) < 1 && Math.abs(b.x + b.width - (r.x + r.width)) < 1, "left part left, right part at the right edge");
    };
    const running = card.locator(".org-usage-row");
    await sameLine(running.locator(".org-message-usage"), running.getByText(/^Confirmed .* · next-call estimate /), running);
    const answered = page.locator('[data-message-id="answer"]');
    const steps = answered.getByRole("button", { name: "1 step", exact: true });
    await sameLine(steps, answered.locator(".org-message-usage"), answered.locator(".org-usage-row"));
    assert.match(await answered.locator(".org-message-usage").innerText(), /^128 in \/ 96 out .* Wh\*$/);
    assert.equal(await steps.getAttribute("aria-expanded"), "false");
    await steps.click();
    const record = answered.getByLabel("Steps and later messages of this reply", { exact: true });
    await record.waitFor();
    assert.ok((await record.boundingBox()).y > (await steps.boundingBox()).y, "the steps open below the row");
    await steps.click();
    await record.waitFor({ state: "detached" });

    // The thread line puts the usual box into thread mode: a tinted bar, the plus inside, no model row.
    await line.click();
    const bar = page.locator(".org-adding-bar");
    await bar.getByText(/Adding to reasoner's reply to the .* question/).waitFor();
    const box = page.locator(".org-composer-box.org-composer-adding");
    assert.equal(await box.getByRole("button", { name: "Attach picture or PDF", exact: true }).count(), 1, "the plus sits inside the box");
    assert.equal(await page.getByRole("button", { name: "Model details", exact: true }).isVisible(), false, "the model row hides");
    assert.equal(await page.getByLabel("Message", { exact: true }).getAttribute("placeholder"), "Add to reasoner's reply…");
    // The card re-renders from the conversation's activity one effect after the box, so its pressed state is awaited.
    await card.locator('.org-thread-line[aria-pressed="true"]').waitFor();

    // Esc returns to the usual box and keeps the text.
    const box1 = page.getByLabel("Message", { exact: true });
    await box1.fill("Is there a version over finite fields?");
    assert.equal(await bar.isVisible(), true, "typing keeps thread mode");
    await box.getByRole("button", { name: "Send", exact: true }).waitFor();
    assert.equal(await box.getByRole("button", { name: "Stop current work", exact: true }).count(), 0, "return replaces Stop");
    await page.screenshot({ path: process.env.REPLY_ADDITIONS_SHOTS ? process.env.REPLY_ADDITIONS_SHOTS + "/thread-mode.png" : undefined });
    await box1.press("Escape");
    assert.equal(await bar.count(), 0);
    await page.getByRole("button", { name: "Model details", exact: true }).waitFor();
    assert.equal(await box1.inputValue(), "Is there a version over finite fields?");
    assert.equal(await page.locator(".org-composer-box.org-composer-adding").count(), 0);
    await page.screenshot({ path: process.env.REPLY_ADDITIONS_SHOTS ? process.env.REPLY_ADDITIONS_SHOTS + "/after-escape.png" : undefined });
    await box1.fill("");

    // ↩ on the question being answered enters thread mode; sending adds the message and requests no reply of its own.
    const question = page.locator('[data-message-id="question"]');
    await question.hover();
    await question.getByRole("button", { name: "Add to reasoner's reply", exact: true }).click();
    await bar.waitFor();
    await box1.fill("Cite exact theorem numbers.");
    await box1.press("Enter");
    // The addition appears in the reply's timeline after its current step, not as a separate transcript message.
    const timeline = card.getByLabel("Reply steps and additions");
    const waiting = timeline.locator(".org-timeline-addition", { hasText: "Cite exact theorem numbers." });
    await waiting.getByText("at step 2").waitFor();
    assert.deepEqual((await timeline.locator("li").allInnerTexts()).map(text => text.replace(/\s+/g, " ").trim()),
      ["Step 1 · searched arXiv for “DM(k, C)”…", "You · Cite exact theorem numbers. at step 2 Undo"]);
    assert.equal(await waiting.locator("q").innerText(), "Cite exact theorem numbers.", "the addition is quoted");
    assert.match(await card.locator("summary").innerText(), /working · \d+ (s|min)/);
    const sent = writes.find(write => write.method === "POST" && write.path.endsWith("/messages"));
    assert.equal(sent.body.adds_to, "working");
    assert.equal(await page.locator('[data-message-id="sent-3"]').count(), 0, "not in the transcript");
    assert.equal(writes.filter(write => write.method === "POST" && write.path.endsWith("/inference-runs")).length, 0);
    assert.equal(await bar.count(), 0, "after sending, the box returns to new questions");
    await line.filter({ hasText: /^Add to this reply$/ }).waitFor();
    await page.screenshot({ path: process.env.REPLY_ADDITIONS_SHOTS ? process.env.REPLY_ADDITIONS_SHOTS + "/timeline.png" : undefined });

    // Undo removes an unread addition from the timeline, and the message returns to the transcript.
    await waiting.getByRole("button", { name: "Undo", exact: true }).click();
    await waiting.waitFor({ state: "detached" });
    assert.equal(writes.at(-1).method, "DELETE");
    await page.locator('[data-message-id="sent-3"]').waitFor();

    // A posted message whose own reply has not started moves in from its menu; a started one cannot.
    const started = page.locator('[data-message-id="started"]');
    await started.hover();
    await started.getByRole("button", { name: "Message actions", exact: true }).click();
    const blocked = page.getByRole("menuitem", { name: /Move into reasoner's thread/ });
    assert.equal(await blocked.isDisabled(), true);
    assert.match(await blocked.textContent(), /Its own reply has started/);
    await page.keyboard.press("Escape");
    const later = page.locator('[data-message-id="later"]');
    await later.hover();
    await later.getByRole("button", { name: "Message actions", exact: true }).click();
    await page.getByRole("menuitem", { name: "Move into reasoner's thread", exact: true }).click();
    await timeline.locator(".org-timeline-addition", { hasText: "Skip French-language sources." }).getByText("at step 2").waitFor();
    await later.waitFor({ state: "detached" });
    assert.deepEqual(writes.at(-1), { method: "POST", path: `/api/organizations/${org}/inference-runs/working/additions`,
      body: { message_id: "later" } });
    await page.locator('[data-run-id="own-later"]').waitFor({ state: "detached" });
    await page.screenshot({ path: process.env.REPLY_ADDITIONS_SHOTS ? process.env.REPLY_ADDITIONS_SHOTS + "/moved.png" : undefined });
    assert.deepEqual(errors, []);
  } finally {
    await browser.close(); await server.close();
  }
});
