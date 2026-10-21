// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { mkdtemp, readFile, realpath, rm } from "node:fs/promises";
import { createServer } from "node:http";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { chromium } from "playwright";

const repository = fileURLToPath(new URL("../../", import.meta.url));
async function assertSingleLineReaction(message) {
  const layout = await message.getByLabel("Add reaction", { exact: true }).evaluate(button => {
    const icon = button.querySelector("svg").getBoundingClientRect(), box = button.getBoundingClientRect();
    return { icon: [icon.left, icon.top, icon.right, icon.bottom], box: [box.left, box.top, box.right, box.bottom] };
  });
  const [left, top, right, bottom] = layout.icon, [boxLeft, boxTop, boxRight, boxBottom] = layout.box;
  assert.ok(left >= boxLeft - .5 && top >= boxTop - .5 && right <= boxRight + .5 && bottom <= boxBottom + .5,
    "the reaction icon must fit inside its launcher");
}
async function until(predicate) {
  const deadline = Date.now() + 25000;
  while (Date.now() < deadline) { if (await predicate()) return; await new Promise(resolve => setTimeout(resolve, 100)); }
  throw new Error("Timed out waiting for the topic agent fixture");
}

test("ordinary beta conversation delegates, streams roles and integrates without a goal or worker enrollment", { timeout: 90000 }, async () => {
  const temporary = await realpath(await mkdtemp(path.join(os.tmpdir(), "reasoning-topic-agents-browser-")));
  const socket = net.createServer().listen(0, "127.0.0.1"); await once(socket, "listening");
  const port = socket.address().port; await new Promise(resolve => socket.close(resolve));
  const url = `http://127.0.0.1:${port}`, state = path.join(temporary, "state"), calls = [], errors = [];
  const provider = createServer(async (request, response) => {
    if (request.url.split("?")[0] === "/api/v1/models") {
      response.writeHead(200, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ data: ["fixture/model", "fixture/new"].map(id => ({ id, architecture: { input_modalities: ["text"], output_modalities: ["text"] },
        pricing: { prompt: "0.000001", completion: "0.000002" }, context_length: 200000,
        supported_parameters: ["max_completion_tokens", "tools"], top_provider: { max_completion_tokens: 8192 } })) })); return;
    }
    let raw = ""; for await (const chunk of request) raw += chunk;
    const body = JSON.parse(raw); calls.push(body);
    assert.equal(request.headers.authorization, "Bearer sk-or-unused-native-fixture");
    assert.equal(body.max_completion_tokens, undefined);
    let delta, finish_reason;
    if (calls.length === 1) {
      delta = { content: "Comparing approaches.", tool_calls: ["librarian", "critic"].map((role, index) => ({ index, id: `call-${index}`,
        type: "function", function: { name: "agent_delegate", arguments: JSON.stringify({ role, objective: `Investigate ${role}`, context: "A narrow assigned reference." }) } })) };
      finish_reason = "tool_calls";
    } else if (calls.length > 4) {
      delta = { content: "Retained comparison answer for " + body.model }; finish_reason = "stop";
    } else if (body.messages[0].content.includes("Assigned agent role:")) {
      assert.equal(JSON.stringify(body.messages).includes("Please compare two approaches"), false);
      await new Promise(resolve => setTimeout(resolve, 1200));
      delta = { content: "Scoped evidence report, not a proof certificate." }; finish_reason = "stop";
    } else if (body.messages.some(message => message.content?.includes("Delegated work has returned"))) {
      delta = { content: "Integrated comparison: the evidence is provisional." }; finish_reason = "stop";
    } else {
      assert.equal(body.tools, undefined);
      delta = { content: "Two agents are gathering evidence." }; finish_reason = "stop";
    }
    response.writeHead(200, { "Content-Type": "text/event-stream" });
    response.write("data: " + JSON.stringify({ id: "gen-fixture-" + calls.length, choices: [{ index: 0, delta, finish_reason }] }) + "\n\n");
    response.write("data: " + JSON.stringify({ choices: [], usage: { cost: "0.000008", prompt_tokens: 20, completion_tokens: 10 } }) + "\n\n");
    response.end("data: [DONE]\n\n");
  });
  provider.listen(0, "127.0.0.1"); await once(provider, "listening");
  let server, browser, logs = "";
  try {
    server = spawn(path.join(repository, ".venv/bin/python"), ["-m", "reasoning_ledger.server.cli", "--data-dir", state,
      "serve", "--public-url", url, "--port", String(port), "--frontend-dir", path.join(repository, "web/dist"),
      "--openrouter-url", `http://127.0.0.1:${provider.address().port}/api/v1`], { cwd: repository });
    server.stdout.on("data", chunk => { logs += chunk; }); server.stderr.on("data", chunk => { logs += chunk; });
    await until(async () => { try { return (await fetch(url + "/api/health")).ok; } catch { return false; } });
    const password = "native topic browser fixture password";
    const setup = await fetch(url + "/api/setup", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
      setup_token: await readFile(path.join(state, "setup-token"), "utf8"), email: "owner@example.org", name: "Owner", password,
      organization_name: "Research", transport: "bearer",
    }) });
    assert.equal(setup.status, 201); const session = await setup.json();
    const call = async (endpoint, body, method = "POST") => {
      const reply = await fetch(url + "/api" + endpoint, { method,
        headers: { "Content-Type": "application/json", Authorization: "Bearer " + session.access_token },
        body: body === undefined ? undefined : JSON.stringify(body) });
      assert.ok(reply.ok, `Fixture HTTP ${reply.status}: ${await reply.clone().text()}`); return reply.status === 204 ? null : reply.json();
    };
    const root = "/organizations/" + session.organization_id;
    await call(root + "/provider-keys", { key: "sk-or-unused-native-fixture", label: "Free fixture" });
    await call(root + "/policy", { allowed_models: ["fixture/model"], monthly_budget_microusd: 10000000 }, "PUT");
    const project = await call(root + "/projects", { name: "Evidence" });
    const channel = await call(root + "/channels", { name: "research", project_id: project.id });
    const topic = await call(root + "/topics", { name: "Native agents", kind: "beta", channel_id: channel.id });
    browser = await chromium.launch({ headless: true }); const page = await browser.newPage();
    page.on("pageerror", error => errors.push(error.message));
    await page.goto(url + "/organization.html");
    await page.getByLabel("Email", { exact: true }).fill("owner@example.org");
    await page.getByLabel("Password", { exact: true }).fill(password);
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await page.getByRole("button", { name: /^beta topic -Native agents/ }).click();
    assert.equal(await page.getByRole("link", { name: /^Work in/ }).count(), 0);
    await page.getByText("System prompt", { exact: true }).click();
    await page.getByText(/Agentic work is the default for ordinary messages/).waitFor();
    await page.getByLabel("Message", { exact: true }).fill("Please compare two approaches and consult a librarian and critic.");
    await page.getByRole("button", { name: /^(Send|Send message)$/, exact: true }).click();
    await page.locator("article.org-message").getByText("Integrated comparison: the evidence is provisional.", { exact: true }).waitFor();
    await until(async () => (await call(root + "/topics/" + topic.id + "/export?detail=raw", undefined, "GET")).agent_threads[0]?.status === "complete");
    const trace = await call(root + "/topics/" + topic.id + "/export?detail=raw", undefined, "GET");
    assert.equal(trace.goals.length, 0); assert.equal(trace.agent_tasks.length, 2);
    assert.equal(trace.agent_threads[0].status, "complete");
    assert.deepEqual(trace.agent_tasks.map(task => task.role).sort(), ["critic", "librarian"]);
    assert.equal(calls.length, 4); assert.deepEqual(errors, []);

    // Human feedback is model-attributed; a fork inherits selection but not later output.
    const final = page.locator("article.org-message").filter({ hasText: "Integrated comparison: the evidence is provisional." });
    // Activity polls must not remount markdown or erase a browser/desktop selection.
    const selectedText = await final.locator("p").first().evaluate(element => {
      window.__selectedMessageParagraph = element;
      const range = document.createRange(); range.selectNodeContents(element);
      const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
      return selection.toString();
    });
    await page.waitForTimeout(2300);
    assert.equal(await page.evaluate(() => window.getSelection().toString()), selectedText);
    assert.equal(await final.locator("p").first().evaluate(element => element === window.__selectedMessageParagraph), true,
      "polling must preserve markdown DOM nodes, including selection anchors");
    await page.evaluate(() => window.getSelection().removeAllRanges());
    await page.getByLabel("Message", { exact: true }).hover();
    assert.equal(await final.locator(".chat-reaction-chip").count(), 0);
    assert.equal(await final.getByLabel("Add reaction", { exact: true }).evaluate(element => getComputedStyle(element).opacity), "0");
    const initialBounds = await final.boundingBox();
    await final.hover();
    assert.deepEqual(await final.boundingBox(), initialBounds, "hover controls must not resize messages");
    await assertSingleLineReaction(final);
    const actionBounds = await final.getByLabel("Message actions", { exact: true }).boundingBox();
    const reactionBounds = await final.getByLabel("Add reaction", { exact: true }).boundingBox();
    const copyBounds = await final.getByLabel("Copy message", { exact: true }).boundingBox();
    assert.ok(reactionBounds.x + reactionBounds.width <= actionBounds.x, "reaction comes before fork");
    assert.ok(actionBounds.x + actionBounds.width <= copyBounds.x, "copy comes after fork");
    await page.evaluate(() => {
      Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: async text => { window.__copiedMessage = text; } } });
    });
    await final.getByLabel("Copy message", { exact: true }).click();
    assert.equal(await page.evaluate(() => window.__copiedMessage), "Integrated comparison: the evidence is provisional.");
    await page.evaluate(() => { Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined }); });
    await final.getByLabel("Copy message", { exact: true }).click();
    await final.getByRole("alert").getByText("Could not copy. Select the message text and copy it manually.", { exact: true }).waitFor();
    await page.evaluate(() => {
      Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: async text => { window.__copiedMessage = text; } } });
    });
    await final.getByLabel("Copy message", { exact: true }).click();
    assert.equal(await final.getByRole("alert").count(), 0);
    await final.hover(); await final.getByLabel("Add reaction", { exact: true }).click();
    await page.keyboard.press("ArrowDown");
    assert.equal(await page.getByRole("menuitemcheckbox", { name: "Thumbs down", exact: true }).evaluate(element => element === document.activeElement), true);
    await page.locator(".org-transcript").evaluate(element => { element.scrollTop = Math.max(0, element.scrollTop - 2); });
    await page.waitForTimeout(2300);
    assert.equal(await page.getByRole("menu", { name: "Add reaction", exact: true }).count(), 1,
      "programmatic scroll and activity polls must not dismiss an open action menu");
    assert.equal(await page.getByRole("menuitemcheckbox", { name: "Thumbs down", exact: true }).evaluate(element => element === document.activeElement), true,
      "menu repositioning must not reset keyboard focus");
    await page.keyboard.press("Escape");
    assert.equal(await page.getByRole("menu", { name: "Add reaction", exact: true }).count(), 0);
    await final.hover(); await final.getByLabel("Add reaction", { exact: true }).click();
    await page.getByLabel("Message", { exact: true }).click();
    assert.equal(await page.getByRole("menu", { name: "Add reaction", exact: true }).count(), 0);
    await final.hover(); await final.getByLabel("Add reaction", { exact: true }).click();
    await page.getByRole("menuitemcheckbox", { name: "Thumbs up", exact: true }).click();
    assert.equal(await page.getByRole("menu", { name: "Add reaction", exact: true }).count(), 0, "the picker closes immediately");
    await until(async () => (await call(root + "/model-feedback", undefined, "GET")).models
      .some(row => row.model === "fixture/model" && row.up === 1));
    await final.locator(".chat-reaction-pill").waitFor();
    assert.equal(await final.locator(".chat-reaction-pill .chat-reaction-chip").textContent(), "👍");
    assert.equal(await final.locator(".chat-reaction-pill .chat-reaction-chip").getAttribute("title"), "Thumbs up: Owner");
    await page.getByRole("button", { name: "Choose reply model", exact: true }).click();
    await page.getByRole("radio", { name: "fixture/new", exact: true }).click();
    const sourceId = await page.locator("article.org-message").filter({ hasText: "Please compare two approaches and consult a librarian and critic." }).getAttribute("id");
    const source = page.locator("#" + sourceId);
    await source.hover(); await source.getByLabel("Message actions", { exact: true }).click();
    await page.getByRole("menuitem", { name: "Edit message", exact: true }).click();
    await source.getByLabel("Edit message text").fill("Corrected prompt: compare the approaches.");
    await source.getByRole("button", { name: "Save edit", exact: true }).click();
    await source.getByText("Corrected prompt: compare the approaches.", { exact: true }).waitFor();
    assert.equal(calls.length, 4, "saving a prompt must not automatically spend on inference");
    await source.hover(); await source.getByLabel("Message actions", { exact: true }).click();
    await page.getByRole("menuitem", { name: "Version history", exact: true }).click();
    await source.getByLabel("Message version history").getByText("Please compare two approaches and consult a librarian and critic.", { exact: true }).waitFor();
    await source.getByRole("button", { name: "Close history", exact: true }).click();
    const revisedSource = page.locator("article.org-message").filter({ hasText: "Corrected prompt: compare the approaches." });
    await revisedSource.evaluate(node => node.scrollIntoView({ block: "center", inline: "nearest" }));
    await revisedSource.hover();
    assert.equal(await revisedSource.getByLabel("Message actions", { exact: true }).evaluate(button => {
      const box = button.getBoundingClientRect();
      return button.contains(document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2));
    }), true, "the message header must not intercept its action controls after history collapses");
    await revisedSource.hover(); await revisedSource.getByLabel("Message actions", { exact: true }).click();
    await page.getByRole("menuitem", { name: "Fork here", exact: true }).click();
    await revisedSource.getByLabel("New beta-topic name").fill("Forked comparison");
    await revisedSource.getByRole("button", { name: "Create fork", exact: true }).click();
    await page.getByText("Forked from", { exact: false }).waitFor();
    await until(async () => (await page.locator("article.org-message").count()) === 1);
    assert.equal(await page.locator("article.org-message").count(), 1);
    assert.equal(await page.locator("article.org-message").filter({ hasText: "Integrated comparison" }).count(), 0);
    assert.ok((await page.getByRole("button", { name: "Choose reply model", exact: true }).textContent()).includes("fixture/new"));
    assert.equal(calls.length, 4, "reaction and fork must not create paid-model intents");
    await page.reload();
    await page.getByText("Forked from", { exact: false }).waitFor();
    assert.ok((await page.getByRole("button", { name: "Choose reply model", exact: true }).textContent()).includes("fixture/new"));

    // Existing keys admit a newly available router; retained comparisons show the full question.
    const gamma = await call(root + "/topics", { name: "Retained comparisons", kind: "gamma", channel_id: channel.id });
    const question = "A full comparison question\n" + "Explain the assumptions and all relevant cases. ".repeat(35) + "FULL_QUESTION_END";
    const definition = { format: 1, name: "Retained model comparison", nodes: ["fixture/model", "fixture/new"].map((model, index) => ({
      id: "answer-" + index, kind: "llm", depends_on: [], inputs: {}, parameters: { model, prompt: question, max_completion_tokens: null },
    })) };
    const workflow = await call(root + "/topics/" + gamma.id + "/workflows", { client_id: "retained-compare-definition", definition });
    const started = await call(root + "/workflows/" + workflow.workflow_id + "/runs", { client_id: "retained-compare-start", version: workflow.version });
    await until(async () => (await call(root + "/workflow-runs/" + started.id, undefined, "GET")).status === "succeeded");
    await page.getByRole("button", { name: /^gamma topic -Retained comparisons/ }).waitFor();
    await page.getByRole("button", { name: /^gamma topic -Retained comparisons/ }).click();
    const comparison = page.locator(".org-comparison-message");
    await comparison.getByText("Retained comparison answer for fixture/new", { exact: true }).waitFor();
    const summary = comparison.locator(":scope > summary"), full = summary.locator(".org-comparison-question");
    assert.equal(await full.textContent(), question);
    if (await comparison.evaluate(element => element.open)) await summary.click();
    assert.equal(await comparison.evaluate(element => element.open), false);
    assert.equal(await full.textContent(), question);
    assert.ok(await full.evaluate(element => element.scrollHeight <= element.clientHeight + 1));
    assert.ok((await full.boundingBox()).height > 40, "the long collapsed question must occupy visible lines");
    assert.ok((await summary.textContent()).includes("fixture/new"));
    await summary.click();
    assert.equal(await comparison.getByText(/Workflow state events|revision [0-9]|Run again|succeeded/).count(), 0);
    const answer = comparison.locator(".org-comparison-answer").last();
    await answer.hover();
    await assertSingleLineReaction(answer);
    const gammaReaction = await answer.getByLabel("Add reaction", { exact: true }).boundingBox();
    const gammaCopy = await answer.getByLabel("Copy message", { exact: true }).boundingBox();
    assert.ok(gammaReaction.x + gammaReaction.width <= gammaCopy.x);
    await page.evaluate(() => {
      Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: async text => { window.__copiedMessage = text; } } });
    });
    await answer.getByLabel("Copy message", { exact: true }).click();
    assert.equal(await page.evaluate(() => window.__copiedMessage), "Retained comparison answer for fixture/new");
    await answer.getByLabel("Add reaction", { exact: true }).click();
    await page.getByRole("menuitem", { name: "More reactions", exact: true }).click();
    await page.getByRole("menuitemcheckbox", { name: "Upside-down smile", exact: true }).click();
    await until(async () => (await call(root + "/model-feedback", undefined, "GET")).models
      .some(row => row.model === "fixture/new" && row.upside_down === 1));
    assert.equal(calls.length, 6); assert.deepEqual(errors, []);
    if (process.env.REASONING_SCREENSHOT_DIR) await page.screenshot({ path: path.join(process.env.REASONING_SCREENSHOT_DIR, "gamma-reactions.png") });

    const mobileContext = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
    try {
      await mobileContext.addCookies(await page.context().cookies());
      const mobile = await mobileContext.newPage(); mobile.on("pageerror", error => errors.push(error.message));
      await mobile.goto(url + `/organization.html?organization=${session.organization_id}&topic=${topic.id}`);
      const mobileAnswer = mobile.locator("article.org-message").filter({ hasText: "Integrated comparison: the evidence is provisional." });
      await mobileAnswer.waitFor();
      await assertSingleLineReaction(mobileAnswer);
      await mobileAnswer.getByLabel("Add reaction", { exact: true }).tap();
      const menu = mobile.getByRole("menu", { name: "Add reaction", exact: true });
      const box = await menu.boundingBox();
      assert.ok(box.x >= 0 && box.x + box.width <= 390 && box.y >= 0 && box.y + box.height <= 844);
      await mobile.getByRole("menuitemcheckbox", { name: "Thumbs down", exact: true }).tap();
      await mobileAnswer.getByRole("button", { name: "Thumbs down: Owner", exact: true }).waitFor();
      assert.equal(await mobileAnswer.getByRole("button", { name: "Thumbs up: Owner", exact: true }).count(), 0, "changing a reaction replaces the prior emoji");
      const after = await call(root + "/model-feedback", undefined, "GET");
      assert.ok(after.models.some(row => row.model === "fixture/model" && row.up === 0 && row.down === 1));
      await mobileAnswer.getByLabel("Add reaction", { exact: true }).tap();
      await mobile.route("**/reactions/message/*", route => route.request().method() === "PUT"
        ? route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "Fixture reaction refused" }) }) : route.continue());
      await mobile.getByRole("menuitem", { name: "More reactions", exact: true }).tap();
      await mobile.getByRole("menuitemcheckbox", { name: "Upside-down smile", exact: true }).tap();
      assert.equal(await mobile.getByRole("menu", { name: "Add reaction", exact: true }).count(), 0, "refused reactions still close the picker immediately");
      await mobileAnswer.getByRole("alert").waitFor();
      await mobile.unroute("**/reactions/message/*");
      await mobile.getByLabel("Toggle project navigation", { exact: true }).tap();
      const topicRow = mobile.getByRole("button", { name: /^beta topic -Native agents/ });
      const topicBox = await topicRow.boundingBox();
      const touch = await mobileContext.newCDPSession(mobile);
      await touch.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x: topicBox.x + 40, y: topicBox.y + topicBox.height / 2 }] });
      await mobile.waitForTimeout(550);
      await touch.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
      const inlineSpend = mobile.locator(".org-topic .org-inline-spend[data-visible=true]");
      const preview = await inlineSpend.count() ? inlineSpend : mobile.locator(".org-navigation-spend");
      assert.match(await preview.innerText(), /\$/);
      assert.equal(await mobile.getByRole("navigation", { name: "Projects and channels" }).isVisible(), true, "holding must not navigate and close the pane");
      await mobile.keyboard.press("Enter");
      assert.equal(await mobile.getByRole("navigation", { name: "Projects and channels" }).isVisible(), false, "keyboard activation after a touch preview must still navigate");
      assert.ok(await mobile.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      if (process.env.REASONING_SCREENSHOT_DIR) await mobile.screenshot({ path: path.join(process.env.REASONING_SCREENSHOT_DIR, "mobile-reactions.png") });
      assert.equal(calls.length, 6, "touch feedback must not create inference calls"); assert.deepEqual(errors, []);
    } finally { await mobileContext.close(); }

    // Standalone bot inference participates in the same beta/gamma stop pair.
    // These are synthetic leased receipts, not CLI/model invocations.
    const connectedBot = await call(root + "/my-bots", { name: "Playback fixture", topic_ids: [topic.id, gamma.id],
      scopes: ["messages:read", "messages:write"], lifetime_seconds: 3600 });
    const botCall = async (endpoint, body) => {
      const reply = await fetch(url + "/api/bot" + endpoint, { method: "POST", headers: {
        "Content-Type": "application/json", Authorization: "Bearer " + connectedBot.token,
      }, body: JSON.stringify(body) });
      return reply;
    };
    assert.equal((await botCall("/runtime", { engine: "codex", models: ["default"], local_tools: false })).status, 200);
    await page.goto(url + `/organization.html?organization=${session.organization_id}&topic=${topic.id}`);
    const queuedBot = await call(root + "/topics/" + topic.id + "/bot-runs", {
      model: `bot/${connectedBot.id}/default`, client_id: "playback-queued", through_sequence: trace.messages[0].sequence,
    });
    const stopButton = page.getByRole("button", { name: "Stop current work", exact: true });
    await until(() => stopButton.isEnabled()); await stopButton.click();
    await until(async () => (await call(root + "/bot-runs/" + queuedBot.id, undefined, "GET")).status === "cancelled");
    assert.equal(await page.getByRole("button", { name: /Cancel bot|Stop bot|Pause agents/ }).count(), 0);
    await page.goto(url + `/organization.html?organization=${session.organization_id}&topic=${gamma.id}`);
    const gammaMessage = await call(root + "/topics/" + gamma.id + "/messages", { content: "A synthetic local invocation", client_id: "bot-stop-question" });
    const runningBot = await call(root + "/topics/" + gamma.id + "/bot-runs", {
      model: `bot/${connectedBot.id}/default`, client_id: "playback-running", through_sequence: gammaMessage.sequence,
    });
    const claimResponse = await botCall("/runs/claim", {}); assert.equal(claimResponse.status, 200);
    const claimedBot = await claimResponse.json(); assert.equal(claimedBot.id, runningBot.id);
    await until(() => stopButton.isEnabled()); await stopButton.click();
    await until(async () => (await call(root + "/bot-runs/" + runningBot.id, undefined, "GET")).status === "outcome_unknown");
    assert.equal((await botCall("/runs/" + runningBot.id + "/progress", { lease: claimedBot.lease, phase: "thinking" })).status, 409);
    assert.equal(await page.locator(".org-transport-controls").count(), 1, "one transport pair covers workflow and standalone bot activity");
    assert.equal(calls.length, 6, "bot stop and local synthetic receipts must not create OpenRouter requests");
    await page.goto(url + `/organization.html?organization=${session.organization_id}&topic=${topic.id}`);
    const corrected = page.locator("article.org-message").filter({ hasText: "Corrected prompt: compare the approaches." });
    await corrected.hover(); await corrected.getByLabel("Message actions", { exact: true }).click();
    await page.getByRole("menuitem", { name: "Reply again with selected model", exact: true }).click();
    await until(() => calls.length === 7);
    assert.equal(calls[6].model, "fixture/new");
    assert.ok(JSON.stringify(calls[6].messages).includes("Corrected prompt: compare the approaches."));
    assert.equal(JSON.stringify(calls[6].messages).includes("Please compare two approaches and consult a librarian and critic."), false);
    assert.deepEqual(errors, []);
  } catch (error) { error.message += "\nLocal fixture logs:\n" + logs; throw error; }
  finally {
    await browser?.close(); provider.closeAllConnections(); await new Promise(resolve => provider.close(resolve));
    if (server && server.exitCode === null && server.signalCode === null) {
      server.kill("SIGTERM"); await Promise.race([once(server, "exit"), new Promise(resolve => setTimeout(resolve, 5000))]);
      if (server.exitCode === null && server.signalCode === null) { server.kill("SIGKILL"); await once(server, "exit"); }
    }
    await rm(temporary, { recursive: true, force: true });
  }
});
