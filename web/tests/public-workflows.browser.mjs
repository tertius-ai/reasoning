import assert from "node:assert/strict";
import { createHash } from "node:crypto";
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

// Real rendered HTTP/store/reconciler/inference-executor journey. The provider
// and worker send synthetic output; neither live model calls nor CAS run here.
const repository = fileURLToPath(new URL("../../", import.meta.url));
const password = "workflow browser fixture password", key = "sk-or-workflow-fixture-only";
const limits = { cpu: 1, memory_bytes: 1073741824, disk_bytes: 268435456, pids: 128, wall_time_seconds: 120, artifact_bytes: 1048576 };
const image = { id: "workflow-fixture", name: "Synthetic workflow fixture", reference: "registry.example.org/research/cpu@sha256:" + "a".repeat(64),
  languages: ["python"], source_revision: "b".repeat(40), capabilities_digest: "sha256:" + "c".repeat(64), sbom_digest: "sha256:" + "d".repeat(64),
  capabilities_path: "/etc/reasoning/capabilities.json", sbom_path: "/etc/reasoning/sbom.json", maximum_resources: limits };
const output = "Synthetic reasoning <script>not code</script> π";
async function until(predicate, description) {
  const deadline = Date.now() + 25000;
  while (Date.now() < deadline) { if (await predicate()) return; await new Promise((resolve) => setTimeout(resolve, 100)); }
  throw new Error("Timed out: " + description);
}

