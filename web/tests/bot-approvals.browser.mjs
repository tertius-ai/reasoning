// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { realpath } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

test("People replaces the bot status dot with durable exact-action approvals", { timeout: 60000 }, async () => {
  const root = fileURLToPath(new URL("../", import.meta.url));
  const server = await createServer({ root, server: { host: "127.0.0.1", port: 0,
    fs: { allow: [root, await realpath(root + "node_modules")] } } });
  await server.listen(); const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1300, height: 900 } }), errors = [], writes = [];
    page.on("pageerror", error => errors.push(error.message));
    const user = "a".repeat(32), bot = "b".repeat(32), org = "c".repeat(32), topic = "d".repeat(32);
    const base = { bot_id: bot, run_id: "run", requester_name: "Bob", topic_name: "Research", created_at: 1_800_000_000 };
    let approvals = [
      { ...base, id: "execution", kind: "execution", command_json: '["python3","-c","print(4)"]', command_sha256: "e".repeat(64) },
      { ...base, id: "issue", kind: "issue", repository: "research/bench", title: "Exact issue", body: "**Review me**, not secrets.", document_sha256: "f".repeat(64) },
      { ...base, id: "compute", kind: "compute", image_digest: "sha256:" + "a".repeat(64), request_sha256: "a".repeat(64),
        parameters: { image_id: "research-cpu", language: "sage", program: "print(factor(12))", resources: { cpu: 1, memory_mb: 512, wall_seconds: 30 } } },
    ];
    const hierarchy = { projects: [{ id: "project", name: "Project", visibility: "organization" }],
      channels: [{ id: "channel", name: "channel", project_id: "project" }],
      topics: [{ id: topic, name: "Research", channel_id: "channel", kind: "alpha", default_model: null, knowledge_tools: [] }], topic_state: [] };
    await page.route("**/api/**", async route => {
      const request = route.request(), path = new URL(request.url()).pathname;
      let data = [];
      if (path === "/api/health") data = { initialized: true };
      else if (path === "/api/auth/me") data = { id: user, email: "alice@example.org", name: "Alice" };
      else if (path === "/api/organizations") data = [{ id: org, name: "Research", role: "owner" }];
      else if (path.endsWith("/hierarchy")) data = hierarchy;
      else if (path.endsWith("/members")) data = [{ id: user, name: "Alice", role: "owner" }];
      else if (path.endsWith("/presence")) data = { humans: [{ id: user, name: "Alice", online: true }],
        bots: [{ id: bot, name: "Math Bot", owner_user_id: user, online: false, engine: "codex", models: ["default"] }] };
      else if (path.endsWith("/pending-approvals")) data = approvals;
      else if (request.method() === "POST" && /\/(execution|issue|compute)-approvals\//.test(path)) {
        writes.push({ path, body: request.postDataJSON() });
        approvals = approvals.filter(row => row.id !== path.split("/").at(-1)); data = { status: "approved" };
      }
      else if (path.endsWith("/navigation-usage")) data = { projects: {}, channels: {}, topics: {} };
      else if (path.endsWith("/message-updates")) data = { after: 0, messages: [] };
      else if (path.endsWith("/model-prices")) data = { models: [] };
      else if (path.endsWith("/compute-configuration")) data = { images: [], policy: { allowed_images: [] } };
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(data) });
    });
    await page.goto(server.resolvedUrls.local[0] + "organization.html");
    const indicator = () => page.getByRole("button", { name: "Review 3 pending requests from Math Bot", exact: true });
    await indicator().waitFor();
    assert.equal(await page.locator(`[data-bot-id="${bot}"] i`).count(), 0, "approval replaces, rather than adds to, the connection dot");
    await indicator().click();
    const dialog = () => page.getByRole("dialog", { name: "Review requests from Math Bot", exact: true });
    await dialog().getByText("Bot offline.", { exact: false }).waitFor();
    await dialog().getByText("Run on the bot computer", { exact: true }).waitFor();
    await dialog().getByText("Publish a GitHub issue", { exact: true }).waitFor();
    await dialog().getByText("Run container computation", { exact: true }).waitFor();
    assert.equal(await dialog().locator("strong").filter({ hasText: "Review me" }).count(), 1, "issue preview renders Markdown");
    await dialog().getByRole("button", { name: "Close approval dialog" }).click();
    assert.equal(writes.length, 0, "closing the dialog neither decides nor discards requests");
    await page.reload(); await indicator().waitFor();
    // Your own bot's name opens your conversation with it; its mark opens the review.
    await indicator().click();
    const execution = dialog().locator("article").filter({ hasText: "Run on the bot computer" });
    await execution.getByRole("button", { name: "Allow once", exact: true }).click();
    await page.getByRole("button", { name: "Review 2 pending requests from Math Bot", exact: true }).waitFor();
    assert.deepEqual(writes[0].body, { allow: true, request_sha256: "e".repeat(64) });
    const issue = dialog().locator("article").filter({ hasText: "Publish a GitHub issue" });
    await issue.getByRole("button", { name: "Reject", exact: true }).click();
    await page.getByRole("button", { name: "Review 1 pending requests from Math Bot", exact: true }).waitFor();
    assert.deepEqual(writes[1].body, { allow: false, request_sha256: "f".repeat(64) });
    const compute = dialog().locator("article").filter({ hasText: "Run container computation" });
    await compute.getByRole("button", { name: "Allow once", exact: true }).click();
    await dialog().getByRole("status").filter({ hasText: "No pending requests" }).waitFor();
    assert.deepEqual(writes[2].body, { allow: true, request_sha256: "a".repeat(64) });
    assert.equal(await page.locator(`[data-bot-id="${bot}"] i[aria-label="offline"]`).count(), 1);
    approvals = [{ ...base, id: "interrupted", kind: "execution", delivery_interrupted: true,
      command_json: '["python3","-c","print(4)"]', command_sha256: "e".repeat(64) }];
    await page.reload();
    await page.getByRole("button", { name: "Review 1 pending requests from Math Bot", exact: true }).click();
    await dialog().getByRole("alert").filter({ hasText: "Bot delivery interrupted before execution" }).waitFor();
    assert.equal(await dialog().getByRole("button", { name: "Allow once", exact: true }).isDisabled(), true);
    assert.equal(await dialog().getByRole("button", { name: "Reject", exact: true }).isEnabled(), true);
    assert.equal(writes.length, 3, "interrupted transport never implies resumable approval or automatically runs anything");
    assert.deepEqual(errors, []);
  } finally { await browser.close(); await server.close(); }
});
