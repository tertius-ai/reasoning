import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { once } from "node:events";
import { mkdtemp, readFile, realpath, rm, writeFile } from "node:fs/promises";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { chromium } from "playwright";

// This fixture exercises rendered controls and the real HTTP/store protocol.
// It sends synthetic worker output; it never executes or proves a CAS result.
const repository = fileURLToPath(new URL("../../", import.meta.url));
const password = "public compute fixture password";
const limits = { cpu: 1, memory_bytes: 1073741824, disk_bytes: 268435456, pids: 128, wall_time_seconds: 120, artifact_bytes: 1048576 };
const image = { id: "protocol-fixture", name: "Non-executing algebra fixture", reference: "registry.example.org/research/cpu@sha256:" + "a".repeat(64),
  languages: ["sage", "python"], source_revision: "b".repeat(40), capabilities_digest: "sha256:" + "c".repeat(64), sbom_digest: "sha256:" + "d".repeat(64),
  capabilities_path: "/etc/reasoning/capabilities.json", sbom_path: "/etc/reasoning/sbom.json", maximum_resources: limits };
async function until(predicate, description) {
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) { if (await predicate()) return; await new Promise((resolve) => setTimeout(resolve, 100)); }
  throw new Error("Timed out: " + description);
}

test("rendered compute policy, enrollment, shared replay/output, cancellation and restart", { timeout: 120000 }, async (context) => {
  const temporary = await realpath(await mkdtemp(path.join(os.tmpdir(), "reasoning-compute-browser-")));
  const state = path.join(temporary, "state"), catalog = path.join(temporary, "images.json");
  await writeFile(catalog, JSON.stringify([image]), { mode: 0o600 });
  const listener = net.createServer(); listener.listen(0, "127.0.0.1"); await once(listener, "listening");
  const port = listener.address().port; await new Promise((resolve) => listener.close(resolve));
  const url = `http://127.0.0.1:${port}`;
  let server, browser, logs = "";
  const errors = [];
  const start = async () => {
    server = spawn(path.join(repository, ".venv/bin/python"), ["-m", "reasoning_ledger.server.cli", "--data-dir", state,
      "serve", "--public-url", url, "--port", String(port), "--frontend-dir", path.join(repository, "web/dist"), "--compute-images", catalog], { cwd: repository });
    server.stdout.on("data", (chunk) => { logs += chunk; }); server.stderr.on("data", (chunk) => { logs += chunk; });
    await until(async () => { try { return (await fetch(url + "/api/health")).ok; } catch { return false; } }, "server readiness");
  };
  const stop = async () => {
    if (!server || server.exitCode !== null || server.signalCode !== null) return;
    server.kill("SIGTERM");
    await Promise.race([once(server, "exit"), new Promise((resolve) => setTimeout(resolve, 5000))]);
    if (server.exitCode === null && server.signalCode === null) { server.kill("SIGKILL"); await once(server, "exit"); }
  };
  try {
    await start();
    const response = await fetch(url + "/api/setup", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
      setup_token: await readFile(path.join(state, "setup-token"), "utf8"), email: "owner@example.org", name: "Owner", password, organization_name: "Research", transport: "bearer",
    }) });
    assert.equal(response.status, 201);
    const ownerSession = await response.json(), org = ownerSession.organization_id;
    const call = async (endpoint, body, method = "POST", token = ownerSession.access_token) => {
      const result = await fetch(url + "/api" + endpoint, { method, headers: { "Content-Type": "application/json", Authorization: "Bearer " + token },
        body: body === undefined ? undefined : JSON.stringify(body) });
      assert.ok(result.ok, `Protocol fixture request failed: ${result.status}`);
      return result.status === 204 ? undefined : result.json();
    };
    const root = "/organizations/" + org;
    const project = await call(root + "/projects", { name: "Algebra" });
    const channel = await call(root + "/channels", { name: "research", project_id: project.id });
    const topic = await call(root + "/topics", { name: "Shared computation", kind: "gamma", channel_id: channel.id });
    for (const role of ["member", "viewer"]) {
      const invitation = await call(root + "/invitations", { email: role + "@example.org", role });
      const accepted = await fetch(url + "/api/invitations/accept", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token: invitation.token, name: role, password }) });
      assert.equal(accepted.status, 200);
    }
    browser = await chromium.launch({ headless: true });
    const pages = [];
    for (const role of ["owner", "member", "viewer"]) {
      const page = await (await browser.newContext({ viewport: { width: 1200, height: 950 }, acceptDownloads: true })).newPage();
      page.on("pageerror", (error) => errors.push(error.message));
      await page.goto(url); await page.getByLabel("Email", { exact: true }).fill(role + "@example.org");
      await page.getByLabel("Password", { exact: true }).fill(password); await page.getByRole("button", { name: "Sign in", exact: true }).click();
      await page.getByRole("heading", { name: "γ-Shared computation" }).waitFor();
      pages.push(page);
    }
    const [owner, member, viewer] = pages;
    await member.evaluate(id => window.dispatchEvent(new CustomEvent("reasoning-topic-command", { detail: { topicId: id, command: "compute" } })), topic.id);
    assert.equal(await member.getByRole("button", { name: "Submit computation", exact: true }).isEnabled(), false);
    await owner.getByRole("button", { name: "Account menu", exact: true }).click();
    await owner.getByRole("button", { name: "Administration" }).click();
    const settings = owner.getByRole("dialog");
    await settings.getByRole("button", { name: "Compute", exact: true }).click();
    await settings.getByRole("heading", { name: "Organization compute policy" }).waitFor();
    await settings.locator("fieldset").filter({ hasText: "Allowed compute images" }).getByRole("checkbox").check();
    await settings.getByLabel("Monthly CPU-time quota (seconds)", { exact: true }).fill("10000");
    await settings.getByLabel("Maximum concurrent jobs").fill("128");
    await settings.getByLabel("Pending-job limit (blank: unlimited)", { exact: true }).fill("1000");
    await settings.getByLabel("Requests per user / 15 minutes (blank: unlimited)", { exact: true }).fill("200");
    await settings.getByRole("button", { name: "Save compute policy" }).click();
    await until(async () => (await call(root + "/compute-configuration", undefined, "GET")).policy.monthly_cpu_seconds === 10000, "saved policy");
    const configuredPolicy = (await call(root + "/compute-configuration", undefined, "GET")).policy;
    assert.equal(configuredPolicy.max_active_jobs, 128);
    assert.equal(configuredPolicy.max_pending_jobs, 1000);
    assert.equal(configuredPolicy.requests_per_user_15_minutes, 200);
    await settings.getByLabel("Maximum concurrent jobs").fill("2");
    await settings.getByLabel("Pending-job limit (blank: unlimited)", { exact: true }).fill("");
    await settings.getByLabel("Requests per user / 15 minutes (blank: unlimited)", { exact: true }).fill("");
    await settings.getByRole("button", { name: "Save compute policy" }).click();
    await until(async () => (await call(root + "/compute-configuration", undefined, "GET")).policy.max_active_jobs === 2, "unlimited queue policy");
    assert.equal((await call(root + "/compute-configuration", undefined, "GET")).policy.max_pending_jobs, undefined);
    const automation = settings.getByRole("region", { name: "Scoped compute automation", exact: true });
    await automation.getByLabel("Scaler name", { exact: true }).fill("Burst compute controller");
    await automation.getByRole("group", { name: "Scaler-approved images", exact: true }).getByRole("checkbox").check();
    await automation.getByRole("button", { name: "Create scaler credential", exact: true }).click();
    const scalerToken = await automation.getByLabel("One-time scaler credential", { exact: true }).inputValue();
    assert.match(scalerToken, /^rns_[A-Za-z0-9_-]{43}$/);
    assert.equal(await owner.evaluate((token) => JSON.stringify({ ...localStorage, ...sessionStorage }).includes(token), scalerToken), false);
    const grants = await call(root + "/compute-scalers", undefined, "GET");
    assert.equal(grants.length, 1); assert.equal(grants[0].maximum_workers, 2); assert.equal(grants[0].token, undefined);
    const scalerIdentity = await call("/compute-scaler/identity", undefined, "GET", scalerToken);
    assert.equal(scalerIdentity.id, grants[0].id); assert.deepEqual(scalerIdentity.scopes, ["compute:snapshot", "workers:enroll"]);
    owner.once("dialog", (dialog) => dialog.accept()); await automation.getByRole("button", { name: "Dismiss scaler credential", exact: true }).click();
    owner.once("dialog", (dialog) => dialog.accept()); await automation.getByRole("button", { name: "Rotate scaler credential", exact: true }).click();
    await until(async () => (await automation.getByLabel("One-time scaler credential", { exact: true }).inputValue()) !== scalerToken, "rotated scaler token");
    const rotatedScalerToken = await automation.getByLabel("One-time scaler credential", { exact: true }).inputValue();
    assert.equal((await fetch(url + "/api/compute-scaler/identity", { headers: { Authorization: "Bearer " + scalerToken } })).status, 401);
    assert.equal((await call("/compute-scaler/identity", undefined, "GET", rotatedScalerToken)).id, grants[0].id);
    owner.once("dialog", (dialog) => dialog.accept()); await automation.getByRole("button", { name: "Dismiss scaler credential", exact: true }).click();
    owner.once("dialog", (dialog) => dialog.accept()); await automation.getByRole("button", { name: "Revoke scaler", exact: true }).click();
    await until(async () => (await call(root + "/compute-scalers", undefined, "GET"))[0].revoked_at !== null, "scaler revocation");
    await settings.getByLabel("Worker name", { exact: true }).fill("Separate fixture worker");
    await settings.locator("fieldset").filter({ hasText: "Worker images" }).getByRole("checkbox").check();
    await settings.getByRole("button", { name: "Register worker credential" }).click();
    await settings.getByRole("button", { name: "Show enrollment JSON" }).click();
    const enrollment = JSON.parse(await settings.getByLabel("Private worker enrollment JSON", { exact: true }).inputValue());
    assert.equal(enrollment.organization_id, org); assert.ok(enrollment.token);
    assert.equal(await owner.evaluate((token) => JSON.stringify({ ...localStorage, ...sessionStorage }).includes(token), enrollment.token), false);
    owner.once("dialog", (dialog) => dialog.accept()); await settings.getByRole("button", { name: "Dismiss enrollment", exact: true }).click();
    await settings.getByRole("button", { name: "Close dialog" }).click();

    assert.equal(await viewer.getByRole("button", { name: "Submit computation", exact: true }).count(), 0);
    await until(async () => await member.getByLabel("Compute image", { exact: true }).locator("option").count() === 2, "image policy refresh");
    await member.getByLabel("Compute image", { exact: true }).selectOption(image.id);
    await member.getByLabel("Program language", { exact: true }).selectOption("sage");
    await member.getByLabel("Compute program", { exact: true }).fill("# Protocol fixture only; no execution\nprint(2 + 2)");
    const resourceDetails = member.locator("details").filter({ has: member.locator("summary", { hasText: "Resource limits and quota" }) });
    assert.equal(await resourceDetails.evaluate(element => element.open), false);
    assert.equal(await member.getByLabel("Wall time (seconds)", { exact: true }).isVisible(), false);
    await resourceDetails.locator(":scope > summary").click();
    await member.getByLabel("Wall time (seconds)", { exact: true }).fill("0");
    await resourceDetails.locator(":scope > summary").click();
    await member.getByRole("button", { name: "Submit computation", exact: true }).click();
    assert.equal(await resourceDetails.evaluate(element => element.open), true);
    await member.getByLabel("Wall time (seconds)", { exact: true }).fill("60");
    await resourceDetails.locator(":scope > summary").click();
    const requests = []; let hideHistory = true;
    await member.route(`**/api${root}/topics/${topic.id}/compute-jobs*`, async (route) => {
      if (route.request().method() === "GET" && hideHistory) { await route.fulfill({ json: [] }); return; }
      if (route.request().method() === "POST") {
        requests.push(route.request().postDataJSON());
        if (requests.length === 1) { await route.fetch(); await route.abort("failed"); return; }
        hideHistory = false;
      }
      await route.continue();
    });
    await member.getByRole("button", { name: "Submit computation", exact: true }).click();
    await member.getByRole("button", { name: "Retry unchanged computation", exact: true }).waitFor();
    await member.getByRole("button", { name: "Close containerized computation", exact: true }).click();
    // Topic remount must retain the exact pending request, not re-enable editing.
    const alpha = await call(root + "/topics", { name: "Human discussion", kind: "alpha", channel_id: channel.id });
    await member.getByRole("navigation", { name: "Projects and channels" }).getByRole("button", { name: /Human discussion/ }).click();
    await member.getByRole("navigation", { name: "Projects and channels" }).getByRole("button", { name: /Shared computation/ }).click();
    await until(async () => await member.getByRole("button", { name: "Retry unchanged computation", exact: true }).isEnabled(), "retry readiness");
    assert.equal(await member.getByLabel("Compute program", { exact: true }).isEnabled(), false);
    await member.getByRole("button", { name: "Retry unchanged computation", exact: true }).click();
    await member.locator(".org-run-message").first().waitFor();
    assert.equal(requests.length, 2); assert.deepEqual(requests[0], requests[1]);
    const jobs = await call(root + "/topics/" + topic.id + "/compute-jobs", undefined, "GET");
    assert.equal(jobs.length, 1); const job = jobs[0];
    assert.equal(job.parameters.resources.wall_time_seconds, 60);
    const memberCard = member.locator('[data-activity-id="compute:' + job.id + '"] .org-run-message');
    const details = memberCard.getByText("Computation details", { exact: true }).locator("..");
    await details.waitFor({ state: "visible" });
    assert.equal(await details.evaluate(element => element.open), false);
    assert.equal(await memberCard.getByText("Resource limits", { exact: true }).isVisible(), false);
    assert.equal(await memberCard.getByRole("heading", { name: "Files", exact: true }).count(), 0);
    assert.equal(await memberCard.getByText(/Exit code pending|Elapsed pending|No committed artifacts/).count(), 0);
    const claimed = await call("/worker/claim", {}, "POST", enrollment.token);
    assert.equal(claimed.id, job.id); const lease = { attempt_id: claimed.attempt_id, lease: claimed.lease };
    await memberCard.getByRole("status").filter({ hasText: "Running" }).waitFor();
    assert.equal(await memberCard.getByRole("button", { name: "Stop this computation", exact: true }).isVisible(), true);
    const workerPath = "/worker/jobs/" + job.id;
    await call(workerPath + "/logs", { ...lease, chunk_index: 0, text: "Protocol output only. <script>not HTML</script>\n" }, "POST", enrollment.token);
    const payload = Buffer.from("Untrusted protocol artifact, not a CAS result.");
    const artifact = await call(workerPath + "/artifacts", { ...lease, name: "fixture.txt", size_bytes: payload.length, digest: "sha256:" + createHash("sha256").update(payload).digest("hex") }, "POST", enrollment.token);
    const artifactPath = workerPath + "/artifacts/" + artifact.id;
    await call(artifactPath + "/chunks", { ...lease, chunk_index: 0, content_base64: payload.toString("base64") }, "PUT", enrollment.token);
    await call(artifactPath + "/commit", lease, "POST", enrollment.token);
    await call(workerPath + "/finish", { ...lease, status: "succeeded", elapsed_ms: 1000, exit_code: 0 }, "POST", enrollment.token);
    await until(async () => (await member.getByLabel("Compute logs", { exact: true }).textContent()).includes("<script>not HTML</script>"), "plain output");
    assert.equal(await member.locator(".org-compute-detail script").count(), 0);
    await details.locator(":scope > summary").click();
    assert.equal(await details.getByText(job.id, { exact: true }).isVisible(), true);
    await details.locator(":scope > summary").click();
    {
      const card = viewer.locator('[data-activity-id="compute:' + job.id + '"] .org-run-message');
      await card.waitFor(); if (!await card.evaluate(element => element.open)) await card.locator(":scope > summary").click();
    }
    await viewer.getByRole("link", { name: "fixture.txt", exact: true }).waitFor();
    const downloadPromise = viewer.waitForEvent("download"); await viewer.getByRole("link", { name: "fixture.txt", exact: true }).click();
    const downloaded = await downloadPromise; assert.equal(downloaded.suggestedFilename(), "fixture.txt");
    assert.deepEqual(await readFile(await downloaded.path()), payload);
    // A deliberately new submission uses a fresh ID; queued cancellation settles without worker execution.
    await member.evaluate(id => window.dispatchEvent(new CustomEvent("reasoning-topic-command", { detail: { topicId: id, command: "compute" } })), topic.id);
    await member.getByRole("button", { name: "Submit computation", exact: true }).click();
    await until(async () => (await call(root + "/topics/" + topic.id + "/compute-jobs", undefined, "GET")).length === 2, "new explicit request");
    const stopComputation = member.getByRole("button", { name: "Stop this computation", exact: true });
    await until(() => stopComputation.isEnabled(), "the queued computation's card stop is enabled");
    await stopComputation.click();
    await until(async () => (await call(root + "/topics/" + topic.id + "/compute-jobs", undefined, "GET"))[0].status === "cancelled", "queued cancellation");
    // Failed setup can charge the full reservation; it is not measured wall time.
    const failed = await call(root + "/topics/" + topic.id + "/compute-jobs", { ...requests[0], client_id: "failed-setup-fixture" });
    const failedClaim = await call("/worker/claim", {}, "POST", enrollment.token);
    assert.equal(failedClaim.id, failed.id);
    const failedReceipt = await call("/worker/jobs/" + failed.id + "/finish", { attempt_id: failedClaim.attempt_id, lease: failedClaim.lease,
      status: "failed", elapsed_ms: 60000, exit_code: null, error_code: "execution_failed" }, "POST", enrollment.token);
    const failedCard = member.locator('[data-activity-id="compute:' + failed.id + '"] .org-run-message');
    await failedCard.getByRole("status").filter({ hasText: "Computation failed" }).waitFor();
    const elapsed = Math.max(0, failedReceipt.finished_at - failedReceipt.started_at);
    assert.equal((await failedCard.getByRole("status").textContent()).trim(), `Computation failed · ${Math.floor(elapsed / 60)}m ${elapsed % 60}s elapsed`);
    assert.equal(await failedCard.getByLabel("Compute logs", { exact: true }).textContent(), "No output recorded.");
    await stop(); await start(); await viewer.reload();
    await viewer.getByRole("heading", { name: "γ-Shared computation" }).waitFor();
    {
      const card = viewer.locator('[data-activity-id="compute:' + job.id + '"] .org-run-message');
      await card.waitFor(); if (!await card.evaluate(element => element.open)) await card.locator(":scope > summary").click();
    }
    await viewer.getByRole("link", { name: "fixture.txt", exact: true }).waitFor();
    await owner.getByRole("button", { name: "Account menu", exact: true }).click();
    await owner.getByRole("button", { name: "Administration" }).click(); await settings.getByRole("button", { name: "Compute", exact: true }).click();
    await settings.getByRole("button", { name: "Drain worker", exact: true }).click();
    await settings.getByRole("button", { name: "Resume claims", exact: true }).waitFor();
    owner.once("dialog", (dialog) => dialog.accept()); await settings.getByRole("button", { name: "Revoke worker", exact: true }).click();
    await until(async () => (await call(root + "/compute-workers", undefined, "GET"))[0].revoked_at !== null, "worker revocation");
    const refused = await fetch(url + "/api/worker/claim", { method: "POST", headers: { "Content-Type": "application/json", Authorization: "Bearer " + enrollment.token }, body: "{}" });
    assert.equal(refused.status, 401); assert.deepEqual(errors, []); assert.ok(alpha.id);
    context.diagnostic("Real rendered HTTP/store fixture: no Docker execution, paid compute, TLS, or cloud autoscaling is verified.");
  } catch (error) { error.message += "\nServer diagnostics:\n" + logs; throw error; }
  finally { await browser?.close(); await stop(); await rm(temporary, { recursive: true, force: true }); }
});
