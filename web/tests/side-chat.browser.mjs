// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { realpath } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

test("a private side chat answers beside the topic; Bring to chat posts an exchange as a card that opens beside it; others' can be asked about",
  { timeout: 90000 }, async () => {
  const root = fileURLToPath(new URL("../", import.meta.url));
  const server = await createServer({ root, server: { host: "127.0.0.1", port: 0, fs: { allow: [root, await realpath(root + "node_modules")] } } });
  await server.listen(); const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } }), errors = [], asked = [], replies = [], brought = [],
      sideAsked = [];
    const user = "a".repeat(32), deven = "d".repeat(32), org = "b".repeat(32), topic = "c".repeat(32);
    const model = { id: "fixture/reasoner", name: "Reasoner", supports_tools: true, supports_reasoning: true, reasoning_efforts: [],
      supported_parameters: [], input_modalities: [], output_modalities: [], max_completion_tokens: 4096 };
    // While `hold` is set, a reply at work stays at work.
    let sequence = 0, sideSequence = 0, hold = false;
    const post = (author_id, content, extra = {}) => ({ id: String(++sequence).padStart(32, "0"), sequence, topic_id: topic, author_id,
      author_kind: "human", content, client_id: "seed-" + sequence, created_at: 1_800_000_000 + sequence * 60, revision: 0, reply_to: null, ...extra });
    // Deven brought one exchange of his own side chat earlier; one of his side messages stayed private.
    const devenPost = post(deven, "From my side chat", { side_chat: { owner_id: deven, words: "I checked the K-theory side too.", omitted: 1,
      linked: [], messages: [
        { id: "e".repeat(32), author_kind: "human", assistant_model: null, content: "Which K-groups carry the regulator?", created_at: 1_799_999_000 },
        { id: "e".repeat(31) + "f", author_kind: "assistant", assistant_model: "fixture/reasoner", content: "The odd ones, which Borel computed.",
          created_at: 1_799_999_060 }] } });
    const main = [post(user, "Do we know the motivic case?"), devenPost, post(deven, "Is the Borel regulator injective here?")];
    const side = [], runs = [];
    const sideMessage = (author_kind, content, extra = {}) => ({ id: "f" + String(++sideSequence).padStart(31, "0"), sequence: sideSequence,
      topic_id: topic, owner_id: user, author_kind, content, client_id: "side-" + sideSequence, created_at: 1_800_000_100 + sideSequence * 60,
      assistant_model: null, inference_run_id: null, reply_to: null, brought: null, ...extra });
    const answers = { "Which regulator do we mean here?": "Beilinson's; it agrees with Borel's up to a factor.",
      "Is the Borel regulator injective for number fields?": "Yes, by Borel 1977.",
      "And the motivic version?": "It follows from the Borel case with Suslin rigidity.",
      "Does that rank match the order of vanishing?": "Yes, at s = 1 − n." };
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
      else if (path.endsWith("/members")) data = [{ id: user, name: "Elden Ring", role: "owner" }, { id: deven, name: "Deven", role: "member" }];
      else if (path.endsWith("/presence")) data = { humans: [], bots: [] };
      else if (path.endsWith("/inference-configuration")) data = { allowed_models: [model.id], tools: [] };
      else if (path.endsWith("/model-prices")) data = { models: [model], allowed_models: [model.id] };
      else if (path.endsWith("/research-instructions")) data = { system_prompt: "Research safely.", prompt_version: "fixture" };
      else if (path.endsWith("/research-settings")) data = { tools: ["history"], web_search_configured: false, current_date: "2026-11-06" };
      else if (path.endsWith("/compute-configuration")) data = { images: [], policy: { allowed_images: [] } };
      else if (path.endsWith("/navigation-usage")) data = { projects: {}, channels: {}, topics: {} };
      else if (path.endsWith("/side-chat/messages")) {
        sideAsked.push(body);
        data = sideMessage("human", body.content, { client_id: body.client_id, about: body.about_message_id
          ? { message_id: body.about_message_id, created_at: devenPost.created_at, author_id: deven, author_name: "Deven" } : null });
        side.push(data); status = 201;
      } else if (path.endsWith("/side-chat/replies")) {
        replies.push(body);
        const created = { id: "run-" + body.question_id, model: body.models[0], status: "running", created_at: 1_800_000_500, requested_by: user,
          output_text: "", cancel_requested: 0, message_id: null, side_chat: { question_id: body.question_id } };
        runs.unshift(created); data = [created]; status = 202;
      } else if (path.endsWith("/side-chat/bring")) {
        // As the server does: the chosen replies read the side chat before their questions; posts that brought any of it are
        // linked, and the rest is counted.
        brought.push(body);
        const chosen = side.filter(item => body.message_ids.includes(item.id));
        const bound = Math.max(...chosen.filter(item => item.author_kind === "assistant")
          .map(reply => side.find(item => item.id === reply.reply_to)?.sequence ?? reply.sequence));
        const before = side.filter(item => item.sequence < bound && !body.message_ids.includes(item.id));
        const linked = [...new Map(before.filter(item => item.brought).map(item => [item.brought.message_id, item.brought])).values()];
        data = post(user, (body.words ? body.words + "\n\n" : "") + "From my side chat", { client_id: body.client_id, reply_to: body.reply_to ?? null,
          side_chat: { owner_id: user, words: body.words ?? "", omitted: before.filter(item => !item.brought).length, linked,
            messages: chosen.map(item => ({ id: item.id, author_kind: item.author_kind, assistant_model: item.assistant_model,
              content: item.content, created_at: item.created_at })) } });
        for (const item of chosen) item.brought = { message_id: data.id, created_at: data.created_at };
        main.push(data); status = 201;
      } else if (path.endsWith("/side-chat")) {
        // A reply at work finishes on the next read of the side chat, with one step and its usage.
        for (const run of runs.filter(item => item.status === "running" && !hold)) {
          run.status = "succeeded";
          const question = side.find(item => item.id === run.side_chat.question_id);
          side.push(sideMessage("assistant", answers[question.content], { assistant_model: model.id, inference_run_id: run.id, reply_to: question.id,
            steps: [{ sequence: 1, phase: "round", round: 1 }], usage: { prompt_tokens: 10, completion_tokens: 20, actual_microusd: null } }));
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
    await page.getByText("Is the Borel regulator injective here?", { exact: true }).waitFor();

    // The side chat opens beside the conversation, private and linked to the topic; its box names no model and has no size handle.
    await page.getByRole("button", { name: "Side chat", exact: true }).click();
    const panel = page.getByRole("complementary", { name: "Side chat", exact: true });
    await panel.getByText("Private · linked to Regulators", { exact: true }).waitFor();
    const box = panel.getByLabel("Side chat message", { exact: true });
    assert.equal(await box.evaluate(element => getComputedStyle(element).resize), "none");
    // The first three questions; the fourth comes later, about Deven's exchange. The first reply is held at work for a while: it
    // waits on the model's side, under the model's name and in its color, not under your question, and the reply takes its place.
    const toneOf = locator => locator.evaluate(element => [...element.classList].find(name => name.startsWith("org-person-tone-")));
    for (const [index, question] of Object.keys(answers).slice(0, 3).entries()) {
      hold = index === 0;
      await box.fill(question);
      await box.press("Enter");
      if (hold) {
        const place = panel.locator(".org-side-waiting");
        await place.locator(".org-side-working", { hasText: /^Answering · \d+ s/ }).waitFor();
        assert.equal(await place.count(), 1);
        assert.equal(await place.evaluate(element => element.classList.contains("org-message-group-mine")), false);
        assert.match(await place.locator("header strong").innerText(), /reasoner/i);
        assert.equal(await panel.locator(".org-message-group-mine .org-side-working").count(), 0, "not under your question");
        const placeBox = await place.boundingBox(), messagesBox = await panel.getByLabel("Side chat messages", { exact: true }).boundingBox();
        assert.ok(placeBox.x - messagesBox.x < 20, "on the left, where the reply will be");
        const tone = await toneOf(place);
        hold = false;
        await panel.getByText(answers[question], { exact: true }).waitFor();
        assert.equal(await place.count(), 0, "the reply takes its place");
        assert.equal(await toneOf(panel.locator(".org-message-group", { hasText: answers[question] })), tone, "in the same color");
      } else await panel.getByText(answers[question], { exact: true }).waitFor();
    }
    assert.equal(await panel.getByText(/reasoner/i).count(), 3, "the model is named only on its replies");
    assert.deepEqual(replies.map(item => [item.models, item.max_completion_tokens]), Array(3).fill([[model.id], 4096]));
    // It reads like the conversation: your messages on the right, rails dashed in each speaker's color, steps under each reply.
    assert.equal(await panel.locator(".org-message-group-mine").count(), 3);
    const transcriptBox = await panel.getByLabel("Side chat messages", { exact: true }).boundingBox();
    const mineBox = await panel.locator(".org-message-group-mine").first().boundingBox();
    assert.ok(transcriptBox.x + transcriptBox.width - (mineBox.x + mineBox.width) < 20, "your messages end at the right edge");
    assert.ok(mineBox.x - transcriptBox.x > 30, "and leave room on their left, as in the conversation");
    const rail = await panel.locator(".org-message-group").first().evaluate(element => getComputedStyle(element, "::before").borderLeftStyle);
    assert.equal(rail, "dashed");
    assert.equal(await panel.getByRole("button", { name: "1 step", exact: true }).count(), 3);
    assert.equal(await panel.getByRole("list", { name: "Ledger statements" }).count(), 0, "replies show their steps, not statement codes");
    const transcript = page.getByLabel("Message history");
    assert.equal(await transcript.getByText("Yes, by Borel 1977.").count(), 0, "the side chat stays out of the topic");

    // Bring to chat on a reply opens a menu with its exchange and the one before it; Escape closes it without posting.
    const second = panel.locator("article", { hasText: "Yes, by Borel 1977." });
    await second.hover();
    await second.getByRole("button", { name: "Bring to chat", exact: true }).click();
    const menu = panel.getByRole("group", { name: "Bring to chat", exact: true });
    await menu.getByText("This exchange", { exact: true }).waitFor();
    assert.equal(await menu.getByRole("checkbox").count(), 1);
    // Your words have focus as soon as the menu shows, so an Escape pressed at once closes it.
    assert.equal(await menu.getByLabel("Your words", { exact: true }).evaluate(field => field === document.activeElement), true);
    await page.keyboard.press("Escape");
    await menu.waitFor({ state: "detached" });
    assert.deepEqual(brought, []);

    // Deven asked since your last message, so the post answers him; your words mention him above the group.
    await second.hover();
    await second.getByRole("button", { name: "Bring to chat", exact: true }).click();
    await menu.locator(".org-side-answer", { hasText: "Answer Deven · Is the Borel regulator injective here?" }).waitFor();
    const words = menu.getByLabel("Your words", { exact: true });
    await words.pressSequentially("@De");
    await menu.getByRole("option", { name: "Deven" }).waitFor();
    await words.press("Enter");
    await words.pressSequentially("yes, see below.");
    await menu.getByRole("button", { name: "Bring 1 exchange as a reply", exact: true }).waitFor();
    await words.press("Enter");
    await panel.getByText("Brought 1 exchange to the topic as a reply.", { exact: true }).waitFor();
    assert.deepEqual(brought[0].message_ids, [side[2].id, side[3].id]);
    assert.equal(brought[0].words, `@[${deven}] yes, see below.`);
    assert.equal(brought[0].reply_to, main[2].id);
    await transcript.getByText("yes, see below.", { exact: false }).waitFor();
    await transcript.locator(".org-reply-pointer", { hasText: "Is the Borel regulator injective here?" }).waitFor();
    await transcript.getByText("Builds on 2 side messages that stay private.", { exact: true }).waitFor();
    // The post shows the exchange as a card titled by its question, with no teaser; the card opens it beside the conversation,
    // in place of the side chat, until Escape.
    const card = transcript.getByRole("button", { name: /^Is the Borel regulator injective for number fields\? Your side chat · 1 exchange · reasoner/ });
    assert.equal(await card.getAttribute("aria-expanded"), "false");
    assert.equal(await transcript.getByText("Yes, by Borel 1977.").count(), 0);
    // What it builds on is the card's footer, centered between the line above it and the card's edge.
    const gaps = await transcript.locator(".org-side-card > .org-side-group-note").first().evaluate(note => {
      const range = document.createRange(); range.selectNodeContents(note);
      const text = range.getBoundingClientRect(), box = note.getBoundingClientRect(), edge = note.parentElement.getBoundingClientRect();
      return { above: text.top - box.top - 1, below: edge.bottom - 1 - text.bottom,
        button: getComputedStyle(note.parentElement.querySelector(".org-side-card-open")).borderBottomWidth,
        corner: getComputedStyle(note.parentElement.querySelector(".org-side-card-open")).borderBottomLeftRadius };
    });
    assert.ok(Math.abs(gaps.above - gaps.below) <= 1.5 && gaps.above <= 4.5 && gaps.button === "0px" && gaps.corner === "0px",
      JSON.stringify(gaps));
    await card.click();
    const exchangePanel = page.getByRole("complementary", { name: "Exchange from a side chat", exact: true });
    await exchangePanel.getByRole("heading", { name: "From your side chat", exact: true }).waitFor();
    await exchangePanel.getByText("Yes, by Borel 1977.", { exact: true }).waitFor();
    await exchangePanel.getByText(/· answers Deven$/).waitFor();
    assert.equal(await card.getAttribute("aria-expanded"), "true");
    await panel.waitFor({ state: "hidden" });
    await page.keyboard.press("Escape");
    await exchangePanel.waitFor({ state: "detached" });
    await panel.getByText("Private · linked to Regulators", { exact: true }).waitFor();
    // A reply in the topic links to its post instead of offering Bring to chat again.
    await second.hover();
    await second.getByRole("link", { name: "Show it in the topic", exact: true }).waitFor();
    assert.equal(await second.getByRole("button", { name: "Bring to chat" }).count(), 0);

    // The last reply builds on both earlier exchanges: the one in the topic is linked, not ticked; the other comes along.
    const third = panel.locator("article", { hasText: "It follows from the Borel case with Suslin rigidity." });
    await third.hover();
    await third.getByRole("button", { name: "Bring to chat", exact: true }).click();
    await menu.getByText(/^In chat since /).waitFor();
    assert.equal(await menu.getByRole("checkbox").count(), 1, "the exchange already in the topic cannot be ticked");
    assert.equal(await menu.locator(".org-side-answer").count(), 0, "nobody asked since your post");
    // Ticking adds an earlier exchange and unticking takes it out again; the reply's own exchange always stays.
    await menu.getByRole("checkbox").check();
    await menu.getByRole("button", { name: "Bring 2 exchanges", exact: true }).waitFor();
    await menu.getByRole("checkbox").uncheck();
    await menu.getByRole("button", { name: "Bring 1 exchange", exact: true }).waitFor();
    await menu.getByRole("checkbox").check();
    await menu.getByRole("button", { name: "Bring 2 exchanges", exact: true }).click();
    await panel.getByText("Brought 2 exchanges to the topic.", { exact: true }).waitFor();
    assert.deepEqual(brought[1].message_ids, [side[0].id, side[1].id, side[4].id, side[5].id]);
    assert.equal(brought[1].words, undefined);
    assert.equal(brought[1].reply_to, undefined);
    const back = transcript.getByRole("link", { name: /^your post at / });
    await back.waitFor();
    assert.match(await back.getAttribute("href"), new RegExp(`message=${main[3].id}$`));
    // The link marks the earlier post, which is on screen, without a new address; the mark then fades.
    await back.click();
    await transcript.locator(`article.org-message-linked[data-message-id="${main[3].id}"]`).waitFor();
    assert.equal(new URL(page.url()).searchParams.get("message"), null);
    await transcript.locator("article.org-message-linked").waitFor({ state: "detached", timeout: 6000 });
    // The second card's chain leads back to the first post's exchange, in the same panel; your own exchange then continues in
    // your side chat, scrolled to and marked there.
    await transcript.getByRole("button", { name: /^Which regulator do we mean here\? Your side chat · 2 exchanges · reasoner/ }).click();
    await exchangePanel.getByText("Beilinson's; it agrees with Borel's up to a factor.", { exact: true }).waitFor();
    await exchangePanel.getByRole("link", { name: /^your post at / }).click();
    await exchangePanel.getByText("Yes, by Borel 1977.", { exact: true }).waitFor();
    assert.equal(await exchangePanel.getByText("Beilinson's; it agrees with Borel's up to a factor.").count(), 0);
    await exchangePanel.getByRole("button", { name: "Continue in side chat", exact: true }).click();
    await exchangePanel.waitFor({ state: "detached" });
    await panel.locator(`article.org-message-linked[data-side-message-id="${side[3].id}"]`).waitFor();
    // That mark belongs to the moment you continued: closing and opening the side chat again marks nothing.
    await panel.getByRole("button", { name: "Close side chat", exact: true }).click();
    await panel.waitFor({ state: "detached" });
    await page.getByRole("button", { name: "Side chat", exact: true }).click();
    await panel.getByText("Yes, by Borel 1977.", { exact: true }).waitFor();
    await page.waitForTimeout(400);
    assert.equal(await panel.locator("article.org-message-linked").count(), 0);

    // Someone else's exchange can be asked about privately: your next question carries it, and bringing the answer replies
    // to their post.
    await transcript.getByRole("button", { name: /^Which K-groups carry the regulator\? Deven's side chat · 1 exchange · reasoner/ }).click();
    await exchangePanel.getByRole("heading", { name: "From Deven's side chat", exact: true }).waitFor();
    await exchangePanel.getByText("Builds on 1 side message that stays private.", { exact: true }).waitFor();
    assert.equal(await exchangePanel.getByRole("button", { name: "Continue in side chat" }).count(), 0);
    await exchangePanel.getByRole("button", { name: "Ask privately about this", exact: true }).click();
    await exchangePanel.waitFor({ state: "detached" });
    await panel.locator(".org-side-about-bar", { hasText: "About Deven's exchange · Which K-groups carry the regulator?" }).waitFor();
    await box.fill("Does that rank match the order of vanishing?");
    await box.press("Enter");
    await panel.getByText("Yes, at s = 1 − n.", { exact: true }).waitFor();
    assert.equal(sideAsked.at(-1).about_message_id, devenPost.id);
    assert.equal(await panel.locator(".org-side-about-bar").count(), 0, "the exchange goes with one question");
    await panel.getByRole("button", { name: /^About Deven's post at / }).waitFor();
    const fourth = panel.locator("article", { hasText: "Yes, at s = 1 − n." });
    await fourth.hover();
    await fourth.getByRole("button", { name: "Bring to chat", exact: true }).click();
    await menu.locator(".org-side-answer", { hasText: /^↳Answer Deven · the post at / }).waitFor();
    await menu.getByRole("button", { name: "Bring 1 exchange as a reply", exact: true }).click();
    await panel.getByText("Brought 1 exchange to the topic as a reply.", { exact: true }).waitFor();
    assert.equal(brought[2].reply_to, devenPost.id);
    assert.equal(asked.length, 0, "bringing and asking privately never ask the topic's model");

    await panel.getByRole("button", { name: "Close side chat", exact: true }).click();
    await panel.waitFor({ state: "detached" });
    assert.equal(await page.getByRole("button", { name: "Side chat", exact: true }).getAttribute("aria-expanded"), "false");
    assert.deepEqual(errors, []);
  } finally { await browser.close(); await server.close(); }
});
