// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
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
async function until(predicate) {
  const deadline = Date.now() + 25000;
  while (Date.now() < deadline) { if (await predicate()) return; await new Promise(resolve => setTimeout(resolve, 100)); }
  throw new Error("Timed out waiting for the blog fixture");
}
async function checkMath(page, root) {
  await page.locator(root + " .katex").first().waitFor({ timeout: 10000 }).catch(async error => {
    error.message += "\nRendered article: " + await page.locator("body").innerText(); throw error;
  });
  const equations = await page.locator(root + " .katex").evaluateAll(nodes => nodes.map(node => {
    const accessible = node.querySelector(".katex-mathml"), visual = node.querySelector(".katex-html");
    const style = accessible && getComputedStyle(accessible);
    return { math: !!accessible?.querySelector("math semantics annotation"),
      width: accessible?.getBoundingClientRect().width, height: accessible?.getBoundingClientRect().height,
      position: style?.position, visual: visual?.getAttribute("aria-hidden") };
  }));
  assert.equal(equations.length, 3);
  for (const equation of equations) {
    assert.equal(equation.math, true); assert.equal(equation.position, "absolute");
    assert.ok(equation.width <= 2 && equation.height <= 2, "MathML remains accessible but does not duplicate the visual equation");
    assert.equal(equation.visual, "true");
  }
  assert.equal(await page.locator(root + " .katex-error").count(), 0);
}

