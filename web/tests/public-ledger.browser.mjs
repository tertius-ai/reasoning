// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { mkdtemp, readFile, rm } from "node:fs/promises";
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
  throw new Error("Timed out waiting for the local ledger fixture");
}

test("ledger graph expands exact statements without overlap, and humans check inspected claim and proof revisions", { timeout: 60000 }, async () => {
  const temporary = await mkdtemp(path.join(os.tmpdir(), "reasoning-ledger-browser-"));
  const socket = net.createServer().listen(0, "127.0.0.1"); await once(socket, "listening");
  const port = socket.address().port; await new Promise(resolve => socket.close(resolve));
  const url = `http://127.0.0.1:${port}`, state = path.join(temporary, "state");
  let server, browser, page, logs = "";
  try {
    server = spawn(path.join(repository, ".venv/bin/python"), ["-m", "reasoning_ledger.server.cli", "--data-dir", state,
      "serve", "--public-url", url, "--port", String(port), "--frontend-dir", path.join(repository, "web/dist")],
      { cwd: repository, env: { ...process.env, PYTHONPATH: path.join(repository, "src") } });
    server.stdout.on("data", chunk => { logs += chunk; }); server.stderr.on("data", chunk => { logs += chunk; });
    await until(async () => { try { return (await fetch(url + "/api/health")).ok; } catch { return false; } });
    const password = "local ledger browser fixture password";
    const setup = await fetch(url + "/api/setup", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
      setup_token: await readFile(path.join(state, "setup-token"), "utf8"), email: "owner@example.org", name: "Owner", password,
      organization_name: "Ledger research", transport: "bearer",
    }) });
    assert.equal(setup.status, 201); const session = await setup.json();
    const call = async (endpoint, body, method = "POST") => {
      const reply = await fetch(url + "/api" + endpoint, { method,
        headers: { "Content-Type": "application/json", Authorization: "Bearer " + session.access_token },
        body: body === undefined ? undefined : JSON.stringify(body) });
      assert.ok(reply.ok, `Fixture HTTP ${reply.status}: ${await reply.clone().text()}`); return reply.status === 204 ? null : reply.json();
    };
    const root = "/organizations/" + session.organization_id;
    const project = await call(root + "/projects", { name: "Exact statements" });
    const statement = "For every integer $n$, $n=n$. " + "The quantified variable ranges over all integers; this is an exact displayed hypothesis. ".repeat(16) + "FULL_STATEMENT_END";
    const first = await call(root + "/mathematics", { name: "ledger_claim", project_id: project.id, client_id: "long-graph-claim", arguments: { statement } });
    const second = await call(root + "/mathematics", { name: "ledger_claim", project_id: project.id, client_id: "second-graph-claim", arguments: { statement: "A distinct statement below the expanded node." } });
    const proof = await call(root + "/mathematics", { name: "ledger_prove", client_id: "graph-claim-proof", arguments: {
      claim_id: first.claim_id, argument: "Reflexivity of equality gives n=n for every integer n.", expected_revision_id: first.revision_id,
    } });
    const graph = await call(root + "/mathematics/graph?project_id=" + project.id, undefined, "GET");
    assert.deepEqual(graph.nodes.map(node => node.claim_id).sort(), [first.claim_id, second.claim_id].sort());
    browser = await chromium.launch({ headless: true }); page = await browser.newPage(); const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.goto(url + "/organization.html");
    await page.getByLabel("Email", { exact: true }).fill("owner@example.org");
    await page.getByLabel("Password", { exact: true }).fill(password);
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await page.getByRole("navigation", { name: "Projects and channels" }).waitFor();
    const reads = [];
    page.on("request", request => { if (request.method() === "GET") { const target = new URL(request.url()); reads.push(target.pathname + target.search); } });
    const opened = Date.now();
    await page.goto(url + "/organization.html?organization=" + session.organization_id + "&view=ledger&project=" + project.id);
    const node = page.locator(`[data-claim-id="${first.claim_id}"]`), below = page.locator(`[data-claim-id="${second.claim_id}"]`);
    await node.waitFor(); const closed = await node.boundingBox();
    await page.waitForTimeout(Math.max(0, opened + 3000 - Date.now()));
    // The statement list arriving does not restart the graph; it refreshes on its own five-second cycle.
    assert.equal(reads.filter(path => path.includes("/mathematics/graph")).length, 1, "opening the ledger reads its graph once");
    await node.locator("annotation[encoding='application/x-tex']").first().waitFor({ state: "attached" });
    assert.deepEqual(await node.locator("annotation[encoding='application/x-tex']").allTextContents(), ["n", "n=n"]);
    assert.equal(await node.locator(".katex-mathml").first().evaluate(element => getComputedStyle(element).position), "absolute");
    const expand = node.getByRole("button", { name: `Expand statement ${first.claim_id}` });
    await expand.focus(); await page.keyboard.press("Enter");
    await node.getByRole("button", { name: `Collapse statement ${first.claim_id}` }).waitFor();
    assert.ok((await node.locator(".graph-node-statement").textContent()).endsWith("FULL_STATEMENT_END"));
    await node.locator("annotation[encoding='application/x-tex']").first().waitFor({ state: "attached" });
    assert.deepEqual(await node.locator("annotation[encoding='application/x-tex']").allTextContents(), ["n", "n=n"]);
    await until(async () => {
      const expanded = await node.boundingBox(), next = await below.boundingBox();
      return expanded.height > closed.height * 3 && (next.y >= expanded.y + expanded.height || expanded.y >= next.y + next.height);
    });
    assert.equal(await node.locator(".graph-node-statement").evaluate(element => getComputedStyle(element).webkitLineClamp), "none");
    await node.getByRole("button", { name: `Inspect proof ${first.claim_id}` }).click();
    const checks = page.getByRole("region", { name: "Human checks" });
    await until(async () => await checks.count() === 2);
    assert.equal(reads.filter(path => path.endsWith(`/mathematics/${first.claim_id}?history=true`)).length, 1,
      "the inspector reuses the record that opened it");
    await checks.first().getByLabel("Human check note").fill("Checked the full statement and its quantifiers.");
    await checks.first().getByRole("button", { name: "Checked by a human", exact: true }).click();
    await checks.first().getByText("✓ Checked by Owner", { exact: true }).waitFor();
    await checks.nth(1).getByRole("button", { name: "Checked by a human", exact: true }).click();
    await checks.nth(1).getByText("✓ Checked by Owner", { exact: true }).waitFor();
    const claimChecks = await call(root + "/mathematics/" + first.claim_id + "/human-checks", undefined, "GET");
    const proofChecks = await call(root + "/mathematics/" + proof.proof_id + "/human-checks", undefined, "GET");
    assert.equal(claimChecks[0].revision_id, first.revision_id); assert.equal(proofChecks[0].revision_id, proof.revision_id);
    assert.equal((await call(root + "/mathematics/" + first.claim_id, undefined, "GET")).status, "pending");
    await page.setViewportSize({ width: 390, height: 844 });
    await page.getByRole("button", { name: `Collapse statement ${first.claim_id}` }).focus(); await page.keyboard.press("Space");
    await node.getByRole("button", { name: `Expand statement ${first.claim_id}` }).waitFor();
    await page.setViewportSize({ width: 1000, height: 844 });
    await call(root + "/mathematics", { name: "ledger_reference", project_id: project.id,
      client_id: "browser-source-reference", arguments: { author: "A. Author", title: "Source for exact statements", notes: "**Primary source**" } });
    await page.goto(url + "/organization.html?organization=" + session.organization_id + "&view=bibliography");
    await page.getByText("A. Author · Source for exact statements", { exact: true }).click();
    await page.getByText("Attach a statement", { exact: true }).click();
    await page.getByLabel("Locator", { exact: true }).fill("Theorem 2.3, p. 17");
    await page.getByLabel("Statement", { exact: true }).fill("For every integer $n$, $n=n$.");
    await page.getByRole("button", { name: "Save and link to ledger" }).click();
    await page.locator(".org-source-statement").getByText("Theorem 2.3, p. 17", { exact: true }).waitFor();
    await page.locator(".org-source-statement").getByRole("link", { name: "Open in ledger" }).click();
    await page.getByRole("region", { name: "Human checks" }).waitFor();
    assert.ok(page.url().includes("claim=clm_"));
    await page.getByRole("link", { name: "Help", exact: true }).click();
    await page.getByRole("region", { name: "Reasoning help" }).waitFor();
    await page.getByRole("button", { name: "Back to chat", exact: true }).click();
    await page.getByRole("region", { name: "Human checks" }).waitFor();
    assert.deepEqual(errors, []);
  } catch (error) { error.message += "\nRendered fixture:\n" + await page?.locator("body").innerText() + "\nLocal fixture logs:\n" + logs; console.error(error.message); throw error; }
  finally {
    await browser?.close();
    if (server && server.exitCode === null && server.signalCode === null) {
      server.kill("SIGTERM"); await Promise.race([once(server, "exit"), new Promise(resolve => setTimeout(resolve, 5000))]);
      if (server.exitCode === null && server.signalCode === null) { server.kill("SIGKILL"); await once(server, "exit"); }
    }
    await rm(temporary, { recursive: true, force: true });
  }
});