test("rendered gamma runs share progress, approvals, attempts/results, immutable lost replies, cancel and restart", { timeout: 180000 }, async (context) => {
  const temporary = await realpath(await mkdtemp(path.join(os.tmpdir(), "reasoning-workflow-browser-")));
  const state = path.join(temporary, "state"), catalog = path.join(temporary, "images.json"), calls = [], errors = [];
  await writeFile(catalog, JSON.stringify([image]), { mode: 0o600 });
  const listener = net.createServer(); listener.listen(0, "127.0.0.1"); await once(listener, "listening");
  const port = listener.address().port; await new Promise((resolve) => listener.close(resolve));
  const url = `http://127.0.0.1:${port}`;
  const provider = createServer(async (request, response) => {
    if (request.url.split("?")[0] === "/api/v1/models") {
      response.writeHead(200, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ data: [{ id: "fixture/model", architecture: { input_modalities: ["text"], output_modalities: ["text"] },
        pricing: { prompt: "0.000001", completion: "0.000002" }, context_length: 200000, supported_parameters: ["max_completion_tokens"], top_provider: { max_completion_tokens: 8192 } }] }));
      return;
    }
    assert.equal(request.url, "/api/v1/chat/completions"); assert.equal(request.headers.authorization, "Bearer " + key);
    let body = ""; for await (const chunk of request) body += chunk;
    const payload = JSON.parse(body); calls.push(payload);
    assert.equal(payload.max_completion_tokens, 128); assert.equal(payload.provider.allow_fallbacks, true);
    response.writeHead(200, { "Content-Type": "text/event-stream" });
    response.write("data: " + JSON.stringify({ id: "gen-workflow-fixture", choices: [{ index: 0, delta: { content: output } }] }) + "\n\n");
    response.write("data: " + JSON.stringify({ id: "gen-workflow-fixture", choices: [], usage: { cost: "0.000007", prompt_tokens: 20, completion_tokens: 5 } }) + "\n\n");
    response.end("data: [DONE]\n\n");
  });
  provider.listen(0, "127.0.0.1"); await once(provider, "listening");
  let server, browser, logs = "";
  const start = async () => {
    server = spawn(path.join(repository, ".venv/bin/python"), ["-m", "reasoning_ledger.server.cli", "--data-dir", state,
      "serve", "--public-url", url, "--port", String(port), "--frontend-dir", path.join(repository, "web/dist"), "--compute-images", catalog,
      "--openrouter-url", `http://127.0.0.1:${provider.address().port}/api/v1`], { cwd: repository });
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
    const setup = await fetch(url + "/api/setup", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
      setup_token: await readFile(path.join(state, "setup-token"), "utf8"), email: "owner@example.org", name: "Owner", password, organization_name: "Research", transport: "bearer",
    }) });
    assert.equal(setup.status, 201); const ownerSession = await setup.json(), org = ownerSession.organization_id, root = "/organizations/" + org;
    const call = async (endpoint, body, method = "POST", token = ownerSession.access_token) => {
      const result = await fetch(url + "/api" + endpoint, { method, headers: { "Content-Type": "application/json", Authorization: "Bearer " + token },
        body: body === undefined ? undefined : JSON.stringify(body) });
      assert.ok(result.ok, `Fixture ${method} ${endpoint}: ${result.status} ${await result.clone().text()}`);
      return result.status === 204 ? undefined : result.json();
    };
    const project = await call(root + "/projects", { name: "Workflow research" });
    const channel = await call(root + "/channels", { name: "research", project_id: project.id });
    const topic = await call(root + "/topics", { name: "Shared workflow", kind: "gamma", channel_id: channel.id });
    await call(root + "/topics", { name: "Human notes", kind: "alpha", channel_id: channel.id });
    await call(root + "/compute-policy", { allowed_images: [image.id], monthly_cpu_seconds: 10000, max_active_jobs: 2 }, "PUT");
    const worker = await call(root + "/compute-workers", { name: "Synthetic worker", image_ids: [image.id], resources: limits });
    await call(root + "/provider-keys", { provider: "openrouter", label: "Fixture", key });
    await call(root + "/policy", { allowed_models: ["fixture/model"], monthly_budget_microusd: 10000000 }, "PUT");
    for (const role of ["member", "viewer"]) {
      const invitation = await call(root + "/invitations", { email: role + "@example.org", role });
      const accepted = await fetch(url + "/api/invitations/accept", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token: invitation.token, name: role, password }) });
      assert.equal(accepted.status, 200);
    }
    browser = await chromium.launch({ headless: true });
    const pages = [];
    for (const role of ["owner", "member", "viewer"]) {
      const page = await (await browser.newContext({ viewport: { width: 1200, height: 950 }, acceptDownloads: true, reducedMotion: "reduce" })).newPage();
      page.on("pageerror", (error) => errors.push(error.message));
      await page.goto(url); await page.getByLabel("Email", { exact: true }).fill(role + "@example.org");
      await page.getByLabel("Password", { exact: true }).fill(password); await page.getByRole("button", { name: "Sign in", exact: true }).click();
      await page.getByRole("navigation", { name: "Projects and channels" }).getByRole("button", { name: /^gamma topic -Shared workflow\b/ }).click();
      await page.getByRole("heading", { name: "γ-Shared workflow" }).waitFor(); pages.push(page);
    }
    const [owner, member, viewer] = pages;
    await member.evaluate(id => window.dispatchEvent(new CustomEvent("reasoning-topic-command", { detail: { topicId: id, command: "workflow" } })), topic.id);
    const picker = member.getByRole("dialog", { name: "New workflow", exact: true });
    await picker.getByRole("button", { name: "Step 1 model", exact: true }).click();
    await picker.getByRole("radio", { name: /fixture\/model/ }).waitFor();
    const radio = await picker.getByRole("radio", { name: /fixture\/model/ }).boundingBox();
    const name = await picker.locator(".org-model-line strong").first().boundingBox();
    assert.ok(radio && name && Math.abs((radio.y + radio.height / 2) - (name.y + name.height / 2)) < 15, "inline gamma model radio");
    await picker.getByRole("button", { name: "Close new workflow", exact: true }).click();
    const definition = { format: 1, name: "Shared immutable workflow", nodes: [
      { id: "prepare", kind: "compute", depends_on: [], inputs: {}, parameters: { image_id: image.id, language: "python", program: "# Synthetic protocol fixture only\nprint(4)", resources: limits } },
      { id: "review", kind: "approval", depends_on: ["prepare"], inputs: { log: { node: "prepare", output: "logs" } }, parameters: { message: "Review <script>untrusted</script> output", required_role: "admin" } },
      { id: "reason", kind: "llm", depends_on: ["prepare", "review"], inputs: { log: { node: "prepare", output: "logs" }, decision: { node: "review", output: "decision" } },
        parameters: { model: "fixture/model", prompt: "Explain the synthetic result.", max_completion_tokens: 128 } },
      { id: "final", kind: "compute", depends_on: ["reason"], inputs: { answer: { node: "reason", output: "text" } }, parameters: { image_id: image.id, language: "python", program: "# Read inert /work/inputs.json\nprint(4)", resources: limits } },
    ] };
    const startRequests = [];
    await member.route(`**/api${root}/workflows/*/runs`, async (route) => {
      if (route.request().method() === "POST") {
        startRequests.push(route.request().postDataJSON());
        if (startRequests.length === 1) { await route.fetch(); await route.abort("failed"); return; }
      }
      await route.continue();
    });
    await member.evaluate(id => window.dispatchEvent(new CustomEvent("reasoning-topic-command", { detail: { topicId: id, command: "json" } })), topic.id);
    await member.getByRole("dialog", { name: "Workflow JSON", exact: true }).locator("textarea").fill(JSON.stringify(definition));
    await member.getByRole("button", { name: "Run workflow", exact: true }).click();
    await member.getByRole("button", { name: "Retry original start request", exact: true }).waitFor();
    assert.equal(await viewer.getByRole("button", { name: "Workflow", exact: true }).count(), 0);
    const saved = (await call(root + "/topics/" + topic.id + "/workflows", undefined, "GET"))[0];
    const navigate = async page => {
      const navigation = page.getByRole("navigation", { name: "Projects and channels" });
      await navigation.getByRole("button", { name: /^alpha topic -Human notes\b/ }).click();
      await navigation.getByRole("button", { name: /^gamma topic -Shared workflow\b/ }).click();
    };

    await member.getByRole("button", { name: "Retry original start request", exact: true }).waitFor();
    await until(() => member.getByRole("button", { name: "Retry original start request", exact: true }).isEnabled(), "lost start reply settled");
    await call(root + "/workflows/" + saved.id + "/versions", { client_id: "concurrent-version-browser-001", expected_version: 1, definition: { ...definition, name: "Concurrent version" } });
    await navigate(member);
    await member.getByRole("button", { name: "Retry original start request", exact: true }).click();
    await member.locator(".org-run-message").first().waitFor();
    assert.deepEqual(startRequests[0], startRequests[1]); assert.equal(startRequests[1].version, 1);
    let history = await call(root + "/topics/" + topic.id + "/workflow-runs", undefined, "GET");
    assert.equal(history.length, 1); const first = history[0], runPath = root + "/workflow-runs/" + first.id;
    const runView = () => call(runPath, undefined, "GET");
    const select = async (page, value) => {
      const card = page.locator('[data-activity-id="workflow:' + value.id + '"]');
      await card.waitFor();
      for (const other of await page.locator('[data-activity-id^="workflow:"] .org-run-message').all()) {
        if (await other.evaluate((element, id) => element.open && element.parentElement.dataset.activityId !== "workflow:" + id, value.id)) {
          await other.locator(":scope > summary").click();
        }
      }
      const detail = card.locator(".org-run-message");
      if (!await detail.evaluate(element => element.open)) await detail.locator(":scope > summary").click();
      await card.getByLabel("Workflow run status", { exact: true }).waitFor();
    };
    await select(owner, first); await select(viewer, first);
    let claimed;
    const claim = async () => {
      await until(async () => { claimed = await call("/worker/claim", { supports_workflow_inputs: true }, "POST", worker.token); return !!claimed; }, "workflow child claim");
      return claimed;
    };
    const prepare = await claim(); assert.deepEqual(prepare.parameters.inputs, {});
    const lease = { attempt_id: prepare.attempt_id, lease: prepare.lease }, workerPath = "/worker/jobs/" + prepare.id;
    await call(workerPath + "/logs", { ...lease, chunk_index: 0, text: "Synthetic output <script>not HTML</script>\n" }, "POST", worker.token);
    const payload = Buffer.from("Synthetic fixture artifact, not an algebra result.");
    const artifact = await call(workerPath + "/artifacts", { ...lease, name: "workflow-fixture.txt", size_bytes: payload.length, digest: "sha256:" + createHash("sha256").update(payload).digest("hex") }, "POST", worker.token);
    await call(workerPath + "/artifacts/" + artifact.id + "/chunks", { ...lease, chunk_index: 0, content_base64: payload.toString("base64") }, "PUT", worker.token);
    await call(workerPath + "/artifacts/" + artifact.id + "/commit", lease, "POST", worker.token);
    await call(workerPath + "/finish", { ...lease, status: "succeeded", elapsed_ms: 1000, exit_code: 0 }, "POST", worker.token);
    await until(async () => (await runView()).status === "awaiting_approval", "approval gate");
    await owner.getByRole("button", { name: "Reject review attempt 1", exact: true }).waitFor();
    assert.equal(await member.getByRole("button", { name: "Approve review attempt 1", exact: true }).count(), 0);
    assert.equal(await viewer.getByRole("button", { name: "Cancel workflow run", exact: true }).count(), 0);
    assert.equal(await viewer.getByRole("button", { name: "Approve review attempt 1", exact: true }).count(), 0);
    const decisions = [];
    await owner.route(`**/api${runPath}/nodes/review/approval`, async (route) => {
      if (route.request().method() === "POST") {
        decisions.push(route.request().postDataJSON());
        if (decisions.length === 1) { await route.fetch(); await route.fulfill({ status: 504, json: { detail: "Synthetic lost gateway reply" } }); return; }
      }
      await route.continue();
    });
    owner.once("dialog", (dialog) => dialog.accept()); await owner.getByRole("button", { name: "Reject review attempt 1", exact: true }).click();
    await owner.getByRole("button", { name: "Retry original approve request", exact: true }).waitFor();
    await until(() => owner.getByRole("button", { name: "Retry original approve request", exact: true }).isEnabled(), "lost rejection reply settled");
    await navigate(owner);
    await owner.getByRole("button", { name: "Retry original approve request", exact: true }).click();
    await until(async () => (await runView()).status === "failed", "rejection failure");
    assert.deepEqual(decisions[0], decisions[1]); assert.equal(decisions[1].decision, "reject");
    const retries = [];
    await owner.route(`**/api${runPath}/retry`, async (route) => {
      if (route.request().method() === "POST") {
        retries.push(route.request().postDataJSON()); if (retries.length === 1) { await route.fetch(); await route.abort("failed"); return; }
      }
      await route.continue();
    });
    await owner.locator(".org-run-message[data-status=failed]").waitFor();
    await until(() => owner.getByRole("button", { name: "Start or resume", exact: true }).isEnabled(), "cassette resume enabled after acknowledgement");
    owner.once("dialog", (dialog) => dialog.accept());
    await owner.getByRole("button", { name: "Start or resume", exact: true }).click();
    await owner.getByRole("button", { name: "Retry original retry request", exact: true }).waitFor();
    await until(() => owner.getByRole("button", { name: "Retry original retry request", exact: true }).isEnabled(), "lost retry reply settled");
    await navigate(owner); await owner.getByRole("button", { name: "Retry original retry request", exact: true }).click();
    await owner.getByRole("button", { name: "Approve review attempt 2", exact: true }).waitFor(); assert.deepEqual(retries[0], retries[1]);
    await viewer.getByRole("button", { name: "Inspect review attempts and results", exact: true }).click();
    await viewer.getByRole("button", { name: "Attempt 1 · failed", exact: true }).waitFor();
    await viewer.getByRole("button", { name: "Attempt 1 · failed", exact: true }).click();
    await viewer.getByRole("region", { name: "Workflow attempt inspection", exact: true }).getByText(/reject by/).waitFor();
    owner.once("dialog", (dialog) => dialog.accept()); await owner.getByRole("button", { name: "Approve review attempt 2", exact: true }).click();
    const final = await claim(); assert.deepEqual(final.parameters.inputs, { answer: output });
    assert.equal(final.parameters.program, definition.nodes[3].parameters.program); assert.equal(calls.length, 1);
    assert.ok(JSON.stringify(calls[0].messages).includes("Synthetic output")); assert.ok(JSON.stringify(calls[0].messages).includes("approved_by"));
    await call("/worker/jobs/" + final.id + "/finish", { attempt_id: final.attempt_id, lease: final.lease, status: "succeeded", elapsed_ms: 1000, exit_code: 0 }, "POST", worker.token);
    await until(async () => (await runView()).status === "succeeded", "completed workflow");
    await viewer.getByRole("button", { name: "Inspect reason attempts and results", exact: true }).click();
    await viewer.getByRole("button", { name: "View model result", exact: true }).click();
    await until(async () => (await viewer.getByLabel("Workflow model output", { exact: true }).textContent()) === output, "shared model result");
    assert.equal(await viewer.getByRole("region", { name: "Selected workflow run", exact: true }).locator("script").count(), 0);
    await viewer.getByRole("button", { name: "Inspect prepare attempts and results", exact: true }).click();
    await viewer.getByRole("button", { name: "View computation result", exact: true }).click();
    await viewer.getByRole("link", { name: "workflow-fixture.txt", exact: true }).waitFor();
    const downloaded = viewer.waitForEvent("download"); await viewer.getByRole("link", { name: "workflow-fixture.txt", exact: true }).click();
    assert.deepEqual(await readFile(await (await downloaded).path()), payload);
    assert.ok((await viewer.getByLabel("Compute logs", { exact: true }).textContent()).includes("<script>not HTML</script>"));
    await member.locator(".org-run-message[data-status=succeeded]").waitFor();
    await member.evaluate(id => window.dispatchEvent(new CustomEvent("reasoning-topic-command", { detail: { topicId: id, command: "uncap" } })), topic.id);
    const uncappedEditor = member.getByRole("dialog", { name: "Workflow JSON", exact: true });
    const uncapped = JSON.parse(await uncappedEditor.getByLabel("Definition JSON", { exact: true }).inputValue());
    assert.equal(uncapped.nodes.find(node => node.kind === "llm").parameters.max_completion_tokens, null);
    assert.equal((await runView()).definition.nodes.find(node => node.kind === "llm").parameters.max_completion_tokens, 128);
    await uncappedEditor.getByRole("button", { name: "Close workflow json", exact: true }).click();
    await member.evaluate(id => window.dispatchEvent(new CustomEvent("reasoning-topic-command", { detail: { topicId: id, command: "play" } })), topic.id);
    await until(async () => (await call(root + "/topics/" + topic.id + "/workflow-runs", undefined, "GET")).length === 2, "deliberate new run");
    history = await call(root + "/topics/" + topic.id + "/workflow-runs", undefined, "GET"); const second = history[0];
    const running = await claim(), cancellations = [];
    await member.route(`**/api${root}/workflow-runs/${second.id}/cancel`, async (route) => {
      if (route.request().method() === "POST") {
        cancellations.push(route.request().postDataJSON()); if (cancellations.length === 1) { await route.fetch(); await route.abort("failed"); return; }
      }
      await route.continue();
    });
    await select(member, second);
    await until(() => member.getByRole("button", { name: "Stop current work", exact: true }).isEnabled(), "cassette stop enabled for active workflow");
    await member.getByRole("button", { name: "Stop current work", exact: true }).click();
    await member.getByRole("button", { name: "Retry original cancel request", exact: true }).waitFor();
    await until(() => member.getByRole("button", { name: "Retry original cancel request", exact: true }).isEnabled(), "lost cancel reply settled");
    assert.equal((await call(root + "/workflow-runs/" + second.id, undefined, "GET")).cancel_requested, 1);
    assert.equal((await call(root + "/workflow-runs/" + second.id, undefined, "GET")).finished_at, null);
    await call("/worker/jobs/" + running.id + "/finish", { attempt_id: running.attempt_id, lease: running.lease, status: "cancelled", elapsed_ms: 1000, exit_code: null }, "POST", worker.token);
    await navigate(member); await member.getByRole("button", { name: "Retry original cancel request", exact: true }).click();
    await until(async () => (await call(root + "/workflow-runs/" + second.id, undefined, "GET")).status === "cancelled", "settled cancellation");
    assert.deepEqual(cancellations[0], cancellations[1]);
    await stop(); await start(); await viewer.reload();
    await viewer.getByRole("navigation", { name: "Projects and channels" }).getByRole("button", { name: /^gamma topic -Shared workflow\b/ }).click();
    await viewer.getByRole("heading", { name: "γ-Shared workflow" }).waitFor(); await select(viewer, first);
    await until(async () => (await viewer.locator('[data-activity-id="workflow:' + first.id + '"]').getByLabel("Workflow run status", { exact: true }).textContent()).startsWith("succeeded"), "durable run after restart");
    await viewer.getByRole("button", { name: "Inspect review attempts and results", exact: true }).click();
    await viewer.getByRole("button", { name: "Attempt 2 · succeeded", exact: true }).waitFor();
    assert.equal(calls.length, 1); assert.equal((await call(root + "/topics/" + topic.id + "/compute-jobs", undefined, "GET")).length, 3);

    // An accepted start with a lost reply remains bound to current authority.
    // Demotion cancels its unpaid approval-only run, not its retained request.
    const permissionDefinition = { format: 1, name: "Permission probe", nodes: [{ id: "permission", kind: "approval", depends_on: [], inputs: {},
      parameters: { message: "Check current role", required_role: "member" } }] };
    let permissionVersion;
    const roleRequests = [];
    await member.route(`**/api${root}/workflows/*/runs`, async (route) => {
      if (route.request().method() === "POST") {
        roleRequests.push(route.request().postDataJSON()); if (roleRequests.length === 1) { await route.fetch(); await route.abort("failed"); return; }
      }
      await route.continue();
    });
    await member.evaluate(id => window.dispatchEvent(new CustomEvent("reasoning-topic-command", { detail: { topicId: id, command: "json" } })), topic.id);
    await member.getByLabel("Definition JSON", { exact: true }).fill(JSON.stringify(permissionDefinition));
    await member.getByRole("button", { name: "Run workflow", exact: true }).click();
    await member.getByRole("button", { name: "Retry original start request", exact: true }).waitFor();
    await until(() => member.getByRole("button", { name: "Retry original start request", exact: true }).isEnabled(), "lost permission reply settled");
    const permissionSaved = (await call(root + "/topics/" + topic.id + "/workflows", undefined, "GET")).find(value => value.name === "Permission probe");
    permissionVersion = { workflow_id: permissionSaved.id };
    const memberRecord = (await call(root + "/members", undefined, "GET")).find((value) => value.email === "member@example.org");
    await call(root + "/members/" + memberRecord.id, { role: "viewer" }, "PATCH");
    await until(async () => !await member.getByRole("button", { name: "Retry original start request", exact: true }).isEnabled(), "demoted replay disabled");
    assert.equal(await member.getByRole("button", { name: "Workflow", exact: true }).count(), 0);
    await call(root + "/members/" + memberRecord.id, { role: "member" }, "PATCH");
    await until(async () => await member.getByRole("button", { name: "Retry original start request", exact: true }).isEnabled(), "restored current role");
    await member.getByRole("button", { name: "Retry original start request", exact: true }).click();
    assert.deepEqual(roleRequests[0], roleRequests[1]);
    await until(async () => !await member.getByRole("button", { name: "Retry original start request", exact: true }).count(), "recovered demoted request");
    const permissionRuns = (await call(root + "/topics/" + topic.id + "/workflow-runs", undefined, "GET")).filter((value) => value.workflow_id === permissionVersion.workflow_id);
    assert.equal(permissionRuns.length, 1); assert.equal(permissionRuns[0].status, "cancelled");

    // Build a second organization with private unpaid history, then attempt to
    // inspect its run ID through the viewer's original organization namespace.
    const privateOrganization = await call("/organizations", {
      name: "Private fixture organization",
      credential_mode: "none",
    });
    const privateRoot = "/organizations/" + privateOrganization.id;
    const privateProject = await call(privateRoot + "/projects", { name: "Private project" });
    const privateChannel = await call(privateRoot + "/channels", { name: "private", project_id: privateProject.id });
    const privateTopic = await call(privateRoot + "/topics", { name: "Private workflow", kind: "gamma", channel_id: privateChannel.id });
    const privateVersion = await call(privateRoot + "/topics/" + privateTopic.id + "/workflows", { client_id: "private-definition-browser-001", definition: { ...permissionDefinition, name: "PRIVATE DEFINITION MUST NOT LEAK" } });
    const privateRun = await call(privateRoot + "/workflows/" + privateVersion.workflow_id + "/runs", { client_id: "private-start-browser-001", version: 1 });
    const forbiddenRun = await viewer.evaluate(async path => (await fetch("/api" + path)).status, privateRoot + "/workflow-runs/" + privateRun.id);
    assert.equal(forbiddenRun, 404);
    assert.equal((await viewer.locator("body").textContent()).includes("PRIVATE DEFINITION MUST NOT LEAK"), false);

    // Exercise actual run pagination without starting compute or model work.
    for (let index = 0; index < 18; index++) {
      const value = await call(root + "/workflows/" + permissionVersion.workflow_id + "/runs", { client_id: "pagination-start-browser-" + String(index).padStart(3, "0"), version: 1 });
      await call(root + "/workflow-runs/" + value.id + "/cancel", { client_id: "pagination-cancel-browser-" + String(index).padStart(3, "0") });
    }
    await viewer.getByRole("button", { name: "Earlier workflow activity", exact: true }).click();
    await select(viewer, first);
    await until(async () => !await viewer.getByRole("button", { name: "Earlier workflow activity", exact: true }).count(), "exhausted history pages");
    const narrowRun = await call(root + "/workflows/" + permissionVersion.workflow_id + "/runs", { client_id: "narrow-approval-start-browser-001", version: 1 });
    await select(owner, { id: narrowRun.id, definition_version: 1 });
    await owner.getByRole("button", { name: "Approve permission attempt 1", exact: true }).waitFor();
    await owner.setViewportSize({ width: 390, height: 844 });
    assert.ok(await owner.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 2), "approval controls overflow narrow viewport");
    await viewer.setViewportSize({ width: 390, height: 844 });
    assert.ok(await viewer.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 2), "workflow controls overflow narrow viewport");
    // Retained records in deleted topics remain inspectable by administrators.
    // Seed an interrupted fake child while the real server is stopped; no provider call is replayed.
    await stop();
    const seed = spawn(path.join(repository, ".venv/bin/python"), ["-c", String.raw`
import json, os
from pathlib import Path
from reasoning_ledger.server.store import Store
from reasoning_ledger.server.workflow_store import WorkflowStore
from reasoning_ledger.server.workflow_runs import WorkflowRuns
from reasoning_ledger.server.compute_store import ComputeStore
from reasoning_ledger.server.inference_store import InferenceStore
from reasoning_ledger.server.openrouter import Quote
store = Store(Path(os.environ["CAPACITY_FIXTURE_STATE"]))
user, org, channel = (os.environ[key] for key in ("CAPACITY_FIXTURE_USER", "CAPACITY_FIXTURE_ORG", "CAPACITY_FIXTURE_CHANNEL"))
topic = store.create_entity(user, org, "topics", "Deleted uncertain topic", channel, "gamma")["id"]
definitions, inference = WorkflowStore(store), InferenceStore(store)
runs = WorkflowRuns(definitions, ComputeStore(store, {}), inference)
version = definitions.create(user, org, topic, "retained-capacity-definition", {"format": 1, "name": "Retained uncertain workflow", "nodes": [
    {"id": "check", "kind": "llm", "depends_on": [], "inputs": {}, "parameters": {"model": "fixture/model", "prompt": "Synthetic question", "max_completion_tokens": 128}}
]})
run = runs.start(user, org, version["workflow_id"], version["version"], "retained-capacity-start")
action = runs.advance(run["id"])
runs.advance(run["id"], quote_action=action, quotes=[Quote("fixture/model", "0.000001", "0.000002", "0", 200000, 8192)])
child = inference.claim()
inference.finish(child, "interrupted", error="worker_interrupted", accounting_unknown=True)
runs.advance(run["id"])
plan = store.deletion_plan(user, org, "topics", topic)
store.delete_entity(user, org, "topics", topic, plan["name"], plan["plan_token"], stop_active=True)
print(json.dumps({"id": run["id"]}))
`], { cwd: repository, env: { ...process.env, CAPACITY_FIXTURE_STATE: state, CAPACITY_FIXTURE_USER: ownerSession.user_id,
      CAPACITY_FIXTURE_ORG: org, CAPACITY_FIXTURE_CHANNEL: channel.id } });
    let seedOutput = "", seedError = ""; seed.stdout.on("data", data => { seedOutput += data; }); seed.stderr.on("data", data => { seedError += data; });
    const [seedCode] = await once(seed, "exit"); assert.equal(seedCode, 0, seedError); const retained = JSON.parse(seedOutput);
    await start(); await owner.reload();
    await owner.getByRole("button", { name: "Workflow activity", exact: true }).click();
    const capacity = owner.getByRole("dialog", { name: "Organization workflows", exact: true });
    await capacity.getByRole("button", { name: /^Inspect Retained uncertain workflow/ }).waitFor();
    await capacity.getByText(/of 64/).waitFor();
    assert.equal(await capacity.evaluate(element => element.scrollWidth <= element.clientWidth + 2), true, "workflow activity fits a phone");
    await capacity.getByRole("button", { name: /^Inspect Permission probe/ }).click();
    const completedInspection = owner.getByRole("dialog", { name: "Inspect workflow place", exact: true });
    await completedInspection.getByText(/Place occupied/).waitFor();
    await call(root + "/workflow-runs/" + narrowRun.id + "/cancel", { client_id: "capacity-completion-cancel" });
    await completedInspection.getByText("Finished; this run no longer uses a place.", { exact: true }).waitFor();
    await completedInspection.getByRole("button", { name: "Close inspect workflow place", exact: true }).click();
    const beforeCapacity = await call(root + "/workflow-capacity", undefined, "GET");
    assert.ok(beforeCapacity.runs.some(run => run.id === retained.id && run.topic_deleted));
    const forbiddenRetained = await viewer.evaluate(async path => (await fetch("/api" + path)).status,
      root + "/workflow-runs/" + retained.id + "/capacity-inspection");
    assert.equal(forbiddenRetained, 404);
    await capacity.getByRole("button", { name: /^Inspect Retained uncertain workflow/ }).click();
    const inspection = owner.getByRole("dialog", { name: "Inspect workflow place", exact: true });
    await inspection.getByText("Charge awaiting confirmation", { exact: false }).waitFor();
    await inspection.getByText(/This topic was deleted/).waitFor();
    assert.equal(await inspection.getByRole("link", { name: "Open workflow topic", exact: true }).count(), 0);
    const releases = [];
    await owner.route(`**/api${root}/workflow-runs/${retained.id}/release-capacity`, async route => {
      if (route.request().method() === "POST") {
        releases.push(route.request().postDataJSON());
        if (releases.length === 1) { await route.fetch(); await route.abort("failed"); return; }
      }
      await route.continue();
    });
    await inspection.getByRole("button", { name: "Release workflow slot", exact: true }).click();
    await inspection.getByRole("button", { name: "Confirm slot release", exact: true }).click();
    await inspection.getByRole("button", { name: "Retry same slot release", exact: true }).waitFor();
    await until(() => inspection.getByRole("button", { name: "Retry same slot release", exact: true }).isEnabled(), "lost release reply");
    await inspection.getByRole("button", { name: "Retry same slot release", exact: true }).click();
    await inspection.getByText("Place released; this run does not count toward the allowance.", { exact: true }).waitFor();
    assert.deepEqual(releases[0], releases[1]);
    const afterCapacity = await call(root + "/workflow-capacity", undefined, "GET");
    assert.equal(afterCapacity.in_use, beforeCapacity.in_use - 1);
    const retainedInspection = await call(root + "/workflow-runs/" + retained.id + "/capacity-inspection", undefined, "GET");
    assert.equal(retainedInspection.nodes[0].children[0].accounting_state, "uncertain");
    assert.equal(retainedInspection.run.status, "outcome_unknown");
    await inspection.getByRole("button", { name: "Close inspect workflow place", exact: true }).click();
    await capacity.locator("summary").filter({ hasText: "Released places" }).click();
    await capacity.getByRole("button", { name: /^Inspect Retained uncertain workflow/ }).waitFor();
    await capacity.getByRole("button", { name: "Close organization workflows", exact: true }).click();
    assert.equal(calls.length, 1, "inspection and release must not replay the uncertain provider call");
    for (const page of pages) assert.equal(await page.evaluate((secret) => JSON.stringify({ ...localStorage, ...sessionStorage }).includes(secret), key), false);
    assert.deepEqual(errors, []);
    context.diagnostic("Real shared workflow HTTP/UI with synthetic provider/worker outputs. No Docker, live model requests, TLS, cloud scaling, or source-built native client is established.");
  } catch (error) { error.message += "\nServer diagnostics:\n" + logs; throw error; }
  finally {
    await browser?.close(); await stop(); await new Promise((resolve) => provider.close(resolve)); await rm(temporary, { recursive: true, force: true });
  }
});
