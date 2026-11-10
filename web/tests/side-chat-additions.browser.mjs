// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { realpath } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

test("a side reply at work shows its steps, and your next message can join it instead of asking", { timeout: 90000 }, async () => {
  const root = fileURLToPath(new URL("../", import.meta.url));
  const server = await createServer({ root, server: { host: "127.0.0.1", port: 0, fs: { allow: [root, await realpath(root + "node_modules")] } } });
  await server.listen(); const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } }), errors = [], posted = [], replies = [], brought = [];
    const user = "a".repeat(32), org = "b".repeat(32), topic = "c".repeat(32);
    const model = { id: "fixture/reasoner", name: "Reasoner", supports_tools: true, supports_reasoning: true, reasoning_efforts: [],
      supported_parameters: [], input_modalities: [], output_modalities: [], max_completion_tokens: 4096 };
    const main = [{ id: "1".padStart(32, "0"), sequence: 1, topic_id: topic, author_id: user, author_kind: "human", content: "Do we know the motivic case?",
      client_id: "seed", created_at: 1_800_000_000, revision: 0, reply_to: null }];
    // While `hold` is set, a reply at work stays at work; `finishFirst` makes the next addition arrive just after its reply finished.
    const side = [], runs = [];
    let sideSequence = 0, hold = true, finishFirst = false;
    const sideMessage = (author_kind, content, extra = {}) => ({ id: "f" + String(++sideSequence).padStart(31, "0"), sequence: sideSequence,
      topic_id: topic, owner_id: user, author_kind, content, client_id: "side-" + sideSequence, created_at: 1_800_000_100 + sideSequence * 60,
      assistant_model: null, inference_run_id: null, reply_to: null, brought: null, addition: null, ...extra });
    // A reply reads what was added to it at step 2, and answers.
    const finish = run => {
      run.status = "succeeded";
      const read = run.additions.map(addition => ({ ...addition, read_at: 1_800_009_000, read_step: 2 }));
      run.additions = read;
      for (const addition of read) side.find(item => item.id === addition.message_id).addition = { run_id: run.id, read_at: 1_800_009_000, read_step: 2 };
      run.activity.push(...(read.length ? [{ sequence: 3, phase: "additions_read", step: 2, message_ids: read.map(item => item.message_id) }] : []),
        { sequence: 4, phase: "round", round: 2 }, { sequence: 5, phase: "answer_ready" });
      const question = side.find(item => item.id === run.side_chat.question_id);
      side.push(sideMessage("assistant", "Answer to " + question.content, { assistant_model: model.id, inference_run_id: run.id,
        reply_to: question.id, steps: run.activity, additions: read, usage: { prompt_tokens: 10, completion_tokens: 20, actual_microusd: null } }));
    };
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
      else if (path.endsWith("/research-settings")) data = { tools: ["history"], web_search_configured: false, current_date: "2026-11-07" };
      else if (path.endsWith("/compute-configuration")) data = { images: [], policy: { allowed_images: [] } };
      else if (path.endsWith("/navigation-usage")) data = { projects: {}, channels: {}, topics: {} };
      else if (path.endsWith("/side-chat/messages")) {
        posted.push(body);
        const run = body.adds_to && runs.find(item => item.id === body.adds_to);
        if (run && finishFirst) { finishFirst = false; finish(run); }
        if (run && run.status !== "running") { status = 409; data = { detail: "the model reply has finished; send this as a new question" }; }
        else {
          data = sideMessage("human", body.content, { client_id: body.client_id,
            addition: run ? { run_id: run.id, read_at: null, read_step: null } : null });
          if (run) run.additions.push({ message_id: data.id, run_id: run.id, added_by: user, moved: false, cancelled_runs: [],
            created_at: data.created_at, read_at: null, read_step: null, content: data.content });
          side.push(data); status = 201;
        }
      } else if (path.includes("/side-chat/additions/") && request.method() === "DELETE") {
        const message = side.find(item => item.id === path.split("/").at(-1));
        for (const run of runs) run.additions = run.additions.filter(item => item.message_id !== message.id);
        message.addition = null; data = message;
      } else if (path.endsWith("/side-chat/replies")) {
        replies.push(body);
        const created = { id: "run-" + body.question_id, model: body.models[0], status: "running", created_at: 1_800_000_500, requested_by: user,
          output_text: "", cancel_requested: 0, message_id: null, side_chat: { question_id: body.question_id }, additions: [],
          activity: [{ sequence: 1, phase: "round", round: 1 }, { sequence: 2, phase: "tool_requested", tool: "topic_search",
            detail: "searching the topic for “regulator”" }] };
        runs.unshift(created); data = [created]; status = 202;
      } else if (path.endsWith("/side-chat/bring")) {
        brought.push(body);
        const chosen = side.filter(item => body.message_ids.includes(item.id));
        data = { ...main[0], id: "2".padStart(32, "0"), sequence: 2, client_id: body.client_id, content: "From my side chat",
          side_chat: { owner_id: user, words: "", omitted: 0, linked: [], messages: chosen.map(item => ({ id: item.id, author_kind: item.author_kind,
            assistant_model: item.assistant_model, content: item.content, created_at: item.created_at })) } };
        for (const item of chosen) item.brought = { message_id: data.id, created_at: data.created_at };
        main.push(data); status = 201;
      } else if (path.endsWith("/side-chat")) {
        if (!hold) for (const run of runs.filter(item => item.status === "running")) finish(run);
        const after = Number(url.searchParams.get("after") ?? 0);
        data = { messages: side.filter(item => item.sequence > after), runs: structuredClone(runs) };
      } else if (path.endsWith("/messages") && request.method() === "GET") data = main;
      else if (path.endsWith("/message-updates")) data = { after: 0, messages: [] };
      else if (path.includes("/reactions/")) data = { counts: { up: 0, down: 0, upside_down: 0 }, mine: [] };
      else if (path.endsWith("/inference-runs")) data = [];
      else if (path.endsWith("/bot-runs") || path.endsWith("/agents")) data = [];
      else if (path.endsWith("/goal")) data = null;
      await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(data) });
    });
    await page.goto(server.resolvedUrls.local[0] + `organization.html?organization=${org}&topic=${topic}`);
    await page.getByText("Do we know the motivic case?", { exact: true }).waitFor();
    await page.getByRole("button", { name: "Side chat", exact: true }).click();
    const panel = page.getByRole("complementary", { name: "Side chat", exact: true });
    const box = panel.getByLabel("Side chat message", { exact: true });
    await box.fill("Which regulator do we mean here?");
    await box.press("Enter");

    // The reply at work shows its steps under the model's name, as a reply in the conversation does.
    const place = panel.locator(".org-side-waiting");
    const steps = place.getByRole("list", { name: "Reply steps and added messages" });
    await steps.getByText("Step 1 · searching the topic for “regulator”…", { exact: true }).waitFor();
    assert.equal(await steps.getByRole("status", { name: "Research progress" }).count(), 1, "the current step is marked as at work");

    // + Add makes the next message join the reply: the box says so, and the message asks for no reply of its own.
    await place.getByRole("button", { name: "Add to this reply", exact: true }).click();
    const bar = panel.getByRole("status").filter({ hasText: "Adding to reasoner's reply · Which regulator do we mean here?" });
    await bar.waitFor();
    assert.equal(await place.getByRole("button", { name: "Adding to this reply", exact: true }).getAttribute("aria-pressed"), "true");
    assert.equal(await box.getAttribute("placeholder"), "Add to the reply…");
    await page.waitForFunction(() => document.activeElement?.id === "org-side-message"); // the box is ready for the message
    await box.fill("Only for number fields, please.");
    await box.press("Enter");
    const added = steps.locator(".org-timeline-addition", { hasText: "Only for number fields, please." });
    await added.getByText("at step 2", { exact: true }).waitFor();
    assert.equal(posted.at(-1).adds_to, runs[0].id);
    assert.equal(replies.length, 1, "an added message asks for no reply");
    await bar.waitFor({ state: "detached" });
    assert.equal(await box.getAttribute("placeholder"), "Ask privately…", "one message joins per + Add");
    assert.equal(await panel.locator(".org-message-group-mine", { hasText: "Only for number fields" }).count(), 0,
      "it shows in the reply's steps, not as a question");

    // An addition the reply has not read can be taken back; it stays in the side chat as a message of its own.
    await place.getByRole("button", { name: "Add to this reply", exact: true }).click();
    await box.fill("Never mind the sign.");
    await box.press("Enter");
    const unread = steps.locator(".org-timeline-addition", { hasText: "Never mind the sign." });
    await unread.getByRole("button", { name: "Undo", exact: true }).click();
    await panel.locator(".org-message-group-mine", { hasText: "Never mind the sign." }).waitFor();
    await unread.waitFor({ state: "detached" });
    await panel.locator(".org-side-ended", { hasText: "No reply was requested." }).waitFor();

    // The reply reads the addition at its next step and answers; the addition folds under the answer.
    hold = false;
    await panel.getByText("Answer to Which regulator do we mean here?", { exact: true }).waitFor();
    await place.filter({ has: page.locator(".org-side-working:not(.org-side-ended)") }).waitFor({ state: "detached" });
    assert.equal(await panel.getByText("Only for number fields, please.", { exact: true }).count(), 0);
    const answer = panel.locator("article", { hasText: "Answer to Which regulator do we mean here?" });
    await answer.getByRole("button", { name: "2 steps · took 1 later message into account", exact: true }).click();
    await answer.locator(".org-timeline-addition", { hasText: "Only for number fields, please." }).getByText("read at step 2", { exact: true }).waitFor();

    // Bringing the exchange brings the message the reply read with it.
    await answer.hover();
    await answer.getByRole("button", { name: "Bring to chat", exact: true }).click();
    await panel.getByRole("group", { name: "Bring to chat", exact: true }).getByRole("button", { name: "Bring 1 exchange", exact: true }).click();
    await panel.getByText("Brought 1 exchange to the topic.", { exact: true }).waitFor();
    const question = side.find(item => item.content === "Which regulator do we mean here?");
    const joined = side.find(item => item.content === "Only for number fields, please.");
    assert.deepEqual(brought[0].message_ids, [question.id, joined.id, side.find(item => item.reply_to === question.id).id]);

    // A reply that finished before the message arrived does not take it: the message is asked as a new question.
    hold = true;
    await box.fill("Is it injective?");
    await box.press("Enter");
    await place.getByRole("button", { name: "Add to this reply", exact: true }).click();
    finishFirst = true;
    await box.fill("And for function fields?");
    await box.press("Enter");
    await panel.getByText("reasoner's reply finished first, so this was asked as a new question.", { exact: true }).waitFor();
    const late = () => side.find(item => item.content === "And for function fields?");
    for (const stop = Date.now() + 10000; replies.at(-1)?.question_id !== late()?.id && Date.now() < stop;)
      await new Promise(resolve => setTimeout(resolve, 50));
    assert.equal(replies.at(-1).question_id, late().id, "its own reply was asked for");
    assert.equal(late().addition, null);
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
    await server.close();
  }
});
