// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { realpath } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

const user = "a".repeat(32), deven = "d".repeat(32), org = "b".repeat(32), topic = "c".repeat(32);
const model = { id: "fixture/reasoner", name: "Reasoner", supports_tools: true, supports_reasoning: true, reasoning_efforts: [],
  supported_parameters: [], input_modalities: [], output_modalities: [], max_completion_tokens: 4096 };

/** A fixture server for one beta topic whose side chat starts open; it records layout saves, open choices and read marks. */
async function fixture(browser, server, viewport, layout) {
  const page = await browser.newPage({ viewport }), errors = [], saved = [], opened = [], read = [];
  let sequence = 0;
  const post = (author_id, content) => ({ id: String(++sequence).padStart(32, "0"), sequence, topic_id: topic, author_id, author_kind: "human",
    content, client_id: "seed-" + sequence, created_at: 1_800_000_000 + sequence * 60, revision: 0, reply_to: null });
  const main = [post(user, "Do we know the motivic case?"), post(deven, "Is the Borel regulator injective here?")];
  const side = [{ id: "f".repeat(32), sequence: 1, topic_id: topic, owner_id: user, author_kind: "human", content: "Which regulator do we mean?",
    client_id: "side-1", created_at: 1_800_000_100, assistant_model: null, inference_run_id: null, reply_to: null, brought: null }];
  page.on("pageerror", error => errors.push(error.message));
  await page.route("**/api/**", async route => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname, body = request.postDataJSON();
    let data = [], status = 200;
    if (path === "/api/health") data = { initialized: true };
    else if (path === "/api/auth/me") data = { id: user, name: "Elden Ring", email: "elden@example.org" };
    else if (path === "/api/organizations") data = [{ id: org, name: "Testing", role: "owner" }];
    else if (path === "/api/auth/model-favorites") data = { user_id: user, models: [] };
    else if (path === "/api/auth/side-chat") { if (request.method() === "PUT") { saved.push(body); layout = body; } data = layout; }
    else if (path.endsWith("/hierarchy")) data = { projects: [{ id: "project", name: "Testing", visibility: "organization" }],
      channels: [{ id: "channel", name: "research", project_id: "project" }],
      topics: [{ id: topic, name: "Regulators", channel_id: "channel", kind: "beta", default_model: model.id, knowledge_tools: ["ledger"] }],
      topic_state: [{ topic_id: topic, subscribed: true, last_read_sequence: 2, last_message_sequence: main.length, unread_count: 0,
        unread_mention_count: 0, side_chat_open: true }] };
    else if (path.endsWith("/members")) data = [{ id: user, name: "Elden Ring", role: "owner" }, { id: deven, name: "Deven", role: "member" }];
    else if (path.endsWith("/presence")) data = { humans: [], bots: [] };
    else if (path.endsWith("/inference-configuration")) data = { allowed_models: [model.id], tools: [] };
    else if (path.endsWith("/model-prices")) data = { models: [model], allowed_models: [model.id] };
    else if (path.endsWith("/research-instructions")) data = { system_prompt: "Research safely.", prompt_version: "fixture" };
    else if (path.endsWith("/research-settings")) data = { tools: ["history"], web_search_configured: false, current_date: "2026-11-22" };
    else if (path.endsWith("/compute-configuration")) data = { images: [], policy: { allowed_images: [] } };
    else if (path.endsWith("/navigation-usage")) data = { projects: {}, channels: {}, topics: {} };
    else if (path.endsWith("/side-chat") && request.method() === "PUT") { opened.push(body.open); status = 204; data = null; }
    else if (path.endsWith("/side-chat")) data = { messages: side, runs: [] };
    else if (path.endsWith("/read")) { read.push(body.sequence); status = 204; data = null; }
    else if (path.endsWith("/messages") && request.method() === "GET") {
      const after = Number(url.searchParams.get("after") ?? 0);
      data = main.filter(message => message.sequence > after);
    } else if (path.endsWith("/message-updates")) data = { after: 0, messages: [] };
    else if (path.includes("/reactions/")) data = { counts: { up: 0, down: 0, upside_down: 0 }, mine: [] };
    else if (path.endsWith("/inference-runs") || path.endsWith("/bot-runs") || path.endsWith("/agents")) data = [];
    else if (path.endsWith("/goal")) data = null;
    await route.fulfill({ status, contentType: "application/json", body: status === 204 ? "" : JSON.stringify(data) });
  });
  await page.goto(server.resolvedUrls.local[0] + `organization.html?organization=${org}&topic=${topic}`);
  // Saves go one at a time, so the newest may still be on its way when the page already shows it.
  const savedAs = async check => {
    for (let wait = 0; wait < 50 && !(saved.length && check(saved.at(-1))); wait++) await page.waitForTimeout(100);
    assert.ok(saved.length && check(saved.at(-1)), "saved: " + JSON.stringify(saved.at(-1)));
  };
  return { page, errors, saved, savedAs, opened, read, colleaguePosts: content => main.push(post(deven, content)) };
}

