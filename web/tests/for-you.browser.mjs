// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { realpath } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

test("a topic opened with unread mentions of you and replies to you steps to each from a bar at its top", { timeout: 90000 }, async () => {
  const root = fileURLToPath(new URL("../", import.meta.url));
  const server = await createServer({ root, server: { host: "127.0.0.1", port: 0, fs: { allow: [root, await realpath(root + "node_modules")] } } });
  await server.listen();
  const browser = await chromium.launch({ headless: true });
  try {
    const context = await browser.newContext({ viewport: { width: 1200, height: 800 } });
    const alice = "a".repeat(32), bob = "b".repeat(32), carol = "c".repeat(32), org = "org", project = "3".repeat(32);
    const topic = "1".repeat(32), other = "2".repeat(32);
    const people = [{ id: alice, name: "Alice Adams" }, { id: bob, name: "Bob Brown" }, { id: carol, name: "Carol Chen" }];
    let sequence = 0;
    const message = (author, content, reply_to = null) => ({
      id: (++sequence).toString(16).padStart(32, "0"), sequence, topic_id: topic, author_id: author, author_kind: "human",
      content, client_id: "seed-" + sequence, created_at: 1_800_000_000 + sequence * 60, revision: 0, reply_to });
    const question = message(alice, "Is the bound in **Lemma 3.2** right?");
    const note = message(bob, "I will read §3 tonight.");
    const reply = message(carol, "The constant should be 2, not 3.", question.id);
    const aside = message(bob, "Unrelated: the seminar moves to Thursday.");
    const mention = message(bob, `@[${alice}] can you check §4 as well?`);
    const theirs = message(carol, "Thanks, Bob.", note.id);
    const messages = [question, note, reply, aside, mention, theirs];
    const hierarchy = { projects: [{ id: project, name: "Algebra", visibility: "organization" }],
      channels: [{ id: "channel", name: "research", project_id: project }],
      topics: [{ id: topic, name: "Bounds", channel_id: "channel", kind: "alpha", default_model: null, knowledge_tools: [] },
        { id: other, name: "Other", channel_id: "channel", kind: "alpha", default_model: null, knowledge_tools: [] }],
      topic_state: [{ topic_id: topic, subscribed: true, last_read_sequence: note.sequence, last_message_sequence: theirs.sequence,
        unread_count: 4, unread_mention_count: 1, unread_reply_count: 1 }] };
    await context.route("**/api/**", async route => {
      const url = new URL(route.request().url()), path = url.pathname, parts = path.split("/");
      let data = [];
      if (path === "/api/health") data = { initialized: true };
      else if (path === "/api/auth/me") data = { id: alice, name: "Alice Adams", email: "alice@example.org" };
      else if (path === "/api/organizations") data = [{ id: org, name: "Research", role: "member" }];
      else if (path.endsWith("/hierarchy")) data = hierarchy;
      else if (path.endsWith("/members")) data = people.map(person => ({ ...person, email: person.id + "@example.org", role: "member" }));
      else if (path.endsWith("/presence")) data = { humans: [], bots: [] };
      else if (path.endsWith("/navigation-usage")) data = { projects: {}, channels: {}, topics: {} };
      else if (path.endsWith("/message-updates")) data = { after: 0, messages: [] };
      else if (path.includes("/reactions/")) data = {};
      else if (parts[6] === "messages" && parts.length === 7 && parts[5] === topic) {
        const after = Number(url.searchParams.get("after") ?? 0);
        data = messages.filter(item => item.sequence > after);
      }
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(data) });
    });
    const page = await context.newPage(), errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.goto(server.resolvedUrls.local[0] + `organization.html?organization=${org}&topic=${topic}`);
    const article = item => page.locator(`article[data-message-id="${item.id}"]`);
    const bar = page.getByRole("toolbar", { name: "Mentions and replies in this topic" });
    await bar.waitFor();

    // The topic list counts each kind, and the bar says what is waiting without moving the reader from the end.
    const row = page.getByRole("button", { name: /^alpha topic -Bounds/ });
    assert.equal(await row.locator(".org-mention-count").innerText(), "@1");
    assert.equal(await row.locator(".org-reply-count").innerText(), "↩1");
    assert.match(await bar.innerText(), /1 mention and 1 reply for you here/);
    assert.equal(await article(reply).locator(".org-for-you-tag").innerText(), "↩ Reply to you");
    assert.equal(await article(mention).locator(".org-for-you-tag").innerText(), "@ Mentions you");
    for (const item of [question, note, aside, theirs]) assert.equal(await article(item).locator(".org-for-you-tag").count(), 0);
    const thread = article(question).locator(".org-thread-count");
    assert.match(await thread.innerText(), /1 reply · 1 new/);
    assert.equal(await thread.getAttribute("aria-label"), "1 reply, 1 new to you: show the whole thread");
    const previous = bar.getByRole("button", { name: "Previous mention or reply" }), next = bar.getByRole("button", { name: "Next mention or reply" });
    assert.equal(await previous.isDisabled(), true);

    // ↓ steps to the oldest, focuses it and says where it is.
    await next.click();
    assert.match(await bar.innerText(), /Reply from Carol Chen\s*1 of 2/);
    assert.equal(await article(reply).evaluate(element => element === document.activeElement && element.classList.contains("org-message-for-you-current")), true);
    await page.waitForFunction(() => [...document.querySelectorAll('[role="status"]')].some(node => node.textContent === "Reply from Carol Chen, 1 of 2 in this topic"));

    // Alt+Shift+arrows step outside text fields, and leave Alt+arrows to change topics.
    await page.keyboard.press("Alt+Shift+ArrowDown");
    assert.match(await bar.innerText(), /Mention from Bob Brown\s*2 of 2/);
    assert.equal(await next.isDisabled(), true);
    assert.match(page.url(), new RegExp(topic), "the topic stays open");
    await page.keyboard.press("Alt+Shift+ArrowUp");
    assert.match(await bar.innerText(), /1 of 2/);
    const composer = page.getByLabel("Message", { exact: true });
    await composer.focus();
    await page.keyboard.press("Alt+Shift+ArrowDown");
    assert.match(await bar.innerText(), /1 of 2/, "the message box keeps its own keys");

    // Escape in the bar closes it, and the tags and the new-reply count go with it.
    await bar.getByRole("button", { name: "Close" }).focus();
    await page.keyboard.press("Escape");
    assert.equal(await bar.count(), 0);
    assert.equal(await page.locator(".org-for-you-tag").count(), 0);
    assert.doesNotMatch(await thread.innerText(), /new/);

    // On a phone the bar fits beside its buttons.
    await page.setViewportSize({ width: 375, height: 760 });
    await page.reload();
    await bar.waitFor();
    const box = await bar.boundingBox();
    assert.ok(box.x >= 0 && box.x + box.width <= 375, JSON.stringify(box));
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
    await server.close();
  }
});
