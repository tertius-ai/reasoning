// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { realpath } from "node:fs/promises";
import { chromium } from "playwright";
import { createServer } from "vite";

test("help shows the verified commit and server status, refreshes on request, and reports failures", { timeout: 60000 }, async () => {
  const root = fileURLToPath(new URL("../", import.meta.url));
  const server = await createServer({ root, logLevel: "silent", server: { host: "127.0.0.1", port: 0, fs: { allow: [root, await realpath(root + "node_modules")] } } });
  await server.listen();
  const browser = await chromium.launch({ headless: true });
  try {
    const context = await browser.newContext({ viewport: { width: 1200, height: 800 } });
    const page = await context.newPage(), errors = [];
    page.on("pageerror", error => errors.push(error.message));
    const revision = "0123456789abcdef0123456789abcdef01234567";
    let checks = 0, failing = false, degraded = false;
    await page.route("**/api/**", async route => {
      const path = new URL(route.request().url()).pathname;
      let data = [];
      if (path === "/api/health") {
        checks += 1;
        if (failing) return route.fulfill({ status: 502, contentType: "text/html", body: "<h1>Bad gateway</h1>" });
        // A stopped required worker: the server answers 503 with its own health report.
        if (degraded) return route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({
          status: "degraded", initialized: true, inference_enabled: true, inference_worker_running: true, workflow_worker_running: true,
          webhook_worker_running: false, release_id: "e".repeat(64), source_revision: revision, source_modified: false, schema_version: 42 }) });
        data = { status: "ok", initialized: true, mail_worker_running: true, release_id: "e".repeat(64), source_revision: revision,
          source_modified: false, schema_version: 42, uptime_seconds: 7260 };
      } else if (path === "/api/auth/me") data = { id: "alice", email: "alice@example.org", name: "alice" };
      else if (path === "/api/organizations") data = [{ id: "org", name: "Research", role: "owner" }];
      else if (path.endsWith("/hierarchy")) data = { projects: [], channels: [], topics: [], topic_state: [] };
      else if (path.endsWith("/presence")) data = { humans: [], bots: [] };
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(data) });
    });
    await page.goto(server.resolvedUrls.local[0] + "organization.html");
    await page.getByLabel("Help", { exact: true }).waitFor();
    const startup = checks;
    await page.getByLabel("Help", { exact: true }).click();
    const summary = page.getByLabel("Server status");
    await summary.getByText("OK · initialized · mail worker running · schema 42 · up 2 h 1 min").waitFor();
    // Development StrictMode mounts the page twice and aborts the first check, so count from the settled view.
    assert.ok(checks > startup, "opening help checks status");
    const opened = checks;
    const link = summary.getByRole("link", { name: revision.slice(0, 12) });
    assert.equal(await link.getAttribute("href"), "https://github.com/tertius-ai/reasoning/commit/" + revision);
    assert.equal(await link.getAttribute("rel"), "noopener noreferrer");
    assert.ok((await summary.innerText()).includes("release eeeeeeeeeeee"));
    await page.setViewportSize({ width: 390, height: 800 });
    assert.ok(await summary.evaluate(element => element.scrollWidth <= element.clientWidth
      && element.getBoundingClientRect().right <= window.innerWidth), "the summary fits a phone");
    await page.setViewportSize({ width: 1200, height: 800 });
    const checked = await summary.locator("time").getAttribute("datetime");
    assert.ok(checked);

    await page.waitForTimeout(1500);
    assert.equal(checks, opened, "status is not polled");
    failing = true;
    await summary.getByRole("button", { name: "Refresh" }).click();
    await summary.getByText("Unavailable: server responded with HTTP 502").waitFor();
    assert.equal(await summary.getByRole("link").count(), 0, "a failed check does not keep a stale commit link");
    assert.equal(checks, opened + 1, "refresh checks once");

    failing = false; degraded = true;
    await summary.getByRole("button", { name: "Refresh" }).click();
    await summary.getByText("Degraded · initialized · webhook worker stopped · schema 42").waitFor();
    await summary.getByRole("link", { name: revision.slice(0, 12) }).waitFor();
    assert.equal(checks, opened + 2, "a degraded report is shown, not treated as a failure");
    degraded = false;
    await context.setOffline(true);
    await summary.getByRole("button", { name: "Refresh" }).click();
    await summary.getByText("Unavailable: this device is offline").waitFor();
    assert.equal(checks, opened + 2, "an offline device does not attempt a request");
    await context.setOffline(false);
    await summary.getByRole("link", { name: revision.slice(0, 12) }).waitFor();
    assert.equal(checks, opened + 3, "reconnecting checks once");

    await page.getByRole("button", { name: "Back to chat" }).click();
    await page.getByLabel("Help", { exact: true }).click();
    await summary.getByRole("link", { name: revision.slice(0, 12) }).waitFor();
    assert.ok(checks > opened + 3, "reopening help checks again");
    assert.deepEqual(errors, []);
  } finally { await browser.close(); await server.close(); }
});
