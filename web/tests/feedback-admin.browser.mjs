// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { realpath } from "node:fs/promises";
import { chromium } from "playwright";
import { createServer } from "vite";

test("administrators expand reports, copy one as Markdown, and resolve it into the Resolved group", { timeout: 60000 }, async () => {
  const root = fileURLToPath(new URL("../", import.meta.url));
  const server = await createServer({ root, logLevel: "silent", server: { host: "127.0.0.1", port: 0, fs: { allow: [root, await realpath(root + "node_modules")] } } });
  await server.listen();
  const browser = await chromium.launch({ headless: true });
  try {
    const context = await browser.newContext({ viewport: { width: 1200, height: 900 } });
    await context.grantPermissions(["clipboard-read", "clipboard-write"], { origin: new URL(server.resolvedUrls.local[0]).origin });
    const page = await context.newPage(), errors = [];
    page.on("pageerror", error => errors.push(error.message));
    const at = Math.floor(Date.now() / 1000) - 3600;
    const reports = [
      { id: "r1", kind: "bug", body: "Stop left the spinner running.\nExpected: the run stops.", created_at: at, resolved_at: null, resolved_by: null,
        reporter: { id: "maya", name: "Maya" }, context: { view: "Research", release: "development", browser: "Chromium", viewport: "1200×900",
          requests: [{ at, method: "POST", path: "/api/organizations/org/runs/x/interrupt", status: 502, detail: "upstream timeout" }], errors: [] } },
      { id: "r2", kind: "idea", body: "Pin a ledger claim to the top of a topic.", created_at: at - 60, resolved_at: null, resolved_by: null,
        reporter: { id: "tomas", name: "Tomás" }, context: {} },
      { id: "r3", kind: "idea", body: "A shortcut to the next unread topic.", created_at: at - 120, resolved_at: at, resolved_by: "alice",
        reporter: { id: "jonas", name: "Jonas" }, context: {} },
    ];
    let resolutions = 0;
    await page.route("**/api/**", async route => {
      const path = new URL(route.request().url()).pathname;
      let data = [];
      if (path === "/api/health") data = { status: "ok", initialized: true };
      else if (path === "/api/auth/me") data = { id: "alice", email: "alice@example.org", name: "Alice" };
      else if (path === "/api/organizations") data = [{ id: "org", name: "Research", role: "owner" }];
      else if (path.endsWith("/hierarchy")) data = { projects: [], channels: [], topics: [], topic_state: [] };
      else if (path.endsWith("/presence")) data = { humans: [], bots: [] };
      else if (path.endsWith("/feedback-reports")) data = reports;
      else if (path.endsWith("/resolution")) {
        resolutions += 1;
        const report = reports.find(item => path.includes("/" + item.id + "/"));
        const resolved = JSON.parse(route.request().postData()).resolved;
        Object.assign(report, { resolved_at: resolved ? at + 60 : null, resolved_by: resolved ? "alice" : null });
        data = report;
      } else if (/\/(members|invitations)$/.test(path)) data = [];
      else if (path.endsWith("/invitation-mail")) data = { enabled: false, daily_limit: null, worker_running: false };
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(data) });
    });
    await page.goto(server.resolvedUrls.local[0] + "organization.html");
    await page.getByRole("button", { name: "Account menu", exact: true }).click();
    await page.getByRole("button", { name: "Administration" }).click();
    const administration = page.getByRole("dialog");
    await administration.getByRole("button", { name: "Feedback", exact: true }).click();

    const openGroup = administration.getByRole("list", { name: "Open reports" });
    await openGroup.waitFor();
    await administration.getByText("2 open · 1 resolved").waitFor();
    assert.equal(await administration.getByRole("list", { name: "Resolved reports" }).count(), 0, "the Resolved group starts collapsed");
    const row = openGroup.getByRole("button", { name: /Stop left the spinner running/ });
    assert.equal(await row.getAttribute("aria-expanded"), "false");
    assert.equal(await administration.getByText("Expected: the run stops.").count(), 0, "a collapsed report shows only its first line");
    await openGroup.getByText("1 failed request, 0 page errors").waitFor({ state: "attached" });

    await row.click();
    assert.equal(await row.getAttribute("aria-expanded"), "true");
    const report = openGroup.getByRole("listitem").filter({ hasText: "Stop left the spinner running." });
    await report.getByText("Expected: the run stops.").waitFor();
    await report.getByRole("heading", { name: "Failed requests (1)" }).waitFor();

    await report.getByRole("button", { name: "Copy report", exact: true }).click();
    await report.getByRole("button", { name: "Copied", exact: true }).waitFor();
    const copied = await page.evaluate(() => navigator.clipboard.readText());
    assert.match(copied, /^### Bug report from Maya · \d{4}-\d\d-\d\d \d\d:\d\d UTC\n\nStop left the spinner running\.\nExpected: the run stops\./);
    assert.match(copied, /<details><summary>Diagnostics<\/summary>[\s\S]*POST \/api\/organizations\/org\/runs\/x\/interrupt → 502 upstream timeout[\s\S]*<\/details>$/);
    await report.getByRole("button", { name: "Copy report", exact: true }).waitFor({ timeout: 4000 });

    await report.getByRole("button", { name: "Resolve", exact: true }).click();
    await administration.getByText("1 open · 2 resolved").waitFor();
    assert.equal(resolutions, 1);
    assert.equal(await openGroup.getByRole("button", { name: /Stop left the spinner running/ }).count(), 0, "a resolved report leaves Open");
    await administration.getByRole("button", { name: /^Resolved/ }).click();
    const resolvedGroup = administration.getByRole("list", { name: "Resolved reports" });
    const moved = resolvedGroup.getByRole("listitem").filter({ hasText: "Stop left the spinner running." });
    await moved.getByRole("button", { name: "Reopen", exact: true }).waitFor();

    await administration.getByRole("button", { name: "Bugs", exact: true }).click();
    assert.equal(await administration.getByRole("button", { name: "Bugs", exact: true }).getAttribute("aria-pressed"), "true");
    await openGroup.getByText("No open reports of this kind.").waitFor();
    assert.equal(await resolvedGroup.locator(":scope > li").count(), 1, "the filter hides ideas");
    await administration.getByRole("button", { name: /^Open/ }).click();
    assert.equal(await openGroup.count(), 0, "the Open group collapses");

    await page.setViewportSize({ width: 390, height: 800 });
    assert.ok(await administration.evaluate(element => element.scrollWidth <= element.clientWidth), "the tab fits a phone");
    assert.deepEqual(errors, []);
  } finally { await browser.close(); await server.close(); }
});
