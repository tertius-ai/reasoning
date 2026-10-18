// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { fileURLToPath } from "node:url";
import { realpath } from "node:fs/promises";
import { chromium } from "playwright";
import { createServer } from "vite";

const loaded = "a".repeat(64), newer = "b".repeat(64), newest = "c".repeat(64);
const NOTICE = "A new version of Reasoning is available";
let server, browser;
before(async () => {
  const root = fileURLToPath(new URL("../", import.meta.url));
  server = await createServer({ root, logLevel: "silent", server: { host: "127.0.0.1", port: 0, fs: { allow: [root, await realpath(root + "node_modules")] } } });
  await server.listen();
  browser = await chromium.launch({ headless: true });
});
after(async () => { await browser?.close(); await server?.close(); });

/**
 * Open the signed-in application against a mocked server. `state.page` is the release embedded in the page (as the
 * production server does) and `state.header` the release each API response reports; null leaves either out.
 */
async function open(state) {
  const context = await browser.newContext({ viewport: { width: 1200, height: 800 } });
  const page = await context.newPage(), errors = [];
  page.on("pageerror", error => errors.push(error.message));
  let polls = 0;
  await page.route(url => url.pathname === "/organization.html", async route => {
    const response = await route.fetch();
    const html = await response.text();
    await route.fulfill({ response, body: state.page ? html.replace("</head>", `<meta name="reasoning-release" content="${state.page}" /></head>`) : html });
  });
  await page.route("**/api/**", async route => {
    const path = new URL(route.request().url()).pathname;
    let data = [];
    if (path === "/api/health") data = { status: "ok", initialized: true, release_id: state.header };
    else if (path === "/api/auth/me") data = { id: "alice", email: "alice@example.org", name: "alice" };
    else if (path === "/api/organizations") data = [{ id: "org", name: "Research", role: "owner" }];
    else if (path.endsWith("/hierarchy")) { polls += 1; data = { projects: [], channels: [], topics: [], topic_state: [] }; }
    else if (path.endsWith("/presence")) data = { humans: [], bots: [] };
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(data),
      headers: state.header ? { "X-Reasoning-Release": state.header } : {} });
  });
  await page.goto(server.resolvedUrls.local[0] + "organization.html");
  await page.getByLabel("Help", { exact: true }).waitFor();
  /** Wait for the next hierarchy poll, an existing request, to complete. */
  const nextPoll = async () => {
    const seen = polls;
    for (let waited = 0; polls <= seen; waited += 100) {
      assert.ok(waited < 10_000, "the hierarchy poll continues");
      await page.waitForTimeout(100);
    }
    await page.waitForTimeout(200);
  };
  return { page, context, errors, nextPoll, notice: page.getByRole("status").filter({ hasText: NOTICE }) };
}

test("a changed release offers a dismissible reload and never reloads by itself", { timeout: 90000 }, async () => {
  const state = { page: loaded, header: loaded };
  const { page, context, errors, nextPoll, notice } = await open(state);
  try {
    await page.evaluate(() => { window.__sameDocument = true; });
    await nextPoll();
    assert.equal(await notice.count(), 0, "an unchanged release shows no notice");

    state.header = newer;
    await notice.waitFor({ timeout: 10_000 });
    assert.ok((await notice.innerText()).includes("Reloading discards unsent drafts"));
    await nextPoll();
    assert.equal(await page.evaluate(() => window.__sameDocument), true, "the page is not reloaded automatically");

    await notice.getByRole("button", { name: "Dismiss update notice" }).click();
    assert.equal(await notice.count(), 0);
    await nextPoll();
    assert.equal(await notice.count(), 0, "a dismissed release stays dismissed");

    state.header = newest;
    await notice.waitFor({ timeout: 10_000 });
    state.page = newest;
    await notice.getByRole("button", { name: "Reload" }).click();
    await page.getByLabel("Help", { exact: true }).waitFor();
    assert.equal(await page.evaluate(() => window.__sameDocument), undefined, "Reload loads the page again");
    await nextPoll();
    assert.equal(await notice.count(), 0, "the reloaded page runs the current release");
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
});

test("an unknown page or response release never shows the notice", { timeout: 60000 }, async () => {
  for (const state of [{ page: null, header: loaded }, { page: loaded, header: null }]) {
    const { page, context, errors, nextPoll, notice } = await open(state);
    try {
      await nextPoll();
      state.header = state.header ? newer : null;
      await nextPoll();
      await page.getByLabel("Help", { exact: true }).click();
      await page.getByLabel("Server status").getByText("OK · initialized").waitFor();
      assert.equal(await notice.count(), 0, JSON.stringify(state));
      assert.deepEqual(errors, []);
    } finally { await context.close(); }
  }
});
