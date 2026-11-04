// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, realpath, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

test("file recovery saves one copy without inference, and editing a long source preserves its complete UTF-8 text", { timeout: 60000 }, async t => {
  const root = fileURLToPath(new URL("../", import.meta.url)), org = "a".repeat(32), topic = "b".repeat(32);
  const apiRoot = `/api/organizations/${org}/topics/${topic}/research-files`;
  const entry = `import React from "react"; import {createRoot} from "react-dom/client";
import {ResearchFiles} from "/src/ResearchFiles.tsx";
createRoot(document.getElementById('root')).render(React.createElement(ResearchFiles, {
  organizationId:'${org}', topicId:'${topic}', canWrite:true, onAuthLost:()=>{throw new Error('Unexpected sign-out');}
}));`;
  let serveDownload;
  const plugin = { name: "research-files-fixture",
    resolveId(id) { if (id === "/research-files-fixture.tsx") return "\0research-files-fixture"; },
    load(id) { if (id === "\0research-files-fixture") return entry; },
    configureServer(server) {
      server.middlewares.use(async (req, res, next) => {
        if (serveDownload?.(req, res)) return;
        if (req.url !== "/research-files-fixture.html") return next();
        res.setHeader("Content-Type", "text/html");
        res.end(await server.transformIndexHtml(req.url,
          '<!doctype html><div id="root"></div><script type="module" src="/research-files-fixture.tsx"></script>'));
      });
    },
  };
  // Fixture plugins must not invalidate another frontend gate's optimized dependency URLs.
  const cacheDir = await mkdtemp(join(tmpdir(), "reasoning-files-vite-"));
  t.after(() => rm(cacheDir, { recursive: true, force: true }));
  const server = await createServer({ root, cacheDir, plugins: [plugin], server: { host: "127.0.0.1", port: 0,
    fs: { allow: [root, cacheDir, await realpath(root + "node_modules")] } } });
  await server.listen();
  let browser, releaseRecovery;
  try {
    browser = await chromium.launch({ headless: true,
      ...(process.env.PLAYWRIGHT_EXECUTABLE_PATH ? { executablePath: process.env.PLAYWRIGHT_EXECUTABLE_PATH } : {}) });
    const page = await browser.newPage(), errors = [], loadFailures = [], apiRequests = [], writes = [], unexpected = [], external = [];
    const origin = new URL(server.resolvedUrls.local[0]).origin;
    const paragraph = '\\section{A "quoted" result}\nFor α, β and 研究: $\\sum_i a_i = \\frac{n(n+1)}{2}$.\n';
    const source = "\\documentclass{article}\n\\begin{document}\n" + paragraph.repeat(1500) + "\\end{document}\n";
    assert.ok(source.length > 90609 && Buffer.byteLength(source) < 256 * 1024);
    const checksum = createHash("sha256").update(source).digest("hex");
    const makeFile = (id, path, revision) => {
      const url = `${apiRoot}/content?path=${encodeURIComponent(path)}&revision=${revision}`;
      return { id, path, revision, head_revision: revision, sha256: checksum, total_characters: source.length,
        url, download_url: url + "&download=true" };
    };
    const published = makeFile("published", "long-survey.tex", 3);
    const recovered = makeFile("recovered", "retained.recovered-42-0.tex", 1);
    const candidate = { event_sequence: 42, tool_index: 0, path: "retained.tex", recovery_path: recovered.path,
      sha256: checksum, size_bytes: Buffer.byteLength(source) };
    const draftText = "\\documentclass{article}\nOnly the first saved section.\n";
    const draft = { draft_id: "unfinished-draft", path: "work-in-progress.tex", size_bytes: Buffer.byteLength(draftText) };
    let wasRecovered = false, recoveryStarted;
    const recoveryRequest = new Promise(resolve => { recoveryStarted = resolve; });
    const recoveryGate = new Promise(resolve => { releaseRecovery = resolve; });
    // Chromium's download manager bypasses page routing. This middleware
    // serves fixture downloads before Vite's ordinary development API proxy.
    serveDownload = (request, response) => {
      const url = new URL(request.url, origin), path = url.pathname;
      if (!path.startsWith("/api/")) return false;
      apiRequests.push({ method: request.method, path, query: url.search });
      let bytes, filename;
      if (request.method === "GET" && path === apiRoot + `/drafts/${draft.draft_id}/content`) {
        bytes = Buffer.from(draftText); filename = "unfinished-source.txt";
      } else if (request.method === "GET" && path === apiRoot + "/content" && url.searchParams.get("download") === "true") {
        const file = url.searchParams.get("path") === published.path ? published
          : wasRecovered && url.searchParams.get("path") === recovered.path ? recovered : null;
        assert.ok(file && url.searchParams.get("revision") === String(file.revision), "download selects the saved revision");
        bytes = Buffer.from(source); filename = file.path;
      } else {
        unexpected.push({ method: request.method, path });
        response.writeHead(404); response.end("Unexpected fixture request"); return true;
      }
      response.writeHead(200, { "Content-Type": "text/plain; charset=utf-8", "Content-Disposition": `attachment; filename="${filename}"` });
      response.end(bytes); return true;
    };
    page.on("pageerror", error => errors.push(error.message));
    page.on("requestfailed", request => loadFailures.push({ url: request.url(), error: request.failure()?.errorText }));
    page.on("response", response => { if (response.status() >= 400) loadFailures.push({ url: response.url(), status: response.status() }); });
    // Every API response is local fixture data. Unexpected or remote requests
    // fail the test instead of reaching a provider or an application backend.
    await page.route("**/*", async route => {
      if (new URL(route.request().url()).origin === origin) await route.continue();
      else { external.push(route.request().url()); await route.abort(); }
    });
    await page.route("**/api/**", async route => {
      const request = route.request(), url = new URL(request.url()), path = url.pathname;
      apiRequests.push({ method: request.method(), path, query: url.search });
      if (request.method() !== "GET") writes.push({ method: request.method(), path, body: request.postDataJSON() });
      let data = [];
      if (path === apiRoot && request.method() === "GET") data = wasRecovered ? [recovered, published] : [published];
      else if (path === apiRoot + "/recovery" && request.method() === "GET") data = wasRecovered ? [] : [candidate];
      else if (path === apiRoot + "/recovery" && request.method() === "POST") {
        recoveryStarted();
        await recoveryGate;
        wasRecovered = true; data = recovered;
      } else if (path === apiRoot + "/drafts" && request.method() === "GET") data = [draft];
      else if (path === `/api/organizations/${org}/topics/${topic}/media` && request.method() === "GET") data = [];
      else if (path === apiRoot + "/read" && request.method() === "GET") {
        const file = url.searchParams.get("path") === published.path ? published
          : wasRecovered && url.searchParams.get("path") === recovered.path ? recovered : null;
        assert.ok(file, "preview selects an available saved file");
        data = { ...file, text: source.slice(0, 16000), truncated: true };
      } else if (path === apiRoot + "/content" && request.method() === "GET") {
        const file = url.searchParams.get("path") === published.path ? published
          : wasRecovered && url.searchParams.get("path") === recovered.path ? recovered : null;
        assert.ok(file && url.searchParams.get("revision") === String(file.revision), "content is pinned to the saved revision");
        await route.fulfill({ status: 200, contentType: "text/plain; charset=utf-8", body: Buffer.from(source),
          headers: url.searchParams.get("download") === "true" ? { "Content-Disposition": `attachment; filename="${file.path}"` } : {} });
        return;
      } else if (path === apiRoot + `/drafts/${draft.draft_id}/content` && request.method() === "GET") {
        await route.fulfill({ status: 200, contentType: "text/plain; charset=utf-8", body: draftText,
          headers: { "Content-Disposition": 'attachment; filename="unfinished-source.txt"' } });
        return;
      } else {
        unexpected.push({ method: request.method(), path });
        await route.fulfill({ status: 404, contentType: "application/json", body: '{"detail":"Unexpected fixture request"}' });
        return;
      }
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(data) });
    });
    const downloadedBytes = async link => {
      const pending = page.waitForEvent("download"); await link.click();
      const download = await pending, stream = await download.createReadStream(), chunks = [];
      for await (const chunk of stream) chunks.push(chunk);
      assert.equal(await download.failure(), null);
      return { bytes: Buffer.concat(chunks), name: download.suggestedFilename() };
    };
    await page.goto(server.resolvedUrls.local[0] + "research-files-fixture.html");
    const recoverable = page.getByRole("region", { name: "Recoverable files", exact: true });
    const unfinished = page.getByRole("region", { name: "Unfinished file drafts", exact: true });
    await recoverable.getByText(candidate.path, { exact: true }).waitFor().catch(error => {
      throw new Error("Recovery fixture did not load: " + JSON.stringify({ errors, loadFailures, apiRequests }), { cause: error });
    });
    await unfinished.getByText(draft.path, { exact: true }).waitFor();
    assert.match(await recoverable.innerText(), /without another model call/);
    assert.match(await unfinished.innerText(), /only the sections saved so far/);
    assert.equal(await unfinished.getByRole("button", { name: "Recover a copy", exact: true }).count(), 0);
    const draftLink = unfinished.getByRole("link", { name: "Download unfinished source", exact: true });
    assert.equal(await draftLink.getAttribute("href"), `${apiRoot}/drafts/${draft.draft_id}/content`);
    const partial = await downloadedBytes(draftLink);
    assert.ok(partial.bytes.equals(Buffer.from(draftText)), "unfinished download contains only the retained sections");

    const recover = recoverable.getByRole("button", { name: "Recover a copy", exact: true });
    await recover.click();
    await recoveryRequest;
    await page.locator("button:disabled", { hasText: "Recover a copy" }).waitFor();
    assert.equal(await recover.isDisabled(), true, "an in-flight recovery cannot be submitted again");
    assert.deepEqual(writes, [{ method: "POST", path: apiRoot + "/recovery", body: {
      event_sequence: candidate.event_sequence, tool_index: candidate.tool_index,
      sha256: checksum, path: candidate.recovery_path,
    } }]);
    releaseRecovery();
    const downloadLink = page.getByRole("link", { name: "Download complete file", exact: true });
    await downloadLink.waitFor();
    assert.equal(await downloadLink.getAttribute("href"), recovered.download_url);
    const complete = await downloadedBytes(downloadLink);
    assert.equal(complete.name, recovered.path);
    assert.ok(complete.bytes.equals(Buffer.from(source)), "recovery delivers the retained source byte for byte");
    await page.getByRole("button", { name: "‹ All files", exact: true }).click();
    await page.getByRole("button", { name: "Open " + recovered.path, exact: true }).waitFor();
    await recoverable.waitFor({ state: "detached" });
    assert.equal(await recoverable.count(), 0, "the recovered copy is now a published file");
    await unfinished.getByRole("link", { name: "Download unfinished source", exact: true }).waitFor();

    await page.getByRole("button", { name: "Open " + published.path, exact: true }).click();
    const preview = page.getByLabel("File source", { exact: true });
    await preview.waitFor();
    assert.equal(await preview.textContent(), source.slice(0, 16000));
    assert.match(await page.getByText(/Showing a bounded preview/).innerText(), /complete saved revision/);
    const fullReads = () => apiRequests.filter(request => request.path === apiRoot + "/content"
      && new URLSearchParams(request.query).get("path") === published.path);
    assert.equal(fullReads().length, 0, "opening a file loads its bounded preview first");
    await page.getByRole("button", { name: "Edit", exact: true }).click();
    const editor = page.getByRole("textbox", { name: "Content", exact: true });
    await editor.waitFor({ timeout: 5000 }).catch(async error => {
      throw new Error("Complete-source editor did not open: " + JSON.stringify({
        alerts: await page.getByRole("alert").allTextContents(), reads: fullReads(),
        textareas: await page.locator("textarea").count(), errors,
      }), { cause: error });
    });
    assert.ok(await editor.inputValue() === source, "editing fetches the complete source rather than the preview");
    assert.equal(fullReads().length, 1);
    assert.equal(await page.getByRole("button", { name: "Save revision", exact: true }).isDisabled(), false);
    const tooManyBytes = "😀".repeat(65537);
    assert.ok(tooManyBytes.length < 256 * 1024 && Buffer.byteLength(tooManyBytes) > 256 * 1024);
    await editor.fill(tooManyBytes);
    assert.ok(await editor.inputValue() === tooManyBytes, "the byte check is separate from the textarea character limit");
    await page.getByRole("alert").filter({ hasText: "at most 256 KiB of UTF-8 text" }).waitFor();
    assert.equal(await page.getByRole("button", { name: "Save revision", exact: true }).isDisabled(), true);
    const tooManyCharacters = "x".repeat(300000);
    await editor.fill(""); await editor.focus(); await page.keyboard.insertText(tooManyCharacters);
    assert.ok(await editor.inputValue() === tooManyCharacters, "oversized inserted source is preserved instead of silently clipped");
    assert.equal(await page.getByRole("button", { name: "Save revision", exact: true }).isDisabled(), true);
    await page.getByRole("alert").filter({ hasText: "at most 256 KiB of UTF-8 text" }).waitFor();
    assert.equal(writes.length, 1, "recovery is the only write; oversized edits cannot save or start inference");
    assert.deepEqual(unexpected, []);
    assert.deepEqual(external, []);
    assert.deepEqual(errors, []);
  } finally {
    releaseRecovery?.();
    await browser?.close(); await server.close();
  }
});

