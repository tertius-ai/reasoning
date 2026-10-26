import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { mkdtemp, readFile, realpath, rm } from "node:fs/promises";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { chromium } from "playwright";

const repository = fileURLToPath(new URL("../../", import.meta.url));
const password = "long public chat test password";

async function visible(locator) { await locator.waitFor({ state: "visible", timeout: 15000 }); }
async function until(predicate, description) {
  const deadline = Date.now() + 15000;
  while (Date.now() < deadline) {
    if (await predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error("Timed out: " + description);
}
async function send(page, content, display = content) {
  await page.getByLabel("Message", { exact: true }).fill(content);
  await page.getByLabel("Message", { exact: true }).press("Tab");
  assert.equal(await page.getByRole("button", { name: "Send", exact: true }).evaluate((element) => element === document.activeElement), true);
  await page.keyboard.press("Enter");
  await visible(page.locator(".org-message").filter({ hasText: display }).first());
}
async function create(page, kind, name, topicKind = "alpha") {
  const trigger = kind === "project" ? "New project" : kind === "channel" ? "New channel in Algebra" : "New topic in #research";
  await page.getByRole("button", { name: trigger, exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "New " + kind });
  await dialog.getByLabel(kind[0].toUpperCase() + kind.slice(1) + " name").fill(name);
  if (kind === "organization") await dialog.getByLabel("AI access").selectOption("none");
  if (kind === "topic") await dialog.locator(`input[name="org-topic-kind"][value="${topicKind}"]`).check();
  await dialog.getByRole("button", { name: "Create " + kind, exact: true }).click();
  await dialog.waitFor({ state: "hidden" });
}
async function invite(owner, email) {
  await owner.getByRole("button", { name: "Account menu", exact: true }).click();
  await owner.getByRole("button", { name: "Administration" }).click();
  const dialog = owner.getByRole("dialog");
  await dialog.getByRole("button", { name: "Invitations", exact: true }).click();
  await dialog.getByLabel("Invite email").fill(email);
  await dialog.getByRole("button", { name: "Create invitation", exact: true }).click();
  await until(async () => (await dialog.getByLabel("Invitation link", { exact: false }).inputValue()).includes("#invite="), "invitation link");
  const link = await dialog.getByLabel("Invitation link", { exact: false }).inputValue();
  await dialog.getByRole("button", { name: "Close dialog" }).click();
  return link;
}

test("three users create an organization, exchange alpha messages, and use durable chat", { timeout: 180000 }, async (context) => {
  const temporary = await realpath(await mkdtemp(path.join(os.tmpdir(), "reasoning-chat-browser-")));
  const state = path.join(temporary, "state");
  const listener = net.createServer(); listener.listen(0, "127.0.0.1"); await once(listener, "listening");
  const port = listener.address().port; await new Promise((resolve) => listener.close(resolve));
  const url = `http://127.0.0.1:${port}`;
  const server = spawn(path.join(repository, ".venv/bin/python"), ["-m", "reasoning_ledger.server.cli", "--data-dir", state,
    "serve", "--public-url", url, "--port", String(port), "--frontend-dir", path.join(repository, "web/dist")], { cwd: repository });
  let logs = "";
  server.stdout.on("data", (chunk) => { logs += chunk; }); server.stderr.on("data", (chunk) => { logs += chunk; });
  let browser;
  try {
    await until(async () => { try { return (await fetch(url + "/api/health")).ok; } catch { return false; } }, "server readiness");
    browser = await chromium.launch({ headless: true });
    const ownerContext = await browser.newContext({ viewport: { width: 1440, height: 920 } });
    const owner = await ownerContext.newPage();
    const pageErrors = [];
    owner.on("pageerror", (error) => pageErrors.push(error.message));
    await owner.goto(url);
    await owner.getByLabel("Setup credential").fill((await readFile(path.join(state, "setup-token"), "utf8")));
    await owner.getByLabel("Email", { exact: true }).fill("owner@example.org");
    await owner.getByLabel("Your name").fill("Owner");
    await owner.getByLabel("Password", { exact: true }).fill(password);
    await owner.getByLabel("Organization name").fill("Mathematics");
    await owner.getByRole("button", { name: "Create organization", exact: true }).click();
    await visible(owner.getByRole("button", { name: "Account menu", exact: true }));
    assert.equal(await owner.getByRole("button", { name: "Sign out" }).count(), 0);
    await owner.getByRole("button", { name: "Account menu", exact: true }).click();
    await owner.getByRole("button", { name: "New organization", exact: true }).click();
    const organizationDialog = owner.getByRole("dialog", { name: "New organization" });
    await organizationDialog.getByLabel("Organization name").fill("Writing group");
    await organizationDialog.getByLabel("AI access").selectOption("none");
    await organizationDialog.getByRole("button", { name: "Create organization", exact: true }).click();
    await organizationDialog.waitFor({ state: "hidden" });
    await owner.getByRole("button", { name: "Account menu", exact: true }).click();
    await owner.locator(".org-org-selector select").selectOption({ label: "Mathematics" });
    await create(owner, "project", "Algebra"); await create(owner, "channel", "research"); await create(owner, "topic", "Planning");
    const treeNavigation = owner.getByRole("navigation", { name: "Projects and channels" });
    assert.equal(await owner.locator(".org-create-actions").count(), 0);
    assert.equal(await treeNavigation.locator(".org-channel summary").count(), 0);
    assert.match((await treeNavigation.locator(".org-channel-heading").first().innerText()).trim(), /^#research/);
    assert.match((await treeNavigation.getByRole("button", { name: /^alpha topic -Planning\b/ }).innerText()).replace(/\s+/g, ""), /^α-Planning/);
    const channelButton = treeNavigation.getByRole("button", { name: "#research", exact: true });
    assert.equal(await channelButton.getAttribute("aria-expanded"), "true"); await channelButton.click();
    assert.equal(await treeNavigation.getByRole("button", { name: /^alpha topic -Planning\b/ }).count(), 0);
    assert.equal(await channelButton.getAttribute("aria-expanded"), "false"); await channelButton.click();
    await visible(treeNavigation.getByRole("button", { name: /^alpha topic -Planning\b/ }));
    await create(owner, "channel", "notes");
    assert.match(await treeNavigation.locator(".org-channel").nth(0).getAttribute("class"), /org-tree-tone-0/);
    assert.match(await treeNavigation.locator(".org-channel").nth(1).getAttribute("class"), /org-tree-tone-1/);
    await send(owner, "Hello colleagues. \\(1+1=2\\) and \\[x^2 \\ge 0\\]", "Hello colleagues.");
    assert.equal(await owner.locator(".org-message .katex").count(), 2);
    await send(owner,"One more thought.");
    assert.equal(await owner.locator(".org-message-group").first().locator(".org-message").count(),2);
    assert.equal(await owner.locator(".org-message-group").first().locator("header strong").count(),1);
    assert.equal(await owner.locator(".org-message-day").count(),1);
    const continuation = owner.locator(".org-message-continuation").first();
    await continuation.hover();
    const timestamp = await continuation.locator("header time").boundingBox();
    const messageActions = await continuation.getByLabel("Message actions", { exact: true }).boundingBox();
    const reaction = await continuation.getByLabel("Add reaction", { exact: true }).boundingBox();
    assert.ok(timestamp.x + timestamp.width <= messageActions.x && messageActions.x + messageActions.width <= reaction.x,
      "continuation timestamp, message actions, and rightmost reaction must not overlap");

    const participants = [];
    for (const [email, name] of [["first@example.org", "Researcher"], ["second@example.org", "Reviewer"]]) {
      const link = await invite(owner, email);
      const browserContext = await browser.newContext(); const page = await browserContext.newPage();
      page.on("pageerror", (error) => pageErrors.push(error.message));
      await page.goto(link); await visible(page.getByRole("heading", { name: "Join an organization" }));
      assert.equal(new URL(page.url()).hash, "");
      await page.getByLabel("Your name").fill(name); await page.getByLabel("Choose a password", { exact: true }).fill(password);
      await page.getByLabel("Confirm password", { exact: true }).fill(password);
      await page.getByRole("button", { name: "Accept invitation", exact: true }).click();
      await visible(page.getByRole("button", { name: "Account menu", exact: true }));
      await visible(page.locator(".org-message").filter({ hasText: "Hello colleagues." }));
      await send(page, `${name} joins the discussion.`);
      participants.push({ page, browserContext });
    }
    const member = participants[0].page; const memberContext = participants[0].browserContext;
    await visible(owner.locator(".org-message").filter({ hasText: "Reviewer joins the discussion." }));

    await owner.bringToFront();
    await create(owner, "topic", "Models", "beta");
    await visible(owner.getByRole("button", { name: "Choose reply model", exact: true }));
    await send(owner, "A separate topic conversation.");
    const models = member.getByRole("navigation", { name: "Projects and channels" }).getByRole("button", { name: /^beta topic -Models\b/ });
    await visible(models.getByLabel("1 unread messages"));
    await member.bringToFront(); await models.click();
    await visible(member.locator(".org-message").filter({ hasText: "A separate topic conversation." }));
    await until(async () => (await models.getByLabel("1 unread messages").count()) === 0, "read acknowledgement updates navigation");
    await member.getByRole("navigation", { name: "Projects and channels" }).getByRole("button", { name: /^alpha topic -Planning\b/ }).click();
    await create(owner, "topic", "Proof run", "gamma");
    assert.equal(await owner.getByRole("button", { name: "Workflow", exact: true }).count(), 0);
    assert.equal(await owner.getByRole("button", { name: "Compare", exact: true }).count(), 0);
    await owner.getByLabel("Message", { exact: true }).fill("/");
    const commands = owner.getByRole("listbox", { name: "Topic commands" });
    await visible(commands);
    assert.equal(await commands.getByRole("option", { name: /\/workflow/ }).count(), 1);
    assert.equal(await commands.getByRole("option", { name: /\/compare/ }).count(), 1);
    await owner.getByLabel("Message", { exact: true }).press("Escape");
    await owner.getByLabel("Message", { exact: true }).fill("");
    assert.equal(await owner.getByLabel("Message", { exact: true }).count(), 1);
    await owner.getByRole("navigation", { name: "Projects and channels" }).getByRole("button", { name: /^alpha topic -Planning\b/ }).click();

    // Follow is a switch in the topic's details card; its state survives a reload.
    await member.locator(".org-topic-menu > summary").click();
    await member.getByRole("switch", { name: "Follow topic", checked: true }).click();
    await visible(member.getByRole("switch", { name: "Follow topic", checked: false }));
    await member.reload();
    await member.locator(".org-topic-menu > summary").click();
    await visible(member.getByRole("switch", { name: "Follow topic", checked: false }));
    await member.getByRole("switch", { name: "Follow topic" }).click();
    await visible(member.getByRole("switch", { name: "Follow topic", checked: true }));
    await member.keyboard.press("Escape");

    await owner.getByRole("button", { name: "Account menu", exact: true }).click();
    await owner.getByRole("button", { name: "Administration" }).click();
    const settings = owner.getByRole("dialog");
    await settings.getByRole("button", { name: "Inference", exact: true }).click();
    await settings.getByRole("button", { name: "+ Add key", exact: true }).click();
    assert.equal(await settings.getByLabel("Key label").inputValue(), "");
    assert.equal(await settings.getByLabel("OpenRouter API key").inputValue(), "");
    await settings.getByLabel("Key label").fill("Organization key");
    await settings.getByLabel("OpenRouter API key").fill("sk-or-test-only-organization-key");
    assert.equal(await settings.getByLabel("Allow every text-to-text model currently listed by OpenRouter").count(), 0);
    await settings.getByRole("button", { name: "Add OpenRouter key" }).click();
    await visible(settings.getByRole("button", { name: "+ Add key", exact: true }));
    assert.equal(await settings.getByLabel("OpenRouter API key").count(), 0);
    await visible(settings.locator("li").filter({ hasText: "Organization key" }));
    assert.equal(await settings.locator("li").filter({ hasText: "sk-or-test-only-organization-key" }).count(), 0);
    await settings.getByRole("button", { name: "Close dialog" }).click();

    assert.equal(await member.getByRole("button", { name: "@ Mention", exact: true }).count(), 0);
    await member.getByLabel("Message", { exact: true }).fill("@Ow");
    await visible(member.getByRole("option", { name: "Owner", exact: true }));
    await member.getByLabel("Message", { exact: true }).press("Enter");
    const mention = await member.getByLabel("Message", { exact: true }).inputValue();
    await member.getByLabel("Message", { exact: true }).fill(mention + "please review this lemma.");
    await member.getByRole("button", { name: "Send", exact: true }).click();
    await visible(owner.locator(".org-message").filter({ hasText: "@Owner please review this lemma." }));
    await member.getByLabel("Search organization messages").fill("Reviewer joins");
    await visible(member.locator(".org-search-results li"));
    await member.locator(".org-search-results li button").click();
    await visible(member.locator(".org-message").filter({ hasText: "Reviewer joins the discussion." }));

    await memberContext.setOffline(true);
    await visible(member.getByText("Reconnecting…", { exact: true }));
    await send(owner, "Message delivered after reconnect.");
    await memberContext.setOffline(false);
    await visible(member.locator(".org-message").filter({ hasText: "Message delivered after reconnect." }));
    assert.equal(await member.locator(".org-message").filter({ hasText: "Message delivered after reconnect." }).count(), 1);

    let releasePoll;
    const heldPoll = new Promise((resolve) => { releasePoll = resolve; });
    let pollHeld = false;
    await owner.route("**/messages?after=*", async (route) => {
      if (!pollHeld) { pollHeld = true; await heldPoll; }
      await route.continue();
    });
    try {
      await until(() => pollHeld, "incremental poll captured");
      await send(member, "Intervening message before owner send.");
      await send(owner, "Owner send must not skip an intervening message.");
      releasePoll();
      await visible(owner.locator(".org-message").filter({ hasText: "Intervening message before owner send." }));
    } finally { releasePoll(); await owner.unroute("**/messages?after=*"); }

    // The response is committed but deliberately hidden from the sender. Retrying
    // must reuse its client ID, not append a second message.
    let hideResponse = true;
    await member.route("**/topics/*/messages", async (route) => {
      if (route.request().method() !== "POST" || !hideResponse) return route.continue();
      hideResponse = false;
      await route.fetch(); await route.abort("failed");
    });
    await member.getByLabel("Message", { exact: true }).fill("Exactly-once browser retry.");
    await member.getByRole("button", { name: "Send", exact: true }).click();
    await until(async () => (await member.getByLabel("Message", { exact: true }).inputValue()) === "" ||
      await member.getByRole("button", { name: "Retry send", exact: true }).isVisible(), "replay confirmation or retry");
    if (await member.getByRole("button", { name: "Retry send", exact: true }).isVisible()) await member.getByRole("button", { name: "Retry send", exact: true }).click();
    await member.unroute("**/topics/*/messages");
    await visible(member.locator(".org-message").filter({ hasText: "Exactly-once browser retry." }));
    assert.equal(await member.locator(".org-message").filter({ hasText: "Exactly-once browser retry." }).count(), 1);

    const tree = await (await ownerContext.request.get(url + "/api/organizations")).json();
    const mathematics = tree.find((item) => item.name === "Mathematics");
    const hierarchy = await (await ownerContext.request.get(url + `/api/organizations/${mathematics.id}/hierarchy`)).json();
    const topic = hierarchy.topics.find((item) => item.name === "Planning");
    const csrf = (await ownerContext.cookies()).find((cookie) => cookie.name === "reasoning_csrf").value;
    for (let index = 0; index < 105; index++) {
      const response = await ownerContext.request.post(url + `/api/organizations/${mathematics.id}/topics/${topic.id}/messages`, {
        headers: { Origin: url, "X-CSRF-Token": csrf }, data: { content: `History row ${index}`, client_id: `history-${index}` },
      }); assert.equal(response.status(), 201);
    }
    await member.reload(); await visible(member.getByRole("button", { name: "Load earlier messages" }));
    await member.getByRole("button", { name: "Load earlier messages" }).click();
    await visible(member.locator(".org-message").filter({ hasText: "Researcher joins the discussion." }));

    for (const viewport of [{ width: 1440, height: 920 }, { width: 920, height: 640 }, { width: 390, height: 844 }]) {
      await member.setViewportSize(viewport);
      const overflow = await member.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
      assert.equal(overflow, false, `horizontal overflow at ${viewport.width}`);
      assert.ok(await member.getByLabel("Message", { exact: true }).isVisible());
      if (viewport.width === 390) {
        await member.getByRole("button", { name: "Toggle project navigation" }).click();
        await visible(member.getByRole("navigation", { name: "Projects and channels" }));
        await member.keyboard.press("Escape");
        assert.equal(await member.getByRole("button", { name: "Toggle project navigation" }).getAttribute("aria-expanded"), "false");
      }
    }
    await member.emulateMedia({ reducedMotion: "reduce" });
    await member.setViewportSize({ width: 920, height: 640 });
    await member.keyboard.press("Control+k");
    assert.equal(await member.getByLabel("Search organization messages").evaluate((element) => element === document.activeElement), true);
    const screenshotDirectory = process.env.REASONING_SCREENSHOT_DIR;
    if (screenshotDirectory) await member.screenshot({ path: path.join(screenshotDirectory, "public-chat.png") });

    const pagesBeforeBlog = owner.context().pages().length;
    await owner.getByRole("link", { name: "Open research blog", exact: true }).click();
    const blog = owner;
    await visible(blog.getByRole("heading", { name: "Research blog", exact: true }));
    assert.match(new URL(blog.url()).pathname, /^\/blog\//);
    assert.equal(owner.context().pages().length, pagesBeforeBlog, "research links must stay in the current tab");
    assert.equal(await blog.getByRole("button", { name: "New post", exact: true }).count(), 0);
    await blog.getByRole("link", { name: "Edit posts", exact: true }).click();
    await visible(blog.getByRole("button", { name: "New post", exact: true }));
    await until(async () => await blog.locator(".research-blog").evaluate(element => getComputedStyle(element).backgroundColor === getComputedStyle(document.querySelector(".org-sidebar")).backgroundColor), "shared blog styling loads in editor");
    assert.equal(await blog.locator(".org-sidebar").count(), 1, "editor uses the chat shell");
    await blog.getByRole("button", { name: "New post", exact: true }).click();
    await blog.getByLabel("Title", { exact: true }).fill("A published lemma");
    await blog.getByLabel("Markdown", { exact: true }).fill("An explicit **argument** over $\\mathbb{Q}$.");
    assert.ok(await blog.getByLabel("Project tag", { exact: true }).inputValue());
    await blog.getByRole("button", { name: "Preview", exact: true }).click();
    await visible(blog.locator(".org-blog-preview .katex"));
    if (screenshotDirectory) await blog.screenshot({ path: path.join(screenshotDirectory, "blog-editor.png"), fullPage: true });
    await blog.getByRole("button", { name: "Publish", exact: true }).click();
    await visible(blog.locator(".org-blog-post").getByRole("link", { name: "A published lemma", exact: true }));
    const postLink = blog.locator(".org-blog-post").getByRole("link", { name: "A published lemma", exact: true });
    assert.equal(await postLink.getAttribute("target"), null);
    const published = await blog.context().newPage();
    await published.goto(new URL(await postLink.getAttribute("href"), blog.url()).href);
    await visible(published.getByRole("heading", { name: "A published lemma", exact: true }));
    await visible(published.locator(".katex"));
    assert.equal(await published.locator(".research-blog").evaluate(element => getComputedStyle(element).backgroundColor), "rgb(255, 255, 255)");
    await published.setViewportSize({ width: 390, height: 720 });
    assert.equal(await published.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    if (screenshotDirectory) await published.screenshot({ path: path.join(screenshotDirectory, "blog-reader-mobile.png"), fullPage: true });
    await blog.setViewportSize({ width: 390, height: 720 });
    assert.equal(await blog.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await blog.getByRole("button", { name: "Edit draft", exact: true }).click();
    await blog.getByLabel("Markdown", { exact: true }).fill("An unpublished replacement.");
    await blog.getByRole("button", { name: "Save draft", exact: true }).click();
    await visible(blog.getByText("An unpublished replacement.", { exact: true }));
    await published.reload(); await visible(published.getByText("An explicit", { exact: false }));
    assert.equal(await published.getByText("An unpublished replacement.", { exact: true }).count(), 0);
    const unauthenticated = await browser.newContext();
    assert.equal((await unauthenticated.request.get(published.url())).status(), 401);
    await unauthenticated.close(); await blog.close(); await published.close();

    await member.getByRole("button", { name: "Account menu", exact: true }).click();
    await member.getByRole("button", { name: "Sign out" }).click();
    await visible(member.getByRole("heading", { name: "Sign in to reasoning" }));
    await member.reload(); await visible(member.getByRole("heading", { name: "Sign in to reasoning" }));
    assert.deepEqual(pageErrors, []);
    context.diagnostic("Verified owner setup, invitations, chat, typed topics, mathematics, reconnect, responsive layout, static-blog-to-editor navigation, shared blog styling, publication snapshots, and logout.");
  } catch (error) {
    console.error("Browser journey failed:", error.message);
    const directory = process.env.REASONING_SCREENSHOT_DIR;
    if (directory && browser) {
      for (const [index, page] of browser.contexts().flatMap((item) => item.pages()).entries()) {
        await page.screenshot({ path: path.join(directory, `failure-${index}.png`) }).catch(() => {});
      }
    }
    throw error;
  } finally {
    await browser?.close();
    server.kill("SIGTERM");
    const live = () => server.exitCode === null && server.signalCode === null;
    if (live()) await Promise.race([once(server, "exit"), new Promise((resolve) => setTimeout(resolve, 5000))]);
    if (live()) { server.kill("SIGKILL"); await once(server, "exit"); }
    assert.ok(!logs.includes(password));
    await rm(temporary, { recursive: true, force: true });
  }
});
