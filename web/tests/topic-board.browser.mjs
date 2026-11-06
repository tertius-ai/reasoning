// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { realpath } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

test("a board maps ideas around its question: routes place the cards, overlays mark them, cards open into boards, and you ask about a card privately",
  { timeout: 120000 }, async () => {
  const root = fileURLToPath(new URL("../", import.meta.url));
  const server = await createServer({ root, server: { host: "127.0.0.1", port: 0, fs: { allow: [root, await realpath(root + "node_modules")] } } });
  await server.listen(); const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } }), errors = [], arranged = [], asked = [], replies = [];
    const user = "a".repeat(32), org = "b".repeat(32), topic = "c".repeat(32);
    const model = { id: "fixture/reasoner", name: "Reasoner", supports_tools: true, supports_reasoning: true, reasoning_efforts: [],
      supported_parameters: [], input_modalities: [], output_modalities: [], max_completion_tokens: 4096 };
    let next = 0;
    const id = () => (++next).toString(16).padStart(32, "0");
    const boards = new Map(), side = [];
    const card = (text, extra = {}) => ({ id: id(), kind: "idea", text, author_kind: "human", created_by: user, created_at: 1_800_000_000 + next,
      updated_at: 0, rank: null, maturity: null, theme: null, promise: null, effort: null, reason: null, statements: [], ...extra });
    const content = board => ({ id: board.id, question: board.question, parent_card_id: board.parent_card_id, arranged_at: board.arranged_at,
      cards: board.cards.map(item => {
        const inner = [...boards.values()].find(other => other.parent_card_id === item.id);
        return { ...item, board: inner ? { id: inner.id, cards: inner.cards.length, gaps: 0 } : null };
      }), links: board.links });
    const view = board => {
      const path = [];
      for (let current = board; current.parent_card_id;) {
        const parent = [...boards.values()].find(other => other.cards.some(item => item.id === current.parent_card_id));
        path.unshift({ board_id: parent.id, question: parent.question, card_id: current.parent_card_id,
          card: parent.cards.find(item => item.id === current.parent_card_id).text });
        current = parent;
      }
      const children = {};
      for (const item of board.cards) {
        const inner = [...boards.values()].find(other => other.parent_card_id === item.id);
        if (inner) children[item.id] = content(inner);
      }
      return { ...content(board), topic_id: topic, created_by: user, created_at: 1, updated_at: 2, children, path, arranging: board.arranging };
    };
    page.on("pageerror", error => errors.push(error.message));
    await page.route("**/api/**", async route => {
      const request = route.request(), url = new URL(request.url()), path = url.pathname, method = request.method(), body = request.postDataJSON();
      const parts = path.split("/");
      let data = [], status = 200;
      const board = parts[4] === "boards" ? boards.get(parts[5]) : null;
      if (path === "/api/health") data = { initialized: true };
      else if (path === "/api/auth/me") data = { id: user, name: "Elden Ring", email: "elden@example.org" };
      else if (path === "/api/organizations") data = [{ id: org, name: "Testing", role: "owner" }];
      else if (path === "/api/auth/model-favorites") data = { user_id: user, models: [] };
      else if (path.endsWith("/hierarchy")) data = { projects: [{ id: "project", name: "Testing", visibility: "organization" }],
        channels: [{ id: "channel", name: "research", project_id: "project" }],
        topics: [{ id: topic, name: "Regulators", channel_id: "channel", kind: "beta", default_model: model.id, knowledge_tools: ["ledger"] }], topic_state: [] };
      else if (path.endsWith("/members")) data = [{ id: user, name: "Elden Ring", role: "owner" }];
      else if (path.endsWith("/presence")) data = { humans: [], bots: [] };
      else if (path.endsWith("/inference-configuration")) data = { allowed_models: [model.id], tools: [] };
      else if (path.endsWith("/model-prices")) data = { models: [model], allowed_models: [model.id] };
      else if (path.endsWith("/research-instructions")) data = { system_prompt: "Research safely.", prompt_version: "fixture" };
      else if (path.endsWith("/research-settings")) data = { tools: ["history"], web_search_configured: false, current_date: "2026-11-06" };
      else if (path.endsWith("/compute-configuration")) data = { images: [], policy: { allowed_images: [] } };
      else if (path.endsWith("/navigation-usage")) data = { projects: {}, channels: {}, topics: {} };
      else if (path.endsWith(`/topics/${topic}/boards`) && method === "POST") {
        const made = { id: id(), question: body.question, parent_card_id: null, arranged_at: null, cards: [], links: [], arranging: null };
        boards.set(made.id, made); data = view(made); status = 201;
      } else if (path.endsWith(`/topics/${topic}/boards`)) data = [...boards.values()].filter(item => !item.parent_card_id)
        .map(item => ({ id: item.id, question: item.question, created_by: user, created_at: 1, updated_at: 2, arranged_at: null, cards: item.cards.length }));
      else if (board && parts[6] === "cards" && parts[8] === "board") {
        let inner = [...boards.values()].find(other => other.parent_card_id === parts[7]);
        if (!inner) {
          inner = { id: id(), question: board.cards.find(item => item.id === parts[7]).text, parent_card_id: parts[7], arranged_at: null,
            cards: [], links: [], arranging: null };
          boards.set(inner.id, inner);
        }
        data = view(inner);
      } else if (board && parts[6] === "cards" && method === "POST") { board.cards.push(card(body.text)); data = view(board); status = 201; }
      else if (board && parts[6] === "links" && method === "POST") {
        board.links.push({ id: id(), source_card_id: body.source_card_id, target_card_id: body.target_card_id ?? null, kind: body.kind, author_kind: "human" });
        data = view(board); status = 201;
      } else if (board && parts[6] === "arrange") {
        arranged.push(body); board.arranging = { id: "run-arrange", status: "queued", model: body.models[0], error_code: null, applied_at: null };
        data = []; status = 202;
      } else if (board) {
        // A queued arrangement is applied by the time the board is read again.
        if (board.arranging?.status === "queued") {
          board.cards.forEach((item, index) => Object.assign(item, { rank: index + 1, maturity: index ? "exploring" : "settled",
            theme: index < 2 ? "Via Borel" : "Via Suslin", promise: index ? 0.4 : 0.9, effort: 0.3, reason: "Fixture reason " + (index + 1) }));
          board.arranging = { ...board.arranging, status: "succeeded", applied_at: 1_800_000_500 }; board.arranged_at = 1_800_000_500;
        }
        data = view(board);
      } else if (path.endsWith("/side-chat/messages")) {
        asked.push(body);
        data = { id: id(), sequence: side.length + 1, topic_id: topic, owner_id: user, author_kind: "human", content: body.content,
          client_id: body.client_id, created_at: 1_800_000_900, assistant_model: null, inference_run_id: null, reply_to: null,
          card: { id: body.card_id, board_id: null, text: "fixture card" } };
        side.push(data); status = 201;
      } else if (path.endsWith("/side-chat/replies")) { replies.push(body); data = []; status = 202; }
      else if (path.endsWith("/side-chat")) data = { messages: side, runs: [] };
      else if (path.endsWith("/messages") && method === "GET") data = [];
      else if (path.endsWith("/message-updates")) data = { after: 0, messages: [] };
      else if (path.endsWith("/inference-runs") || path.endsWith("/bot-runs") || path.endsWith("/agents")) data = [];
      else if (path.endsWith("/goal")) data = null;
      await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(data) });
    });
    await page.goto(server.resolvedUrls.local[0] + `organization.html?organization=${org}&topic=${topic}`);
    const toggle = page.getByRole("button", { name: "Board", exact: true });
    await toggle.click();
    assert.equal(await toggle.getAttribute("aria-pressed"), "true");
    const board = page.getByRole("region", { name: "Board", exact: true });
    await board.getByLabel("Board question", { exact: true }).fill("How do we show the motivic regulator is injective?");
    await board.getByRole("button", { name: "Make board", exact: true }).click();
    await board.getByRole("heading", { name: "How do we show the motivic regulator is injective?" }).waitFor();
    assert.equal(await page.getByLabel("Topic conversation", { exact: true }).isHidden(), true, "the board replaces the conversation");

    for (const text of ["Reduce to Borel's theorem", "Compare with Beilinson's regulator", "Use Suslin rigidity"]) {
      await board.getByRole("button", { name: "+ Add idea", exact: true }).click();
      await board.getByLabel("New idea", { exact: true }).fill(text);
      await board.getByLabel("New idea", { exact: true }).press("Enter");
      await board.locator(`article[aria-label="${text}"]`).waitFor();
    }
    const cardOf = text => board.locator(`.org-board-world > article[aria-label="${text}"]`);
    // Linking a card to the question takes it out of the tray, onto a route beside the question.
    const trayTop = (await cardOf("Reduce to Borel's theorem").boundingBox()).y;
    await cardOf("Reduce to Borel's theorem").click();
    const details = board.getByRole("complementary", { name: "Card details", exact: true });
    await details.getByLabel("Leads to", { exact: true }).selectOption("question");
    await details.getByRole("button", { name: "Link", exact: true }).click();
    await board.locator(".org-board-link").first().waitFor({ state: "attached" });
    assert.ok((await cardOf("Reduce to Borel's theorem").boundingBox()).y > trayTop, "a routed card leaves the tray");
    await details.getByRole("button", { name: "Close card details", exact: true }).click();

    // Arranging asks the conversation's model; its ranks, rings and themes then mark the same cards in the same places.
    await board.getByRole("button", { name: "Arrange with reasoner", exact: true }).click();
    await board.getByLabel("Rank 1", { exact: true }).waitFor({ timeout: 10000 });
    assert.deepEqual(arranged, [{ models: [model.id], client_id: arranged[0].client_id }]);
    assert.equal(await board.getByText(/finished without arranging/).count(), 0);
    const before = await cardOf("Use Suslin rigidity").boundingBox();
    const overlays = board.getByRole("group", { name: "Overlays", exact: true });
    await overlays.getByRole("button", { name: "Quadrants", exact: true }).click();
    await overlays.getByRole("button", { name: "Themes", exact: true }).click();
    await board.getByRole("img", { name: /^Quick win: promise 0.9/ }).waitFor();
    await board.locator(".org-board-theme", { hasText: "Via Borel" }).waitFor();
    assert.equal(await board.locator(".org-board-maturity-settled").count(), 1);
    await overlays.getByRole("button", { name: "Rank", exact: true }).click();
    assert.equal(await board.getByLabel("Rank 1", { exact: true }).count(), 0);
    assert.deepEqual(await cardOf("Use Suslin rigidity").boundingBox(), before, "overlays never move a card");

    // A card opens into a board of its own, with the way back above it.
    await cardOf("Use Suslin rigidity").click();
    await details.getByText("Rank 3 · exploring · Via Suslin · Cheap check", { exact: true }).waitFor();
    await details.getByRole("button", { name: "Open as a board", exact: true }).click();
    await board.getByRole("heading", { name: "Use Suslin rigidity", exact: true }).waitFor();
    const path = board.getByRole("navigation", { name: "Board path", exact: true });
    await board.getByRole("button", { name: "+ Add idea", exact: true }).click();
    await board.getByLabel("New idea", { exact: true }).fill("Rigidity over the algebraic closure");
    await board.getByLabel("New idea", { exact: true }).press("Enter");
    await cardOf("Rigidity over the algebraic closure").waitFor();
    await path.getByRole("button", { name: "How do we show the motivic regulator is injective?", exact: true }).click();
    await board.getByRole("heading", { name: "How do we show the motivic regulator is injective?" }).waitFor();
    await cardOf("Use Suslin rigidity").locator(".org-board-inside").filter({ hasText: "1 card inside" }).waitFor();

    // Zooming into the card shows its board in place, and the box asks about the card most in view.
    await cardOf("Use Suslin rigidity").dblclick();
    const inner = cardOf("Use Suslin rigidity").locator(".org-board-inner");
    await inner.getByRole("button", { name: "Rigidity over the algebraic closure", exact: true }).waitFor();
    assert.ok(Number((await board.getByLabel("Zoom", { exact: true }).innerText()).replace("%", "")) >= 160);
    const ask = board.getByLabel("Ask about a card", { exact: true });
    assert.match(await ask.getAttribute("placeholder"), /Rigidity over the algebraic closure”, the card most in view/);
    await ask.fill("Does rigidity reach rational coefficients?");
    await ask.press("Enter");
    await page.getByRole("complementary", { name: "Side chat", exact: true }).waitFor();
    const innerCard = [...boards.values()].find(item => item.parent_card_id).cards[0];
    assert.equal(asked[0].card_id, innerCard.id);
    assert.deepEqual(replies.map(item => item.models), [[model.id]]);
    await page.getByRole("complementary", { name: "Side chat", exact: true }).getByText("About the card").waitFor();

    await toggle.click();
    await page.getByLabel("Topic conversation", { exact: true }).waitFor();
    assert.deepEqual(errors, []);
  } finally { await browser.close(); await server.close(); }
});