async function withServer(run) {
  const root = fileURLToPath(new URL("../", import.meta.url));
  const server = await createServer({ root, server: { host: "127.0.0.1", port: 0, fs: { allow: [root, await realpath(root + "node_modules")] } } });
  await server.listen(); const browser = await chromium.launch({ headless: true });
  try { await run(server, browser); } finally { await browser.close(); await server.close(); }
}

test("the side chat opens where it was left open, drags to a width, expands over the main chat, floats, and saves each layout",
  { timeout: 90000 }, () => withServer(async (server, browser) => {
  const { page, errors, savedAs, opened, read, colleaguePosts } = await fixture(browser, server, { width: 1440, height: 900 },
    { desktop: { mode: "side", width: 0.5, floating: null }, phone: { height: 0.6 } });
  // Open in this topic, without a click, at the saved half of the conversation area.
  const panel = page.getByRole("complementary", { name: "Side chat", exact: true });
  await panel.getByText("Which regulator do we mean?", { exact: true }).waitFor();
  const row = page.locator(".org-conversation-row");
  const area = (await row.boundingBox()).width;
  assert.ok(Math.abs((await panel.boundingBox()).width - Math.round(area / 2)) <= 1, "half of the conversation area");
  assert.equal(await page.getByRole("button", { name: "Side chat", exact: true }).getAttribute("aria-expanded"), "true");

  // The left edge is a separator: keys move it, and each change is saved to the account.
  const edge = page.getByRole("separator", { name: "Side chat width" });
  const percent = width => `${Math.round(width / area * 100)} percent of the conversation area`;
  assert.equal(await edge.getAttribute("aria-valuetext"), percent(Math.round(area / 2)));
  await edge.focus();
  await page.keyboard.press("Shift+ArrowLeft");
  const wider = Math.round((await panel.boundingBox()).width);
  assert.equal(wider, Math.round(area / 2) + Math.round(area / 10), "Shift moves a tenth of the area");
  await page.keyboard.press("Home");
  assert.equal(Math.round((await panel.boundingBox()).width), 360);
  await page.keyboard.press("Enter");
  assert.equal(Math.round((await panel.boundingBox()).width), wider, "Enter goes back to the last wider width");

  // Dragging draws no guides or labels, and goes all the way: past the main chat's floor the side chat slides over it,
  // and at the far end it takes the whole conversation area, which is Full.
  const middleOf = box => ({ x: box.x + 5, y: box.y + box.height / 2 });
  let start = middleOf(await edge.boundingBox());
  const rowBox = await row.boundingBox();
  await page.mouse.move(start.x, start.y);
  await page.mouse.down();
  await page.mouse.move(rowBox.x + 200, start.y, { steps: 10 });
  assert.equal(await page.locator(".org-side-guides, .org-side-tip").count(), 0, "nothing is drawn on top while dragging");
  assert.ok(await page.locator(".org-conversation-row.org-side-over-row").count(), "the side chat slides over the main chat");
  assert.equal(Math.round((await page.locator("section.org-conversation").boundingBox()).width), 320, "the main chat keeps its floor under it");
  await page.mouse.move(rowBox.x - 40, start.y, { steps: 6 });
  await page.mouse.up();
  await panel.getByRole("button", { name: "Show main chat", exact: true }).waitFor();
  assert.equal(await page.locator("section.org-conversation").isVisible(), false, "the main chat is hidden");
  assert.equal(await edge.getAttribute("aria-valuetext"), "Whole conversation area; the main chat is hidden");
  await savedAs(layout => layout.desktop.mode === "full");
  // From Full the edge drags back; let go halfway beside the main chat, it stays there.
  start = middleOf(await edge.boundingBox());
  await page.mouse.move(start.x, start.y);
  await page.mouse.down();
  await page.mouse.move(rowBox.x + area / 2, start.y, { steps: 10 });
  await page.mouse.up();
  await page.locator("section.org-conversation").waitFor();
  assert.ok(Math.abs((await panel.boundingBox()).width - area / 2) <= 2, "back beside the main chat, at half the area");
  await savedAs(layout => layout.desktop.mode === "side" && Math.abs(layout.desktop.width - 0.5) < 0.003);
  // Let go early in the slide-over, it settles at the main chat's floor.
  start = middleOf(await edge.boundingBox());
  await page.mouse.move(start.x, start.y);
  await page.mouse.down();
  await page.mouse.move(rowBox.x + 300, start.y, { steps: 10 });
  await page.mouse.up();
  assert.ok(Math.abs((await page.locator("section.org-conversation").boundingBox()).width - 320) <= 1, "the main chat keeps 320 px");
  assert.equal(await page.locator(".org-side-over-row").count(), 0);
  await edge.focus();
  await page.keyboard.press("Home");
  await page.keyboard.press("End");
  await panel.getByRole("button", { name: "Show main chat", exact: true }).waitFor();
  await page.keyboard.press("Escape");
  await page.locator("section.org-conversation").waitFor();
  await savedAs(layout => layout.desktop.mode === "side");

  // Expand hides the main chat but keeps the project list; the same button now says “Show main chat” and counts what others post.
  await panel.getByRole("button", { name: "Expand: hide the main chat" }).click();
  await panel.getByRole("button", { name: "Show main chat", exact: true }).waitFor();
  assert.equal(await page.locator("section.org-conversation").isVisible(), false, "the main chat is hidden");
  assert.ok(await page.locator("aside.org-sidebar").isVisible(), "the project list stays");
  await savedAs(layout => layout.desktop.mode === "full");
  const readBefore = read.length;
  colleaguePosts("Borel says yes for number fields.");
  await panel.getByRole("button", { name: "Show main chat, 1 new message" }).waitFor({ timeout: 10000 });
  assert.equal(read.filter(sequence => sequence >= 3).length, 0, "nothing in the hidden main chat is marked read");
  // Esc brings the main chat back, where the new message is read.
  await page.keyboard.press("Escape");
  await page.getByText("Borel says yes for number fields.", { exact: true }).waitFor();
  assert.equal(await panel.getByRole("button", { name: "Expand: hide the main chat" }).evaluate(element => element === document.activeElement), true);
  await savedAs(layout => layout.desktop.mode === "side");
  for (let wait = 0; wait < 50 && !read.includes(3); wait++) await page.waitForTimeout(100);
  assert.ok(read.slice(readBefore).includes(3), "shown again, the new message is marked read");

  // Float lifts it over the topic as a non-modal dialog; the move button moves it by keys; Dock puts it back.
  await panel.getByRole("button", { name: "Float the side chat over the topic" }).click();
  const floating = page.getByRole("dialog", { name: "Side chat" });
  await floating.waitFor();
  assert.equal(await floating.evaluate(element => getComputedStyle(element).position), "absolute");
  assert.equal(await page.locator("section.org-conversation").isVisible(), true, "the topic keeps its full width");
  const before = await floating.boundingBox();
  await floating.getByRole("button", { name: "Move or resize the side chat" }).focus();
  await page.keyboard.press("ArrowLeft");
  assert.equal(Math.round((await floating.boundingBox()).x), Math.round(before.x - 24));
  await savedAs(layout => layout.desktop.mode === "float" && layout.desktop.floating.w > 0);
  await floating.getByRole("button", { name: "Dock the side chat to the right" }).click();
  await page.getByRole("separator", { name: "Side chat width" }).waitFor();

  // Closing it is saved for this topic; the app bar button opens it again.
  await panel.getByRole("button", { name: "Close side chat" }).click();
  await panel.waitFor({ state: "detached" });
  await page.getByRole("button", { name: "Side chat", exact: true }).click();
  await panel.waitFor();
  assert.deepEqual(opened, [false, true]);
  assert.deepEqual(errors, []);
}));

