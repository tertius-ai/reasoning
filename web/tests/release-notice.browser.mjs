// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { fileURLToPath } from "node:url";
import { realpath } from "node:fs/promises";
import { chromium } from "playwright";
import { createServer } from "vite";
import { notesBlock } from "../release-notes.mjs";

const loaded = "a".repeat(64), newer = "b".repeat(64);
const entry = (digit, date, number, title) => ({ commit: digit.repeat(40), date, number, title });
const older = [entry("2", "2026-10-25T10:00:00Z", 70, "Message your own bot privately from People")];
const added = [entry("4", "2026-10-26T16:00:00Z", 75, "List the files a reply saved as chips"),
  entry("3", "2026-10-26T12:00:00Z", 72, "Show a cancelled compare as cancelled with cost unknown")];
const NOTES = {
  [loaded]: { format: 1, revision: "2".repeat(40), revision_date: older[0].date, entries: older },
  [newer]: { format: 1, revision: "4".repeat(40), revision_date: added[0].date, entries: [...added, ...older] },
};
const BLOCK = /<script type="application\/json" id="reasoning-release-notes">[\s\S]*?<\/script>/;
let server, browser;
before(async () => {
  const root = fileURLToPath(new URL("../", import.meta.url));
  server = await createServer({ root, logLevel: "silent", server: { host: "127.0.0.1", port: 0, fs: { allow: [root, await realpath(root + "node_modules")] } } });
  await server.listen();
  browser = await chromium.launch({ headless: true });
});
after(async () => { await browser?.close(); await server?.close(); });

/**
 * Open the signed-in application against a mocked server. `state.page` is the release the server embeds in the page,
 * with its notes, and `state.header` the release each API response reports; null leaves either out.
 */
async function open(state, context) {
  context ??= await browser.newContext({ viewport: { width: 1200, height: 800 } });
  const page = await context.newPage(), errors = [];
  page.on("pageerror", error => errors.push(error.message));
  let polls = 0;
  await page.route(url => url.pathname === "/organization.html", async route => {
    const response = await route.fetch();
    let html = (await response.text()).replace(BLOCK, notesBlock(NOTES[state.page] ?? { entries: [] }));
    if (state.page) html = html.replace("</head>", `<meta name="reasoning-release" content="${state.page}" /></head>`);
    await route.fulfill({ response, body: html });
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
  const button = page.locator(".org-whats-new > button");
  return { page, context, errors, nextPoll, button, panel: page.getByRole("dialog", { name: "What's new" }) };
}

test("a newer release lists its merged changes and offers a reload that never happens by itself", { timeout: 90000 }, async () => {
  const state = { page: loaded, header: loaded };
  const { page, context, errors, nextPoll, button, panel } = await open(state);
  try {
    await page.evaluate(() => { window.__sameDocument = true; });
    await nextPoll();
    assert.equal(await button.getAttribute("aria-label"), "What's new", "the first visit counts the current version as seen");
    assert.equal(await button.getAttribute("data-attention"), null);

    state.header = newer;
    state.page = newer;
    await page.getByLabel("What's new: a new version is available").waitFor({ timeout: 10_000 });
    await button.click();
    await panel.getByText("New version available").waitFor();
    for (const item of added) await panel.getByText(item.title).waitFor();
    assert.equal(await panel.getByText(older[0].title).count(), 0, "changes the page already has are not listed");
    assert.ok((await panel.innerText()).includes("Reloading discards unsent drafts"));
    await page.keyboard.press("Escape");
    assert.equal(await panel.count(), 0);
    await nextPoll();
    assert.equal(await page.evaluate(() => window.__sameDocument), true, "the page is not reloaded automatically");
    assert.equal(await button.getAttribute("aria-label"), "What's new: a new version is available", "closing keeps the offer");

    await button.click();
    await panel.getByRole("button", { name: "Reload to update" }).click();
    await page.getByLabel("Help", { exact: true }).waitFor();
    assert.equal(await page.evaluate(() => window.__sameDocument), undefined, "Reload loads the page again");
    await page.getByLabel("What's new: 2 changes since you last looked").waitFor({ timeout: 10_000 });
    await button.click();
    await panel.getByText("What's new").first().waitFor();
    for (const item of added) await panel.getByText(item.title).waitFor();
    await page.keyboard.press("Escape");
    assert.equal(await button.getAttribute("aria-label"), "What's new", "opening the notes marks them seen");
    await page.reload();
    await page.getByLabel("Help", { exact: true }).waitFor();
    assert.equal(await page.locator(".org-whats-new > button").getAttribute("aria-label"), "What's new", "seen notes stay seen");
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
});

test("an unknown page or response release never offers a new version", { timeout: 60000 }, async () => {
  for (const state of [{ page: null, header: loaded }, { page: loaded, header: null }]) {
    const { page, context, errors, nextPoll } = await open(state);
    try {
      await nextPoll();
      state.header = state.header ? newer : null;
      await nextPoll();
      await page.getByLabel("Help", { exact: true }).click();
      await page.getByLabel("Server status").getByText("OK · initialized").waitFor();
      assert.equal(await page.getByLabel("What's new: a new version is available").count(), 0, JSON.stringify(state));
      assert.deepEqual(errors, []);
    } finally { await context.close(); }
  }
});
