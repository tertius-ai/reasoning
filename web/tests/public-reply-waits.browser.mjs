// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { mkdtemp, readFile, realpath, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { chromium } from "playwright";

// A reply that proposes a computation pauses as a live card, says when nothing can run it, and continues once rejected.
// The fixture worker never claims or executes anything.
const repository = fileURLToPath(new URL("../../", import.meta.url));
const password = "reply waits browser fixture password";
const limits = { cpu: 1, memory_bytes: 1073741824, disk_bytes: 268435456, pids: 128, wall_time_seconds: 120, artifact_bytes: 1048576 };
const image = { id: "latex-fixture", name: "Non-executing TeX fixture", reference: "registry.example.org/research/tex@sha256:" + "a".repeat(64),
  languages: ["python"], source_revision: "b".repeat(40), capabilities_digest: "sha256:" + "c".repeat(64), sbom_digest: "sha256:" + "d".repeat(64),
  capabilities_path: "/etc/reasoning/capabilities.json", sbom_path: "/etc/reasoning/sbom.json", maximum_resources: limits };
async function until(predicate, description) {
  const deadline = Date.now() + 25000;
  while (Date.now() < deadline) { if (await predicate()) return; await new Promise(resolve => setTimeout(resolve, 100)); }
  throw new Error("Timed out: " + description);
}