test("on a phone the side chat is a sheet whose grab bar sets its height, and full screen offers “Show main chat”",
  { timeout: 90000 }, () => withServer(async (server, browser) => {
  const { page, errors, savedAs } = await fixture(browser, server, { width: 390, height: 844 },
    { desktop: { mode: "float", width: null, floating: null }, phone: { height: 0.6 } });
  const panel = page.getByRole("complementary", { name: "Side chat", exact: true });
  await panel.getByText("Which regulator do we mean?", { exact: true }).waitFor();
  assert.equal(await panel.getByRole("button", { name: /Float/ }).count(), 0, "no floating on a phone");
  const box = await panel.boundingBox();
  assert.equal(Math.round(box.x), 0);
  assert.equal(Math.round(box.y + box.height), 844, "a sheet from the bottom");
  assert.ok(Math.abs(box.height - 0.6 * (844 - 44)) <= 2);
  await panel.getByRole("button", { name: "Full screen" }).click();
  await panel.getByRole("button", { name: "Show main chat" }).waitFor();
  assert.ok(Math.abs((await panel.boundingBox()).height - 800) <= 2);
  await savedAs(layout => layout.phone.height === 1);
  const grab = panel.getByRole("separator", { name: "Side chat height" });
  await grab.focus();
  await page.keyboard.press("ArrowDown");
  assert.equal(await grab.getAttribute("aria-valuetext"), "60 percent of the screen");
  await page.keyboard.press("Home");
  assert.equal(await grab.getAttribute("aria-valuetext"), "36 percent of the screen");
  await savedAs(layout => layout.phone.height === 0.36 && layout.desktop.mode === "float");
  assert.deepEqual(errors, []);
}));