test("file navigation discards delayed previews, edits and save callbacks from an earlier view", { timeout: 60000 }, async t => {
  const root = fileURLToPath(new URL("../", import.meta.url)), org = "a".repeat(32), topic = "b".repeat(32), nextTopic = "c".repeat(32);
  const apiRoot = `/api/organizations/${org}/topics/${topic}/research-files`;
  const entry = `import React from "react"; import {createRoot} from "react-dom/client";
import {ResearchFiles} from "/src/ResearchFiles.tsx";
const root = createRoot(document.getElementById('root'));
const render = topicId => root.render(React.createElement(ResearchFiles, {
  organizationId:'${org}', topicId, canWrite:true, onAuthLost:()=>{throw new Error('Unexpected sign-out');}
})); render('${topic}');
document.getElementById('next-topic').onclick = () => render('${nextTopic}');`;
  const plugin = { name: "research-files-navigation-fixture",
    resolveId(id) { if (id === "/navigation-fixture.tsx") return "\0navigation-fixture"; },
    load(id) { if (id === "\0navigation-fixture") return entry; },
    configureServer(server) {
      server.middlewares.use(async (req, res, next) => {
        if (req.url !== "/navigation-fixture.html") return next();
        res.setHeader("Content-Type", "text/html");
        res.end(await server.transformIndexHtml(req.url, '<!doctype html><button id="next-topic">Switch fixture topic</button>'
          + '<div id="root"></div><script type="module" src="/navigation-fixture.tsx"></script>'));
      });
    },
  };
  const cacheDir = await mkdtemp(join(tmpdir(), "reasoning-files-vite-"));
  t.after(() => rm(cacheDir, { recursive: true, force: true }));
  const server = await createServer({ root, cacheDir, plugins: [plugin], server: { host: "127.0.0.1", port: 0,
    fs: { allow: [root, cacheDir, await realpath(root + "node_modules")] } } });
  await server.listen();
  let browser;
  const holds = {}, held = [];
  const hold = name => {
    let start, release, finish;
    const value = { started: new Promise(resolve => { start = resolve; }), release: () => release(),
      gate: new Promise(resolve => { release = resolve; }), finished: new Promise(resolve => { finish = resolve; }), start, finish };
    holds[name] = value; held.push(value); return value;
  };
  try {
    browser = await chromium.launch({ headless: true,
      ...(process.env.PLAYWRIGHT_EXECUTABLE_PATH ? { executablePath: process.env.PLAYWRIGHT_EXECUTABLE_PATH } : {}) });
    const page = await browser.newPage(), writes = [], errors = [], unexpected = [];
    const origin = new URL(server.resolvedUrls.local[0]).origin;
    const sourceA = "\\section{File A}\nAn independent long source.\n".repeat(1000), sourceB = "The independent source of file B.\n";
    const makeFile = (path, text) => ({ id: path, path, revision: 1, head_revision: 1,
      sha256: createHash("sha256").update(text).digest("hex"), total_characters: text.length,
      text: text.slice(0, 16000), truncated: text.length > 16000, url: apiRoot + `/content?path=${path}&revision=1` });
    const a = makeFile("a.tex", sourceA), b = makeFile("b.tex", sourceB);
    page.on("pageerror", error => errors.push(error.message));
    await page.route("**/*", async route => {
      if (new URL(route.request().url()).origin === origin) await route.continue();
      else { unexpected.push(route.request().url()); await route.abort(); }
    });
    await page.route("**/api/**", async route => {
      const request = route.request(), url = new URL(request.url()), path = url.pathname;
      let value = [], pending;
      if (path === apiRoot && request.method() === "GET") value = [a, b];
      else if (path === apiRoot + "/read") {
        value = url.searchParams.get("path") === a.path ? a : b;
        if (value === a) pending = holds.preview;
      } else if (path === apiRoot + "/content") pending = holds.edit;
      else if (path === apiRoot && request.method() === "POST") {
        const body = request.postDataJSON(); writes.push(body); pending = holds.save;
        value = { ...b, text: body.text, revision: 2, sha256: createHash("sha256").update(body.text).digest("hex") };
      } else if (!path.endsWith("/recovery") && !path.endsWith("/drafts") && !path.endsWith("/media")
        && path !== apiRoot.replace(topic, nextTopic)) unexpected.push(path);
      if (pending) {
        for (const name of Object.keys(holds)) if (holds[name] === pending) delete holds[name];
        pending.start(); await pending.gate;
      }
      if (path === apiRoot + "/content") await route.fulfill({ status: 200, contentType: "text/plain; charset=utf-8", body: sourceA });
      else await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(value) });
      pending?.finish();
    });
    const painted = () => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    const sourceView = () => page.getByLabel("File source", { exact: true });
    const back = () => page.getByRole("button", { name: "‹ All files", exact: true }).click();
    const open = path => page.getByRole("button", { name: "Open " + path, exact: true }).click();
    const edit = () => page.getByRole("button", { name: "Edit", exact: true }).click();
    const editor = () => page.getByRole("textbox", { name: "Content", exact: true });
    await page.goto(server.resolvedUrls.local[0] + "navigation-fixture.html");

    // A slow preview cannot replace the later selection.
    const preview = hold("preview");
    await open(a.path); await preview.started;
    await open(b.path); await sourceView().waitFor();
    preview.release(); await preview.finished; await painted();
    assert.equal(await sourceView().textContent(), sourceB);

    // A slow complete-source read cannot edit B using A's text and B's hash.
    await back(); await open(a.path); await sourceView().waitFor();
    const load = hold("edit"); await edit(); await load.started;
    await back(); await open(b.path); await sourceView().waitFor();
    assert.equal(await page.getByRole("button", { name: "Edit", exact: true }).isEnabled(), true);
    await edit(); await editor().waitFor();
    assert.equal(await editor().inputValue(), sourceB);
    load.release(); await load.finished; await painted();
    assert.equal(await page.getByRole("textbox", { name: "Filename", exact: true }).inputValue(), b.path);
    assert.equal(await editor().inputValue(), sourceB);

    // A cancelled edit is discarded when the same file is edited again.
    await editor().fill("An edit the user cancels.");
    await page.getByRole("button", { name: "Cancel", exact: true }).click(); await edit();
    assert.equal(await editor().inputValue(), sourceB);

    // A completed save refreshes the list but cannot reopen its old file over a later view.
    const save = hold("save");
    await editor().fill("An intentional revision of file B.");
    await page.getByRole("button", { name: "Save revision", exact: true }).click(); await save.started;
    assert.equal(await editor().isDisabled(), true);
    await back(); await open(a.path); await sourceView().waitFor();
    save.release(); await save.finished; await painted();
    assert.ok(await sourceView().textContent() === sourceA.slice(0, 16000));
    assert.equal(writes.length, 1);
    assert.deepEqual(writes[0], { path: b.path, text: "An intentional revision of file B.", expected_sha256: b.sha256 });

    // A topic switch cannot inherit a source or an edit from the previous topic.
    const switched = hold("edit"); await edit(); await switched.started;
    await page.getByRole("button", { name: "Switch fixture topic", exact: true }).click();
    await page.getByRole("button", { name: "New file", exact: true }).waitFor();
    switched.release(); await switched.finished; await painted();
    assert.equal(await editor().count(), 0);
    assert.equal(await sourceView().count(), 0);
    assert.deepEqual(errors, []); assert.deepEqual(unexpected, []);
  } finally {
    for (const value of held) value.release();
    await browser?.close(); await server.close();
  }
});