test("static blog and editor show one accessible equation on desktop/mobile; drafts and publications can be deleted", { timeout: 90000 }, async () => {
  const temporary = await realpath(await mkdtemp(path.join(os.tmpdir(), "reasoning-blog-browser-")));
  const socket = net.createServer().listen(0, "127.0.0.1"); await once(socket, "listening");
  const port = socket.address().port; await new Promise(resolve => socket.close(resolve));
  const url = `http://127.0.0.1:${port}`, state = path.join(temporary, "state"), errors = [], failedAssets = [];
  let server, browser, logs = "";
  try {
    // Exercise the exact public asset graph retained by release packaging,
    // including its no-build-manifest fallback rather than only raw Vite dist.
    const frontend = path.join(temporary, "frontend");
    const packer = spawn(path.join(repository, ".venv/bin/python"), ["-c",
      "from pathlib import Path\nfrom reasoning_ledger.deployment.release import public_frontend\n" +
      "import sys\nroot=Path(sys.argv[2])\n" +
      "for name,body in public_frontend(Path(sys.argv[1])).items():\n" +
      " target=root/name.removeprefix('web/'); target.parent.mkdir(parents=True,exist_ok=True); target.write_bytes(body)\n",
      path.join(repository, "web/dist"), frontend], { cwd: repository, env: { ...process.env, PYTHONPATH: path.join(repository, "src") } });
    let packing = ""; packer.stderr.on("data", chunk => { packing += chunk; });
    const [packExit] = await once(packer, "exit"); assert.equal(packExit, 0, packing);
    server = spawn(path.join(repository, ".venv/bin/python"), ["-m", "reasoning_ledger.server.cli", "--data-dir", state,
      "serve", "--public-url", url, "--port", String(port), "--frontend-dir", frontend],
      { cwd: repository, env: { ...process.env, PYTHONPATH: path.join(repository, "src") } });
    server.stdout.on("data", chunk => { logs += chunk; }); server.stderr.on("data", chunk => { logs += chunk; });
    await until(async () => { try { return (await fetch(url + "/api/health")).ok; } catch { return false; } });
    const password = "blog browser fixture password";
    const setup = await fetch(url + "/api/setup", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
      setup_token: await readFile(path.join(state, "setup-token"), "utf8"), email: "owner@example.org", name: "Owner", password,
      organization_name: "Research", transport: "bearer",
    }) });
    assert.equal(setup.status, 201); const session = await setup.json();
    const call = async (endpoint, body, method = "POST") => {
      const reply = await fetch(url + "/api" + endpoint, { method,
        headers: { "Content-Type": "application/json", Authorization: "Bearer " + session.access_token },
        body: body === undefined ? undefined : JSON.stringify(body) });
      assert.ok(reply.ok, `Fixture HTTP ${reply.status}: ${await reply.clone().text()}`); return reply.json();
    };
    const root = "/organizations/" + session.organization_id;
    const project = await call(root + "/projects", { name: "Geometry" });
    const body = "Turns out, $\\int_M d\\alpha=\\int_{\\partial M}\\alpha$.\n\n" +
      "$$\\frac{1}{2} + \\sqrt{x}$$\n\n" + "Bracket form \\(a+b=c\\).";
    const post = await call(root + "/knowledge", { client_id: "blog-browser-published-001", kind: "journal", title: "Stokes result", body,
      state: "published", scope_kind: "project", scope_id: project.id });
    const draft = await call(root + "/knowledge", { client_id: "blog-browser-draft-001", kind: "journal", title: "Unpublished scratch", body: "Draft text",
      state: "draft", scope_kind: "project", scope_id: project.id });
    browser = await chromium.launch({ headless: true }); const page = await browser.newPage();
    page.on("pageerror", error => errors.push(error.message));
    page.on("response", response => { if (response.url().includes("/assets/") && !response.ok()) failedAssets.push(response.url()); });
    await page.goto(url + "/organization.html");
    await page.getByLabel("Email", { exact: true }).fill("owner@example.org");
    await page.getByLabel("Password", { exact: true }).fill(password);
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await page.getByLabel("Email", { exact: true }).waitFor({ state: "detached" });
    for (const viewport of [{ width: 1280, height: 900 }, { width: 390, height: 844 }]) {
      await page.setViewportSize(viewport);
      const reading = await page.goto(url + `/blog/${session.organization_id}/${post.id}`);
      assert.equal(reading.status(), 200);
      await checkMath(page, ".blog-article");
      assert.ok(await page.getByRole("link", { name: "Edit posts", exact: true }).isVisible());
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
      await page.getByRole("link", { name: "Edit posts", exact: true }).click();
      await page.getByRole("button", { name: "Edit draft", exact: true }).first().click();
      await page.getByRole("button", { name: "Preview", exact: true }).click();
      await checkMath(page, ".org-blog-preview");
      await page.getByRole("button", { name: "Close editor", exact: true }).click();
    }
    await page.getByRole("button", { name: "Drafts", exact: true }).click();
    await page.getByText("Unpublished scratch", { exact: true }).waitFor();
    page.once("dialog", dialog => dialog.accept());
    await page.getByRole("button", { name: "Delete post", exact: true }).click();
    await page.getByText("No drafts posts.", { exact: true }).waitFor();
    await page.getByRole("button", { name: "Published", exact: true }).click();
    await page.getByText("Stokes result", { exact: true }).waitFor();
    page.once("dialog", dialog => dialog.accept());
    await page.getByRole("button", { name: "Delete post", exact: true }).click();
    await page.getByText("No published posts.", { exact: true }).waitFor();
    for (const entry of [post, draft]) {
      const reply = await fetch(url + `/api${root}/knowledge/${entry.id}?revision=1`, { headers: { Authorization: "Bearer " + session.access_token } });
      assert.equal(reply.status, 404);
    }
    assert.deepEqual((await call(root + "/blog", undefined, "GET")).posts, []);
    // The empty index renders no math: it loads only the theme script, which still applies an explicit color mode.
    await page.evaluate(() => localStorage.setItem("reasoning.color-mode", "dark"));
    const scripts = [];
    page.on("request", request => { if (request.resourceType() === "script") scripts.push(new URL(request.url()).pathname); });
    await page.goto(url + `/blog/${session.organization_id}`);
    await page.getByText("No published posts.", { exact: true }).waitFor();
    await page.waitForFunction(() => document.documentElement.dataset.theme === "dark");
    assert.ok(scripts.some(path => /\/assets\/blogTheme-/.test(path)), scripts.join(", "));
    assert.deepEqual(scripts.filter(path => /blogMathBootstrap|katex|RichTextPolicy|styles-/.test(path)), []);
    assert.equal(await page.locator("link[rel=stylesheet][href*='katex']").count(), 0);
    assert.deepEqual(errors, []); assert.deepEqual(failedAssets, []);
  } catch (error) { error.message += "\nLocal fixture logs:\n" + logs; throw error; }
  finally {
    await browser?.close();
    if (server && server.exitCode === null && server.signalCode === null) {
      server.kill("SIGTERM"); await Promise.race([once(server, "exit"), new Promise(resolve => setTimeout(resolve, 5000))]);
      if (server.exitCode === null && server.signalCode === null) { server.kill("SIGKILL"); await once(server, "exit"); }
    }
    await rm(temporary, { recursive: true, force: true });
  }
});