test("a paused reply shows its proposed computation, refuses approval without a worker and continues once rejected", { timeout: 90000 }, async () => {
  const temporary = await realpath(await mkdtemp(path.join(os.tmpdir(), "reasoning-reply-waits-browser-")));
  const state = path.join(temporary, "state"), catalog = path.join(temporary, "images.json"), calls = [], errors = [];
  await writeFile(catalog, JSON.stringify([image]), { mode: 0o600 });
  const socket = net.createServer().listen(0, "127.0.0.1"); await once(socket, "listening");
  const port = socket.address().port; await new Promise(resolve => socket.close(resolve));
  const url = `http://127.0.0.1:${port}`;
  const provider = createServer(async (request, response) => {
    if (request.url.split("?")[0] === "/api/v1/key") {
      response.writeHead(200, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ data: { usage_daily: 0, limit: 50, limit_remaining: 50, limit_reset: "daily" } })); return;
    }
    if (["/api/v1/models", "/api/v1/models/user"].includes(request.url.split("?")[0])) {
      response.writeHead(200, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ data: [{ id: "fixture/model", architecture: { input_modalities: ["text"], output_modalities: ["text"] },
        pricing: { prompt: "0.000001", completion: "0.000002" }, context_length: 200000,
        supported_parameters: ["max_completion_tokens", "tools"], top_provider: { max_completion_tokens: 8192 } }] })); return;
    }
    let raw = ""; for await (const chunk of request) raw += chunk;
    const body = JSON.parse(raw); calls.push(body);
    let delta, finish_reason;
    if (calls.length === 1) {
      const submit = { name: "compute_submit", arguments: { image_id: image.id, language: "python", program: "print('compile simple-groups.tex')",
        resources: { cpu: 1, memory_bytes: 1073741824, disk_bytes: 268435456, pids: 128, wall_time_seconds: 120, artifact_bytes: 1048576 } } };
      delta = { content: "I saved the source and will compile it in a sandbox.", tool_calls: [{ index: 0, id: "call-compute", type: "function",
        function: { name: "tool_call", arguments: JSON.stringify(submit) } }] };
      finish_reason = "tool_calls";
    } else if (body.messages.some(message => message.content?.includes("Delegated work has returned"))) {
      delta = { content: "The computation was rejected, so I couldn't compile the PDF. Run pdflatex on the saved source." };
      finish_reason = "stop";
    } else {
      delta = { content: "Waiting for the computation." }; finish_reason = "stop";
    }
    response.writeHead(200, { "Content-Type": "text/event-stream" });
    response.write("data: " + JSON.stringify({ id: "gen-waits-" + calls.length, choices: [{ index: 0, delta, finish_reason }] }) + "\n\n");
    response.write("data: " + JSON.stringify({ choices: [], usage: { cost: "0.000008", prompt_tokens: 20, completion_tokens: 10 } }) + "\n\n");
    response.end("data: [DONE]\n\n");
  });
  provider.listen(0, "127.0.0.1"); await once(provider, "listening");
  let server, browser, logs = "";
  try {
    server = spawn(path.join(repository, ".venv/bin/python"), ["-m", "reasoning_ledger.server.cli", "--data-dir", state,
      "serve", "--public-url", url, "--port", String(port), "--frontend-dir", path.join(repository, "web/dist"), "--compute-images", catalog,
      "--openrouter-url", `http://127.0.0.1:${provider.address().port}/api/v1`], { cwd: repository });
    server.stdout.on("data", chunk => { logs += chunk; }); server.stderr.on("data", chunk => { logs += chunk; });
    await until(async () => { try { return (await fetch(url + "/api/health")).ok; } catch { return false; } }, "server start");
    const setup = await fetch(url + "/api/setup", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
      setup_token: await readFile(path.join(state, "setup-token"), "utf8"), email: "owner@example.org", name: "Owner", password,
      organization_name: "Research", transport: "bearer",
    }) });
    assert.equal(setup.status, 201); const session = await setup.json();
    const call = async (endpoint, body, method = "POST") => {
      const reply = await fetch(url + "/api" + endpoint, { method,
        headers: { "Content-Type": "application/json", Authorization: "Bearer " + session.access_token },
        body: body === undefined ? undefined : JSON.stringify(body) });
      assert.ok(reply.ok, `Fixture HTTP ${reply.status}: ${await reply.clone().text()}`); return reply.status === 204 ? null : reply.json();
    };
    const root = "/organizations/" + session.organization_id;
    await call(root + "/provider-keys", { key: "sk-or-unused-waits-fixture", label: "Free fixture" });
    await call(root + "/policy", { allowed_models: ["fixture/model"] }, "PUT");
    await call(root + "/compute-policy", { allowed_images: [image.id], monthly_cpu_seconds: 10000, max_active_jobs: 2 }, "PUT");
    const worker = await call(root + "/compute-workers", { name: "Standby fixture", image_ids: [image.id], resources: limits });
    const project = await call(root + "/projects", { name: "Groups" });
    const channel = await call(root + "/channels", { name: "research", project_id: project.id });
    await call(root + "/topics", { name: "Simple groups", kind: "beta", channel_id: channel.id });

    browser = await chromium.launch({ headless: true }); const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    page.on("pageerror", error => { errors.push(error.message); console.error(error.message); });
    await page.goto(url + "/organization.html");
    await page.getByLabel("Email", { exact: true }).fill("owner@example.org");
    await page.getByLabel("Password", { exact: true }).fill(password);
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await page.getByRole("button", { name: /^beta topic -Simple groups/ }).click();
    await page.getByLabel("Message", { exact: true }).fill("Create a LaTeX file of this and compile it to PDF.");
    await page.getByRole("button", { name: /^(Send|Send message)$/, exact: true }).click();

    const card = page.locator("details.org-model-run[data-waiting]");
    await card.getByText("Waiting for approval", { exact: true }).waitFor();
    const waits = card.getByLabel("What this reply is waiting for", { exact: true });
    await waits.getByText("Approve this computation?", { exact: true }).waitFor();
    await waits.getByText("latex-fixture · python · 1 CPU · up to 2 min", { exact: true }).waitFor();
    await waits.getByRole("button", { name: "Approve and run", exact: true }).waitFor();
    await card.getByRole("button", { name: "Stop this reply", exact: true }).waitFor();
    if (process.env.REPLY_WAITS_SCREENSHOTS) await card.screenshot({ path: path.join(process.env.REPLY_WAITS_SCREENSHOTS, "runnable.png") });

    // Without a worker nothing can run it: approval is withdrawn and rejecting lets the reply continue.
    await call(root + "/compute-workers/" + worker.id, undefined, "DELETE");
    await waits.getByText(/can't run computations/).waitFor();
    assert.equal(await waits.getByRole("button", { name: "Approve and run", exact: true }).count(), 0);
    if (process.env.REPLY_WAITS_SCREENSHOTS) await card.screenshot({ path: path.join(process.env.REPLY_WAITS_SCREENSHOTS, "unavailable.png") });
    await waits.getByRole("button", { name: "Reject and continue", exact: true }).click();
    await page.locator("article.org-message").getByText(/couldn't compile the PDF/).waitFor();
    await until(async () => await card.count() === 0, "waiting card to close");
    assert.equal(calls.length, 2, "the rejection hands the reply back exactly once");
    assert.deepEqual(errors, []);
  } catch (error) {
    console.error(logs);
    throw error;
  } finally {
    await browser?.close();
    server?.kill();
    provider.close();
    await rm(temporary, { recursive: true, force: true });
  }
});
