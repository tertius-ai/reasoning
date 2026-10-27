// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { realpath } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

test("a reply points to its message, and the pointer opens the whole thread over the conversation", { timeout: 90000 }, async () => {
  const root = fileURLToPath(new URL("../", import.meta.url));
  const server = await createServer({ root, server: { host: "127.0.0.1", port: 0, fs: { allow: [root, await realpath(root + "node_modules")] } } });
  await server.listen();
  const browser = await chromium.launch({ headless: true });
  try {
    const context = await browser.newContext({ viewport: { width: 1200, height: 800 } });
    const alice = "a".repeat(32), bob = "b".repeat(32), carol = "c".repeat(32), org = "org", topic = "1".repeat(32), project = "3".repeat(32);
    const people = [{ id: alice, name: "Alice Adams" }, { id: bob, name: "Bob Brown" }, { id: carol, name: "Carol Chen" }];
    let sequence = 0;
    const message = (author, content, reply_to = null, client_id = "seed-" + (sequence + 1)) => ({
      id: (++sequence).toString(16).padStart(32, "0"), sequence, topic_id: topic, author_id: author, author_kind: "human",
      content, client_id, created_at: 1_800_000_000 + sequence * 60, revision: 0, reply_to });
    const question = message(bob, "Can you check the bound in **Lemma 3.2** before we post the draft?");
    const aside = message(alice, "Unrelated: the seminar moves to Thursday.");
    const answer = message(carol, "The constant should be 2, not 3.", question.id);
    const messages = [question, aside, answer], posts = [], threads = [];
    const thread = id => {
      let top = messages.find(item => item.id === id);
      while (top.reply_to) top = messages.find(item => item.id === top.reply_to);
      const ids = [top.id];
      for (let index = 0; index < ids.length; index++) ids.push(...messages.filter(item => item.reply_to === ids[index]).map(item => item.id));
      return { root_id: top.id, message_id: id, messages: messages.filter(item => ids.includes(item.id)), truncated: false };
    };
    await context.route("**/api/**", async route => {
      const request = route.request(), url = new URL(request.url()), path = url.pathname, parts = path.split("/");
      let status = 200, data = [];
      if (path === "/api/health") data = { initialized: true };
      else if (path === "/api/auth/me") data = { id: alice, name: "Alice Adams", email: "alice@example.org" };
      else if (path === "/api/organizations") data = [{ id: org, name: "Research", role: "member" }];
      else if (path.endsWith("/hierarchy")) data = { projects: [{ id: project, name: "Algebra", visibility: "organization" }],
        channels: [{ id: "channel", name: "research", project_id: project }],
        topics: [{ id: topic, name: "Bounds", channel_id: "channel", kind: "alpha", default_model: null, knowledge_tools: [] }], topic_state: [] };
      else if (path.endsWith("/members")) data = people.map(person => ({ ...person, email: person.id + "@example.org", role: "member" }));
      else if (path.endsWith("/presence")) data = { humans: [], bots: [] };
      else if (path.endsWith("/navigation-usage")) data = { projects: {}, channels: {}, topics: {} };
      else if (path.endsWith("/message-updates")) data = { after: 0, messages: [] };
      else if (path.includes("/reactions/")) data = {};
      else if (parts[6] === "messages" && parts[8] === "thread") { threads.push(parts[7]); data = thread(parts[7]); }
      else if (parts[6] === "messages" && parts.length === 7 && request.method() === "POST") {
        const body = request.postDataJSON(); posts.push(body);
        const existing = messages.find(item => item.client_id === body.client_id);
        data = existing ?? message(alice, body.content, body.reply_to ?? null, body.client_id);
        if (!existing) messages.push(data);
        status = 201;
      } else if (parts[6] === "messages" && parts.length === 7) {
        const after = Number(url.searchParams.get("after") ?? 0);
        data = messages.filter(item => item.sequence > after);
      }
      await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(data) });
    });
    const page = await context.newPage(), errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.goto(server.resolvedUrls.local[0] + `organization.html?organization=${org}&topic=${topic}`);
    const article = id => page.locator(`article[data-message-id="${id}"]`);
    await article(answer.id).waitFor();

    // The reply names the person it answers and a plain excerpt of their message, and starts its own speaker group.
    const pointer = article(answer.id).locator(".org-reply-pointer");
    assert.match(await pointer.innerText(), /Bob\s+Can you check the bound in Lemma 3\.2 before we post the draft\?/);
    assert.equal(await article(question.id).locator(".org-reply-pointer").count(), 0);
    assert.equal(await article(answer.id).evaluate(element => element.matches(".org-message-group > .org-message:first-child")), true);
    // The thread's first message counts its replies; other messages show no count.
    const count = article(question.id).locator(".org-thread-count");
    assert.equal(await count.textContent(), "↩1 reply");
    assert.equal(await page.locator(".org-thread-count").count(), 1);

    // Reply starts a reply in the composer; Escape cancels it, and a sent reply carries its target.
    const composer = page.getByLabel("Message", { exact: true });
    await article(aside.id).hover();
    await article(aside.id).getByRole("button", { name: "Reply", exact: true }).click();
    const bar = page.locator(".org-reply-bar");
    assert.match(await bar.innerText(), /Replying to Alice Adams · Unrelated: the seminar moves to Thursday\./);
    await composer.focus(); await composer.press("Escape");
    await bar.waitFor({ state: "detached" });
    await article(question.id).hover();
    await article(question.id).getByRole("button", { name: "Reply", exact: true }).click();
    await bar.waitFor();
    await composer.fill("I will check it tonight.");
    await composer.press("Enter");
    await bar.waitFor({ state: "detached" });
    const sent = posts.at(-1);
    assert.equal(sent.content, "I will check it tonight."); assert.equal(sent.reply_to, question.id);
    const mine = messages.at(-1);
    assert.match(await article(mine.id).locator(".org-reply-pointer").innerText(), /Bob\s+Can you check/);
    await count.filter({ hasText: /^↩2 replies$/ }).waitFor();

    // Your own messages say "You" and hug the right edge with left-aligned text; everyone else's stay on the left.
    const layout = id => article(id).evaluate(element => {
      const group = element.closest(".org-message-group"), transcript = element.closest(".org-transcript");
      const box = element.getBoundingClientRect(), area = transcript.getBoundingClientRect(), style = getComputedStyle(transcript);
      return { mine: group.classList.contains("org-message-group-mine"), name: group.querySelector("header strong")?.textContent,
        align: getComputedStyle(element.querySelector("p")).textAlign, width: box.width,
        left: box.left - area.left - parseFloat(style.paddingLeft), right: area.right - parseFloat(style.paddingRight) - box.right };
    });
    const own = await layout(mine.id), theirs = await layout(answer.id);
    assert.equal(own.mine, true); assert.equal(own.name, "You"); assert.equal(own.align, "start");
    assert.ok(own.right < 1 && own.left > 100, `your message hugs the right edge: ${JSON.stringify(own)}`);
    assert.equal(theirs.mine, false); assert.equal(theirs.name, "Carol Chen");
    assert.ok(theirs.left < 1 && theirs.right < 1, `their message spans the column: ${JSON.stringify(theirs)}`);
    assert.ok(own.width < theirs.width / 2, "a short message of yours is as wide as its text");
    assert.match(await article(aside.id).locator("header strong").innerText(), /^You$/);

    // The pointer opens the whole thread: the root and every reply, in order, over a dimmed conversation.
    await pointer.click();
    const dialog = page.getByRole("dialog", { name: "Thread", exact: true });
    await dialog.waitFor();
    assert.equal(threads.at(-1), answer.id);
    await dialog.getByText("3 messages", { exact: true }).waitFor();
    const order = await dialog.locator("article.org-message").evaluateAll(items => items.map(item => item.innerText));
    assert.deepEqual(order.map(text => text.split("\n")[0]), ["Bob Brown", "Carol Chen", "You"]);
    assert.ok(!order.some(text => text.includes("seminar")), "an unrelated message is not part of the thread");
    assert.deepEqual(await dialog.locator(".org-message-group").evaluateAll(groups => groups.map(group => group.classList.contains("org-message-group-mine"))),
      [false, false, true], "your reply sits on the right in the thread too");

    // Replying in the thread answers its root and shows the new reply in the thread and the conversation.
    await dialog.getByLabel("Reply in thread", { exact: true }).fill("Agreed, 2 it is.");
    await dialog.getByLabel("Reply in thread", { exact: true }).press("Enter");
    await dialog.getByText("4 messages", { exact: true }).waitFor();
    assert.equal(posts.at(-1).reply_to, question.id);
    // Your two replies a minute apart form one run under one "You", as in a direct conversation.
    const runs = await dialog.locator(".org-message-group").evaluateAll(groups => groups.map(group =>
      [...group.querySelectorAll("article")].map(item => item.classList.contains("org-message-continuation") ? "+" : item.querySelector("header strong").textContent)));
    assert.deepEqual(runs, [["Bob Brown"], ["Carol Chen"], ["You", "+"]]);
    await article(messages.at(-1).id).waitFor();
    await page.keyboard.press("Escape");
    await dialog.waitFor({ state: "detached" });
    await count.filter({ hasText: /^↩3 replies$/ }).waitFor();
    // The count opens the same thread.
    await count.click();
    await dialog.getByText("4 messages", { exact: true }).waitFor();
    assert.equal(threads.at(-1), question.id);
    await page.keyboard.press("Escape");
    await dialog.waitFor({ state: "detached" });

    // Closing the thread with the mouse returns focus to the pointer, but the reply's actions hide once the mouse leaves.
    await pointer.click();
    await dialog.getByRole("button", { name: "Close thread", exact: true }).click();
    await dialog.waitFor({ state: "detached" });
    assert.equal(await page.evaluate(() => document.activeElement?.classList.contains("org-reply-pointer")), true);
    const composerBox = await composer.boundingBox();
    await page.mouse.move(composerBox.x + 20, composerBox.y + composerBox.height / 2);
    assert.equal(await article(answer.id).getByLabel("Add reaction", { exact: true }).evaluate(element => getComputedStyle(element).opacity), "0");
    assert.equal(await article(answer.id).locator(".chat-message-actions").evaluate(element => getComputedStyle(element, "::before").content), "none");
    assert.deepEqual(errors, []);
  } finally {
    await browser.close(); await server.close();
  }
});
