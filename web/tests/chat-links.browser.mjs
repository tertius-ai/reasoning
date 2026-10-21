// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { realpath } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { join } from "node:path";
import { test } from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

test("message links land on the message in long topics and # references stay access-checked", { timeout: 90000 }, async () => {
  const root = fileURLToPath(new URL("../", import.meta.url));
  const server = await createServer({ root, server: { host: "127.0.0.1", port: 0, fs: { allow: [root, await realpath(root + "node_modules")] } } });
  await server.listen();
  const browser = await chromium.launch({ headless: true });
  try {
    const context = await browser.newContext({ viewport: { width: 1200, height: 800 } });
    await context.addInitScript(() => {
      Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: async text => { window.__copied = text; } } });
    });
    const user = "a".repeat(32), org = "org", long = "1".repeat(32), results = "2".repeat(32), project = "3".repeat(32), hidden = "4".repeat(32);
    const names = { [long]: "Long" };
    const hierarchy = () => ({ projects: [{ id: project, name: "Algebra", visibility: "organization" }],
      channels: [{ id: "channel", name: "research", project_id: project }],
      topics: [{ id: long, name: names[long], channel_id: "channel", kind: "alpha", default_model: null, knowledge_tools: [] },
        { id: results, name: "Results", channel_id: "channel", kind: "alpha", default_model: null, knowledge_tools: [] }], topic_state: [] });
    let sequence = 0;
    const message = (topic, content) => ({ id: (++sequence).toString(16).padStart(32, "0"), sequence, topic_id: topic, author_id: user,
      author_kind: "human", content, client_id: "seed-" + sequence, created_at: 1_800_000_000 + sequence * 600, revision: 0 });
    const messages = [];
    for (let index = 0; index < 300; index++) messages.push(message(long, `Message ${index}`));
    messages.push(message(results, `Earlier work is in #[topic:${hidden}].`));
    const writes = [], contexts = [], resolutions = [], latest = [];
    await context.route("**/api/**", async route => {
      const request = route.request(), url = new URL(request.url()), path = url.pathname, parts = path.split("/");
      if (request.method() !== "GET") writes.push({ path, body: request.postDataJSON() });
      let status = 200, data = [];
      const topicMessages = topic => messages.filter(item => item.topic_id === topic);
      if (path === "/api/health") data = { initialized: true };
      else if (path === "/api/auth/me") data = { id: user, name: "Alice", email: "alice@example.org" };
      else if (path === "/api/organizations") data = [{ id: org, name: "Research", role: "member" }];
      else if (path.endsWith("/hierarchy")) data = hierarchy();
      else if (path.endsWith("/members")) data = [{ id: user, name: "Alice", email: "alice@example.org", role: "member" }];
      else if (path.endsWith("/presence")) data = { humans: [], bots: [] };
      else if (path.endsWith("/navigation-usage")) data = { projects: {}, channels: {}, topics: {} };
      else if (path.endsWith("/message-updates")) data = { after: 0, messages: [] };
      else if (path.includes("/reactions/")) data = {};
      else if (path.endsWith("/references")) {
        // The server resolves only targets the reader can open; the hidden topic is not one of them.
        resolutions.push(url.search);
        const topics = (url.searchParams.get("topics") ?? "").split(",").filter(Boolean);
        const projects = (url.searchParams.get("projects") ?? "").split(",").filter(Boolean);
        data = { topics: Object.fromEntries(topics.map(id => [id, hierarchy().topics.some(item => item.id === id)
          ? { id, name: hierarchy().topics.find(item => item.id === id).name, kind: "alpha", channel_id: "channel", channel_name: "research",
            project_id: project, project_name: "Algebra" } : null])),
          projects: Object.fromEntries(projects.map(id => [id, id === project ? { id, name: "Algebra" } : null])) };
      } else if (parts[6] === "messages" && parts[8] === "context") {
        contexts.push(parts[7]);
        const list = topicMessages(parts[5]), index = list.findIndex(item => item.id === parts[7]);
        if (index < 0) { status = 404; data = { detail: "not found" }; }
        else {
          const limit = Number(url.searchParams.get("limit")), earlier = list.slice(0, index + 1), later = list.slice(index + 1);
          const before = Math.min(earlier.length, Math.max(Math.floor(limit / 2), limit - later.length));
          data = { message_id: parts[7], messages: [...earlier.slice(earlier.length - before), ...later.slice(0, limit - before)],
            older: earlier.length > before };
        }
      } else if (parts[6] === "messages" && parts.length === 7 && request.method() === "POST") {
        const body = request.postDataJSON(), posted = { ...message(parts[5], body.content), client_id: body.client_id };
        messages.push(posted); data = posted; status = 201;
      } else if (parts[6] === "messages" && parts.length === 7) {
        const list = topicMessages(parts[5]), after = Number(url.searchParams.get("after") ?? 0), before = url.searchParams.get("before");
        if (before) latest.push(parts[5]);
        data = before ? list.filter(item => item.sequence < Number(before)).slice(-Number(url.searchParams.get("limit")))
          : list.filter(item => item.sequence > after).slice(0, Number(url.searchParams.get("limit")));
      }
      await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(data) });
    });
    const page = await context.newPage(), errors = [];
    page.on("pageerror", error => errors.push(error.message));
    const base = server.resolvedUrls.local[0] + "organization.html";
    const target = messages[200];

    await page.goto(`${base}?organization=${org}&topic=${long}&message=${target.id}`);
    const linked = page.locator("article.org-message-linked");
    await linked.waitFor();
    assert.match(await linked.innerText(), /Message 200/);
    assert.equal(await page.evaluate(() => document.activeElement?.dataset.messageId), target.id, "the linked message receives focus");
    const [box, view] = await Promise.all([linked.boundingBox(), page.getByLabel("Message history", { exact: true }).boundingBox()]);
    assert.ok(box.y >= view.y && box.y + box.height <= view.y + view.height, "the linked message is scrolled into view");
    // Development StrictMode mounts effects twice; every request is for the linked message.
    assert.ok(contexts.length > 0 && contexts.every(id => id === target.id));
    assert.deepEqual(latest, [], "a message link does not load the latest page first");
    await page.getByRole("button", { name: "Load earlier messages", exact: true }).waitFor();
    if (process.env.REASONING_SCREENSHOT_DIR) await page.screenshot({ path: join(process.env.REASONING_SCREENSHOT_DIR, "message-link.png") });
    assert.equal(await page.locator("article.org-message").first().innerText().then(text => /Message 151\b/.test(text)), true);
    assert.ok(!writes.some(write => write.path.endsWith("/read")), "opening a link does not acknowledge unseen newer messages");

    await linked.hover();
    await linked.hover(); await linked.getByLabel("Message actions", { exact: true }).click();
    await page.getByRole("menuitem", { name: "Copy link", exact: true }).click();
    await linked.getByText("Link copied.", { exact: true }).waitFor();
    const copied = await page.evaluate(() => window.__copied);
    assert.equal(copied, new URL(`/organization.html?organization=${org}&topic=${long}&message=${target.id}`, base).href);

    const other = await context.newPage();
    await other.goto(copied);
    await other.locator("article.org-message-linked").waitFor();
    assert.match(await other.locator("article.org-message-linked").innerText(), /Message 200/);
    await other.close();

    await page.goto(`${base}?organization=${org}&topic=${long}&message=${"0".repeat(32)}`);
    await page.getByText("The linked message is unavailable. It may have been removed, or you may not have access to it.").waitFor();
    await page.getByText("Message 299", { exact: true }).waitFor();
    await page.goto(`${base}?organization=${org}&topic=${hidden}`);
    await page.getByText("This link is unavailable. It may point to something that was removed, or that you cannot access.").waitFor();
    assert.match(await page.locator(".org-topic-menu h1").innerText(), /Long/);
    await page.getByRole("button", { name: "Dismiss notice", exact: true }).click();

    await page.getByRole("button", { name: /^alpha topic -Results/ }).click();
    await page.locator(".org-reference-unavailable").waitFor();
    assert.equal(await page.locator(".org-reference-unavailable").innerText(), "unavailable");
    const composer = page.getByLabel("Message", { exact: true });
    // A choice restores the caret on the next animation frame; type only after it.
    const settled = () => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    await composer.click(); await composer.pressSequentially("See #Lo");
    const list = page.getByRole("listbox", { name: "Reference a topic or project" });
    await list.waitFor();
    const options = list.getByRole("option");
    assert.deepEqual(await options.allInnerTexts(), ["α-Long · Algebra / #research"]);
    assert.equal(await composer.getAttribute("aria-controls"), "org-reference-list");
    assert.equal(await composer.getAttribute("aria-activedescendant"), `org-reference-topic-${long}`);
    assert.equal(await options.first().getAttribute("aria-selected"), "true");
    await composer.press("Escape");
    assert.equal(await list.count(), 0, "Escape dismisses the menu");
    assert.equal(await composer.evaluate(element => element === document.activeElement), true);
    await composer.press("Backspace"); await composer.pressSequentially("o");
    await list.waitFor();
    await composer.press("Enter");
    assert.equal(await composer.inputValue(), "See #α-Long ");
    await settled();
    assert.equal(await composer.evaluate(element => element === document.activeElement), true, "selection keeps composer focus");
    await composer.pressSequentially("and #");
    assert.deepEqual(await options.allInnerTexts(), ["α-Long · Algebra / #research", "α-Results · Algebra / #research", "Algebra · Project"]);
    await composer.press("ArrowDown"); await composer.press("ArrowDown");
    assert.equal(await composer.getAttribute("aria-activedescendant"), `org-reference-project-${project}`);
    await composer.press("ArrowDown");
    assert.equal(await composer.getAttribute("aria-activedescendant"), `org-reference-topic-${long}`, "arrows wrap around");
    await composer.press("ArrowUp");
    await composer.press("Tab");
    assert.equal(await composer.inputValue(), "See #α-Long and #Algebra ");
    await settled();
    await composer.pressSequentially("#Hidden");
    assert.equal(await list.count(), 0, "targets outside the writer's hierarchy are not offered");
    await composer.press("Tab");
    assert.equal(await composer.evaluate(element => element === document.activeElement), true, "an unmatched reference keeps focus");
    for (let index = 0; index < "#Hidden".length; index++) await composer.press("Backspace");
    await composer.press("Enter");
    await page.waitForFunction(() => document.querySelector("#org-message")?.value === "");
    const post = writes.find(write => write.path.endsWith(`/topics/${results}/messages`));
    assert.equal(post.body.content, `See #[topic:${long}] and #[project:${project}] `, "references are stored by identifier, not name");
    const sent = page.locator("article.org-message").last();
    await sent.locator("a.org-reference", { hasText: "α-Long" }).waitFor();
    await sent.locator("a.org-reference", { hasText: "Algebra" }).waitFor();
    assert.ok(resolutions.every(query => new URLSearchParams(query).values().toArray().join(",").split(",").filter(Boolean).length <= 100));
    assert.doesNotMatch(await page.content(), /Hidden/);
    if (process.env.REASONING_SCREENSHOT_DIR) {
      await composer.pressSequentially("#"); await list.waitFor();
      await page.screenshot({ path: join(process.env.REASONING_SCREENSHOT_DIR, "references.png") });
      await composer.press("Escape"); await composer.fill("");
    }

    names[long] = "Long proofs";
    await sent.locator("a.org-reference", { hasText: "α-Long proofs" }).waitFor({ timeout: 15000 });
    assert.equal(await sent.locator("a.org-reference").first().getAttribute("href"), `/organization.html?organization=${org}&topic=${long}`);
    await sent.locator("a.org-reference", { hasText: "α-Long proofs" }).click();
    await page.waitForFunction(() => /Long proofs/.test(document.querySelector(".org-topic-menu h1")?.textContent ?? ""));
    assert.match(page.url(), new RegExp(`topic=${long}`));
    await page.getByText("Message 299", { exact: true }).waitFor();
    assert.deepEqual(errors, []);
  } finally {
    await browser.close(); await server.close();
  }
});
