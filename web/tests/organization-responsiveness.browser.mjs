import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

test("conversations, models, and bots render while unrelated startup and metadata requests are held", { timeout: 30000 }, async () => {
  const root = fileURLToPath(new URL("../", import.meta.url));
  const server = await createServer({ root, logLevel: "silent", server: { host: "127.0.0.1", port: 0, strictPort: false } });
  await server.listen();
  const browser = await chromium.launch({ headless: true });
  const releases = [];
  const gate = () => new Promise(resolve => releases.push(resolve));
  const slowStartup = gate(), slowHierarchy = gate();
  let holdHierarchy = false;
  try {
    const page = await browser.newPage();
    page.setDefaultTimeout(5000);
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    const org = "a".repeat(32), user = "b".repeat(32), topic = "c".repeat(32), bot = "d".repeat(32);
    const hierarchy = {
      projects: [{ id: "project", name: "Project" }],
      channels: [{ id: "channel", project_id: "project", name: "research" }],
      topics: [{ id: topic, channel_id: "channel", name: "Conversation", kind: "alpha", default_model: null, knowledge_tools: [] }],
      topic_state: [],
    };
    await page.route("**/api/**", async route => {
      const path = new URL(route.request().url()).pathname;
      if (["/api/health", `/api/organizations/${org}/members`, `/api/organizations/${org}/topics/${topic}/message-updates`].includes(path)) await slowStartup;
      if (path.endsWith("/model-prices")) await slowStartup;
      if (holdHierarchy && path.endsWith("/hierarchy")) await slowHierarchy;
      let data = [];
      if (path === "/api/health") data = { initialized: true };
      else if (path === "/api/auth/me") data = { id: user, name: "Alice", email: "alice@example.test" };
      else if (path === "/api/organizations") data = [{ id: org, name: "Research", role: "owner" }];
      else if (path.endsWith("/hierarchy")) data = hierarchy;
      else if (path.endsWith("/messages")) data = [{ id: "message", topic_id: topic, sequence: 1, author_id: user,
        author_kind: "human", content: "This conversation is ready", client_id: "fixture", created_at: 1800000000 }];
      else if (path.endsWith("/message-updates")) data = { after: 0, messages: [] };
      else if (path.endsWith("/presence")) data = { humans: [], bots: [] };
      else if (path.endsWith("/navigation-usage")) data = { projects: {}, channels: {}, topics: {} };
      else if (path.endsWith("/inference-configuration")) data = { allowed_models: ["fixture/model"], tools: [] };
      else if (path.endsWith("/model-prices")) data = { models: [], allowed_models: [] };
      else if (path.includes("/reactions/")) data = { counts: { up: 0, down: 0, upside_down: 0 }, mine: [] };
      else if (path.endsWith("/my-bots")) data = [{ id: bot, name: "Ready Bot", enabled: true, scopes: ["messages:read"],
        topic_ids: [], explicit_topic_ids: [], public_access: true, expires_at: 1900000000, revoked_at: null }];
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(data) });
    });
    await page.goto(server.resolvedUrls.local[0] + "organization.html", { waitUntil: "domcontentloaded" });
    await page.getByText("This conversation is ready", { exact: true }).waitFor().catch(async error => {
      throw new Error(JSON.stringify({ errors, body: await page.locator("body").innerText() }), { cause: error });
    });
    assert.ok(releases.length, "health, members, and edit history remain held while messages render");
    holdHierarchy = true;
    await page.getByRole("button", { name: "Account menu" }).click();
    await page.getByRole("button", { name: "My bots", exact: true }).click();
    await page.locator(".org-bot-card").getByText("Ready Bot", { exact: true }).waitFor();
    await page.getByRole("dialog", { name: "My bots", exact: true }).getByRole("button", { name: "Close dialog" }).click();
    holdHierarchy = false;
    await page.getByRole("button", { name: "New topic in #research", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "New topic", exact: true });
    await dialog.getByRole("radio", { name: /AI chat/ }).check();
    await dialog.getByRole("button", { name: "Model", exact: true }).click();
    await dialog.getByRole("radio", { name: "fixture/model", exact: true }).waitFor();
    assert.deepEqual(errors, []);
  } finally {
    releases.forEach(release => release());
    await browser.close();
    await server.close();
  }
});
