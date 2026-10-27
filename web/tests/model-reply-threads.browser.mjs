// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { realpath } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

test("a model's answer replies to its prompt, a reply to the answer asks a follow-up, and the Thread view follows the work", { timeout: 90000 }, async () => {
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
    const messages = [prompt, aside, answer], cancels = [];
    let runs = [];
    const media = "e".repeat(32), sha = "f".repeat(64), png = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==", "base64");
    const picture = `/api/organizations/${org}/topics/${topic}/media/${media}/${sha}/content`;
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
      else if (path === picture) { await route.fulfill({ status: 200, contentType: "image/png", body: png }); return; }
      else if (path.endsWith(`/media/${media}/${sha}/read`)) data = { mime: "image/png" };
      else if (path.endsWith("/media") && request.method() === "POST") data = { id: media, chunk_bytes: 1 << 20 };
      else if (path.includes(`/media/${media}/chunks/`)) data = {};
      else if (path.endsWith(`/media/${media}/finish`)) data = { id: media, name: "k3.png", mime: "image/png", sha256: sha, size_bytes: png.length,
        url: picture, markdown: `![k3.png](${picture})` };
      else if (path.endsWith("/messages") && request.method() === "POST") {
        posts.push(body); data = message(user, body.content, body.reply_to ?? null, { client_id: body.client_id }); status = 201;
        if (body.adds_to) {
          data.addition = { message_id: data.id, run_id: body.adds_to, added_by: user, moved: false, cancelled_runs: [],
            created_at: data.created_at, read_at: null, read_step: null, content: body.content };
          runs = runs.map(run => run.id === body.adds_to ? { ...run, additions: [...run.additions, data.addition] } : run);
        }
        messages.push(data);
      } else if (path.endsWith("/messages")) data = messages;
      else if (path.endsWith("/message-updates")) data = { after: 0, messages: [] };
      else if (path.includes("/reactions/")) data = { counts: { up: 0, down: 0, upside_down: 0 }, mine: [] };
      else if (path.endsWith("/inference-runs") && request.method() === "POST") { asked.push(body); data = []; }
      else if (path.endsWith("/inference-runs")) data = runs;
      else if (path.endsWith("/cancel") && request.method() === "POST") {
        cancels.push(parts[5]); runs = runs.map(run => run.id === parts[5] ? { ...run, cancel_requested: 1 } : run); data = runs[0];
      }
      else if (path.endsWith("/bot-runs")) data = [];
      else if (path.endsWith("/agents")) data = [];
      else if (path.endsWith("/goal")) data = null;
      await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(data) });
    });
    await page.goto(server.resolvedUrls.local[0] + "organization.html");
    const article = id => page.locator(`article[data-message-id="${id}"]`);
    await article(answer.id).waitFor();

    // The answer points at its prompt, even with a teammate's message between them, and the prompt counts the reply.
    assert.match(await article(answer.id).locator(".org-reply-pointer").innerText(), /You\s+Survey what is known about higher regulators\./);
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

    // In the Thread view, alpha's square plus sits at the left of the box and a chosen picture waits as a chip above it.
    await article(prompt.id).locator(".org-thread-count").click();
    const dialog = page.getByRole("dialog", { name: "Thread", exact: true });
    await dialog.getByText("3 messages", { exact: true }).waitFor();
    const reply = dialog.getByLabel("Reply in thread", { exact: true });
    const plus = dialog.getByRole("button", { name: "Attach pictures, PDFs or Markdown", exact: true });
    const [plusBox, replyBox] = await Promise.all([plus.boundingBox(), reply.boundingBox()]);
    assert.equal(plusBox.width, 28); assert.equal(plusBox.height, 28);
    assert.ok(plusBox.x + plusBox.width <= replyBox.x, "the plus is left of the text");
    await dialog.locator('input[type="file"]').setInputFiles({ name: "k3.png", mimeType: "image/png", buffer: png });
    const chip = dialog.getByRole("list", { name: "Attachments to send", exact: true }).getByRole("listitem");
    await chip.filter({ hasText: "k3.png" }).waitFor();
    await dialog.getByRole("button", { name: "Send reply", exact: true }).waitFor();

    // A reply written in the thread carries the picture and asks the model too.
    await reply.fill("And for K3 surfaces?");
    await reply.press("Enter");
    await dialog.getByText("4 messages", { exact: true }).waitFor();
    assert.equal(posts.at(-1).content, `![k3.png](${picture})\n\nAnd for K3 surfaces?`);
    assert.equal(await chip.count(), 0, "the chips clear once sent");
    for (let wait = 0; wait < 50 && asked.length < 2; wait++) await page.waitForTimeout(100);
    assert.equal(asked.length, 2);
    const k3 = messages.at(-1);
    assert.equal(asked[1].through_sequence, k3.sequence);

    // While the model works, the Thread view shows a trimmed card: model, Stop, and the step it is on.
    runs = [{ id: "run-k3", requested_by: user, model: "fixture/reasoner", status: "running", context_through_sequence: k3.sequence,
      created_at: 1_800_000_900, started_at: 1_800_000_900, cancel_requested: 0, message_id: null, output_text: "", accounting_state: "reserved",
      actual_microusd: null, reserved_microusd: 10, additions: [],
      activity: [{ sequence: 1, phase: "round", round: 1 }, { sequence: 2, phase: "tool_requested", tool: "arxiv_search", detail: "searched arXiv for “K3 regulator”" }] }];
    const working = dialog.locator('.org-thread-working[data-run-id="run-k3"]');
    await working.waitFor({ timeout: 15000 });
    assert.match(await working.getByRole("status", { name: "Reply progress" }).innerText(), /searched arXiv for “K3 regulator”…$/);
    assert.match(await working.locator("header").innerText(), /fixture\/reasoner\s+working/);

    // "+ Add" beside Stop puts the thread's box into adding mode; the next message joins the running reply and waits in its card.
    const line = working.getByRole("button", { name: "Add to this reply", exact: true });
    assert.equal(await line.innerText(), "+ Add");
    const [addBox, stopBox, headerBox] = await Promise.all([line.boundingBox(),
      working.getByRole("button", { name: "Stop this reply", exact: true }).boundingBox(), working.locator("header").boundingBox()]);
    assert.ok(addBox.x + addBox.width <= stopBox.x && Math.abs(stopBox.x + stopBox.width - (headerBox.x + headerBox.width)) < 1,
      "+ Add sits just left of Stop at the header's right edge");
    await line.click();
    // Adding mode has no bar: the placeholder names the reply, and the box's border, plus and return turn blue.
    const adding = dialog.locator("form.org-thread-composer-adding");
    await adding.waitFor();
    assert.equal(await adding.locator(".org-thread-adding-bar").count(), 0);
    assert.equal(await reply.getAttribute("placeholder"), "Add to reasoner's reply…");
    assert.equal(await page.evaluate(() => document.activeElement?.id), "org-thread-reply", "the box takes focus");
    const blue = await adding.evaluate(form => {
      const color = element => getComputedStyle(element).color;
      return { border: getComputedStyle(form).borderTopColor, plus: color(form.querySelector(".org-upload-plus")),
        send: color(form.querySelector(".org-composer-button")) };
    });
    assert.equal(blue.plus, blue.border); assert.equal(blue.send, blue.border);
    const pressed = working.getByRole("button", { name: "Adding to this reply", exact: true });
    assert.equal(await pressed.getAttribute("aria-pressed"), "true");
    assert.equal(await pressed.innerText(), "Adding…");
    // Escape leaves adding mode, from the box or from anywhere else in the thread, and leaves the thread open.
    await reply.press("Escape");
    await adding.waitFor({ state: "detached" });
    assert.equal(await reply.getAttribute("placeholder"), "Reply in thread…");
    assert.equal(await dialog.isVisible(), true, "Escape leaves adding mode before it closes the thread");
    await line.click(); await adding.waitFor();
    await working.getByRole("button", { name: "Adding to this reply", exact: true }).focus();
    await page.keyboard.press("Escape");
    await adding.waitFor({ state: "detached" });
    assert.equal(await dialog.isVisible(), true);
    await line.click();
    await reply.fill("Skip French-language sources.");
    await reply.press("Enter");
    await adding.waitFor({ state: "detached" });
    assert.deepEqual({ adds_to: posts.at(-1).adds_to, reply_to: posts.at(-1).reply_to }, { adds_to: "run-k3", reply_to: undefined });
    const waitingLine = working.getByLabel("Messages waiting for this reply", { exact: true }).locator("li");
    await waitingLine.filter({ hasText: "Skip French-language sources." }).waitFor({ timeout: 15000 });
    assert.match(await waitingLine.innerText(), /^You · Skip French-language sources\.\s*at step \d+\s*Undo$/);
    assert.equal(asked.length, 2, "an addition asks for no reply of its own");
    await dialog.getByText("4 messages", { exact: true }).waitFor();
    await working.getByRole("button", { name: "Stop this reply", exact: true }).click();
    for (let wait = 0; wait < 50 && !cancels.length; wait++) await page.waitForTimeout(100);
    assert.deepEqual(cancels, ["run-k3"], "Stop cancels the reply");

    // When the reply finishes, its answer takes the card's place in the open thread, with its steps and usage.
    runs = [{ ...runs[0], status: "succeeded", message_id: "done" }];
    messages.push(message(user, "For K3 surfaces the regulator is injective on the transcendental part.", k3.id,
      { author_kind: "assistant", assistant_model: "fixture/reasoner", inference_run_id: "run-k3",
        steps: [{ sequence: 1, phase: "round", round: 1 }, { sequence: 3, phase: "round", round: 2 }],
        usage: { prompt_tokens: 96, completion_tokens: 70, actual_microusd: 1200 } }));
    await dialog.getByText("For K3 surfaces the regulator is injective on the transcendental part.").waitFor({ timeout: 15000 });
    await dialog.getByText("5 messages", { exact: true }).waitFor();
    await working.waitFor({ state: "detached" });
    await dialog.getByRole("button", { name: "2 steps", exact: true }).waitFor();
    assert.deepEqual(errors, []);
  } finally {
    await browser.close(); await server.close();
  }
});
