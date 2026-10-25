import assert from "node:assert/strict";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { realpath } from "node:fs/promises";
import { chromium } from "playwright";
import { createServer } from "vite";

test("phone toolbar retains every action and scoped navigation survives refresh", { timeout: 60000 }, async () => {
  const root = fileURLToPath(new URL("../", import.meta.url));
  const server = await createServer({ root, server: { host: "127.0.0.1", port: 0, fs: { allow: [root, await realpath(root + "node_modules")] } } });
  await server.listen();
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1200, height: 800 } }), errors = [];
    page.on("pageerror", error => errors.push(error.message));
    let account = "alice";
    const topics = ["First", "Second"].map((name, index) => ({ id: "topic-" + index, name, channel_id: "channel", kind: "alpha", default_model: null, knowledge_tools: [] }));
    const organizations = [{ id: "org", name: "Research", role: "owner" }, { id: "other", name: "Other", role: "owner" }];
    const hierarchy = { projects: [{ id: "project", name: "Project", visibility: "organization" }], channels: [{ id: "channel", name: "channel", project_id: "project" }], topics, topic_state: [{topic_id:"topic-1", subscribed:true, unread_mention_count:2, unread_count:0}] };
    const cost = { actual_microusd: 12345678, unknown_calls: 1, external_runs: 2 };
    const benchmarks = { models: { "provider/model": { intelligence_index: 71.2, agentic_index: 58.3, coding_index: 65.8 } },
      meta: { source: "artificial-analysis", source_url: "https://artificialanalysis.ai", as_of: "2026-10-09T12:00:00Z",
        citation: "Source: Artificial Analysis via OpenRouter." }, status: "cached" };
    await page.route("**/api/**", async route => {
      const path = new URL(route.request().url()).pathname;
      let data = [];
      if (path === "/api/health") data = { initialized: true };
      else if (path === "/api/auth/me") data = { id: account, email: account + "@example.org", name: account };
      else if (path === "/api/organizations") data = organizations;
      else if (path.endsWith("/hierarchy")) data = hierarchy;
      else if (path.endsWith("/members")) data = [{ id: account, name: account, email: account + "@example.org", role: "owner" }];
      else if (path.endsWith("/navigation-usage")) data = { projects: { project: cost }, channels: { channel: cost }, topics: { "topic-0": cost, "topic-1": cost } };
      else if (path.endsWith("/presence")) data = { humans: [], bots: [] };
      else if (path.endsWith("/message-updates")) data = { after: 0, messages: [] };
      else if (path.endsWith("/mathematics")) data = {references:[]};
      else if (path.endsWith("/model-benchmarks")) data = benchmarks;
      else if (path.endsWith("/model-prices")) data = { models: [{ id: "provider/model", name: "Test model", supports_tools: true,
        provider: "provider", description: "", input_modalities: ["text"], output_modalities: ["text"], supported_parameters: ["tools"] }],
        allowed_models: ["provider/model"], fetched_at: 1791547200, source: "fixture" };
      else if (path.endsWith("/inference-configuration")) data = { routing: { strategy: "recommended", zdr: false, region: "global" } };
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(data) });
    });
    await page.goto(server.resolvedUrls.local[0] + "organization.html");
    await page.getByRole("button", { name: "Copy topic JSON", exact: true }).waitFor();
    assert.equal(await page.getByRole("button", { name: "Copy topic JSON", exact: true }).isVisible(), true);
    const second = () => page.getByRole("button", { name: /^alpha topic -Second/ });
    await second().click();
    const project = () => page.locator("details.org-project");
    const readout = page.locator(".org-inline-spend[data-visible=true]");
    await page.locator(".org-brand").hover();
    assert.equal(await readout.count(), 0);
    assert.equal(await second().locator(".org-mention-count").innerText(), "@");
    assert.ok(await second().evaluate(element => parseFloat(getComputedStyle(element).paddingLeft) > 20));
    const before = await second().boundingBox();
    for (const [row, name] of [[project().locator("summary"), "Project"], [page.getByRole("button", { name: /^#channel/ }), "#channel"], [second(), "α-Second"]]) {
      await row.hover();
      assert.match(await readout.innerText(), /\$12\.35\*/);
      assert.doesNotMatch(await readout.innerText(), /[()]/);
      assert.ok(await row.locator(".org-inline-spend[data-visible=true]").isVisible(), name);
      assert.deepEqual(await second().boundingBox(), before, "spending must not shift rows");
    }
    await page.locator(".org-brand").hover();
    assert.equal(await readout.count(), 0);
    await second().focus();
    await page.keyboard.press("Tab"); await page.keyboard.press("Shift+Tab");
    assert.match(await second().locator(".org-inline-spend[data-visible=true]").innerText(), /\$12\.35/);
    await page.getByLabel("Copy topic JSON", { exact: true }).focus();
    assert.equal(await readout.count(), 0);
    const icons = page.locator(".org-toolbar-icons .org-icon-button");
    assert.equal(await icons.count(), 7);
    const boxes = await Promise.all((await icons.all()).map(icon => icon.boundingBox()));
    for(let index=1; index<boxes.length; index++) assert.equal(boxes[index].x-boxes[index-1].x,30);
    const helpStyle = await page.getByLabel("Help", { exact: true }).evaluate(element => {
      const style = getComputedStyle(element), peer = getComputedStyle(document.querySelector(".org-presence-toggle"));
      return { decoration: style.textDecorationLine, color: style.color, peerColor: peer.color, border: style.borderBottomWidth };
    });
    assert.equal(helpStyle.decoration, "none"); assert.equal(helpStyle.border, "0px");
    assert.equal(helpStyle.color, helpStyle.peerColor);
    await page.emulateMedia({colorScheme:"dark"});
    await page.waitForFunction(() => document.documentElement.dataset.theme === "dark");
    await page.emulateMedia({colorScheme:"light"});
    await page.waitForFunction(() => document.documentElement.dataset.theme === "light");
    await page.getByLabel("Switch to dark mode", { exact: true }).click();
    await page.getByLabel("Account menu").click();
    assert.equal(await page.getByLabel("Color theme").count(), 0);
    await page.keyboard.press("Escape");
    await page.reload(); await project().waitFor();
    assert.equal(await page.evaluate(() => document.documentElement.dataset.theme), "dark");
    await page.emulateMedia({ colorScheme: "light" });
    assert.equal(await page.evaluate(() => document.documentElement.dataset.theme), "dark", "manual choice overrides the system");
    await page.getByLabel("Switch to light mode", { exact: true }).click();
    const navToggle = page.getByLabel("Toggle project navigation", { exact: true });
    await navToggle.click(); assert.equal(await page.locator(".org-sidebar").isVisible(), false);
    assert.equal(await navToggle.isVisible(), true, "navigation toggle survives closing navigation");
    await navToggle.click(); assert.equal(await page.locator(".org-sidebar").isVisible(), true);
    await page.keyboard.press("Control+b"); assert.equal(await page.locator(".org-sidebar").isVisible(), false);
    await page.keyboard.press("Meta+b"); assert.equal(await page.locator(".org-sidebar").isVisible(), true);
    await page.keyboard.press("Escape"); assert.equal(await page.locator(".org-sidebar").isVisible(), true);
    const peopleToggle = page.getByLabel("Show online people and bots", { exact: true });
    const peopleInitially = await peopleToggle.getAttribute("aria-expanded");
    await page.keyboard.press("Control+Shift+p");
    assert.notEqual(await peopleToggle.getAttribute("aria-expanded"), peopleInitially);
    await page.keyboard.press("Meta+Shift+p");
    assert.equal(await peopleToggle.getAttribute("aria-expanded"), peopleInitially);
    await page.keyboard.press("Shift+Slash"); await page.getByRole("dialog",{name:"Keyboard shortcuts and commands"}).waitFor();
    await page.keyboard.press("Shift+Slash");
    assert.equal(await page.getByRole("dialog", { name: "Keyboard shortcuts and commands" }).count(), 0);
    await page.getByLabel("Help", { exact: true }).click();
    await page.getByRole("heading", { name: "Help", exact: true }).waitFor(); await page.keyboard.press("Shift+Slash");
    assert.equal(await page.getByRole("heading", { name: "Help", exact: true }).count(), 0);
    await page.getByLabel("Message",{exact:true}).focus(); await page.keyboard.press("Shift+Slash");
    assert.equal(await page.getByRole("dialog",{name:"Keyboard shortcuts and commands"}).count(),0,"typing a question mark does not open help");
    assert.equal(await page.getByLabel("Message",{exact:true}).inputValue(),"?"); await page.getByLabel("Message",{exact:true}).fill("");
    const composer = page.getByLabel("Message", { exact: true });
    await composer.fill("@ali");
    await page.getByRole("option", { name: "alice", exact: true }).waitFor();
    await composer.press("Tab");
    assert.equal(await composer.inputValue(), "@alice ");
    assert.equal(await composer.evaluate(element => element === document.activeElement), true);
    await composer.fill("@missing"); await composer.press("Tab");
    assert.equal(await composer.evaluate(element => element === document.activeElement), true, "an unmatched active mention keeps focus");
    await composer.fill("");
    const attachment = page.getByLabel("Attach picture or PDF", { exact: true });
    assert.equal(await attachment.evaluate(element => getComputedStyle(element).borderWidth), "0px");
    assert.equal(await attachment.locator("svg").count(), 1, "attaching is the drawn plus");
    assert.equal((await attachment.boundingBox()).width, 28);
    const pages = page.context().pages().length;
    await page.getByLabel("Open bibliography",{exact:true}).click();
    assert.match(page.url(), /view=bibliography/); assert.equal(page.context().pages().length,pages);
    assert.equal(await page.locator(".org-sidebar").count(),1);
    await second().click();
    if (process.env.REASONING_SCREENSHOT_DIR) {
      await second().hover();
      await page.screenshot({ path: path.join(process.env.REASONING_SCREENSHOT_DIR, "navigation-hover-cost.png") });
    }
    await page.getByRole("button", { name: /^#channel/ }).click();
    await project().locator("summary").click();
    await page.waitForFunction(() => JSON.parse(localStorage.getItem("reasoning.navigation.v1/alice/org")).collapsedProjects.includes("project"));
    await page.reload(); await project().waitFor();
    assert.equal(await project().getAttribute("open"), null);
    assert.match(await page.locator(".org-topic-menu h1").innerText(), /Second/);
    await project().locator("summary").click();
    assert.equal(await page.getByRole("button", { name: /^#channel/ }).getAttribute("aria-expanded"), "false");
    for (const width of [320, 375]) {
      await page.setViewportSize({ width, height: 700 });
      await navToggle.click(); assert.equal(await page.locator(".org-sidebar").isVisible(), true);
      await page.keyboard.press("Escape"); assert.equal(await page.locator(".org-sidebar").isVisible(), true);
      await navToggle.click(); assert.equal(await page.locator(".org-sidebar").isVisible(), false);
      await page.getByLabel("More topic actions", { exact: true }).click();
      for (const label of ["Search organization messages", "Switch to dark mode", "Show online people and bots", "Copy topic JSON", "Open ledger", "Open bibliography", "Open research blog", "Help"]) {
        const action = page.getByLabel(label, { exact: true });
        assert.equal(await action.isVisible(), true, label);
        const box = await action.boundingBox(); assert.ok(box.x >= 0 && box.x + box.width <= width, label);
      }
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await page.getByLabel("More topic actions", { exact: true }).click();
    }
    account = "bob"; await page.setViewportSize({ width: 1200, height: 800 }); await page.goto(server.resolvedUrls.local[0] + "organization.html"); await project().waitFor();
    assert.notEqual(await project().getAttribute("open"), null);
    assert.equal(await page.getByRole("button", { name: /^#channel/ }).getAttribute("aria-expanded"), "true");
    assert.match(await page.locator(".org-topic-menu h1").innerText(), /First/);
    await page.getByLabel("Account menu").click(); await page.getByLabel("Organization", { exact: true }).selectOption("other");
    await page.waitForFunction(() => document.querySelector(".org-organization-name")?.textContent === "Other");
    assert.notEqual(await project().getAttribute("open"), null);
    await page.getByLabel("Account menu").click(); await page.getByRole("button", { name: "Model capabilities", exact: true }).click();
    const capabilities = page.getByRole("region", { name: "Model capabilities", exact: true });
    await capabilities.getByLabel("Intelligence index 71.2", { exact: true }).waitFor();
    assert.equal(await capabilities.getByLabel("Agentic index 58.3", { exact: true }).isVisible(), true);
    assert.match(await capabilities.locator(".org-catalog-status").last().innerText(), /Source: Artificial Analysis via OpenRouter.*2026.*cached.*CC BY 4.0/);
    const detail = capabilities.locator("details.org-model-card");
    assert.equal(await detail.getAttribute("open"), null);
    await detail.locator("summary").click();
    assert.match(await detail.innerText(), /coding 65.8/);
    await page.keyboard.press("Escape");
    assert.deepEqual(errors, []);
  } finally { await browser.close(); await server.close(); }
});
