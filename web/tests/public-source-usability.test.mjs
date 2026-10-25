// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { before, after, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";

let server, models, help, status, bibliography;
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL("../", import.meta.url)), appType: "custom", logLevel: "silent", server: { middlewareMode: true } });
  models = await server.ssrLoadModule("/src/ModelBrowser.tsx");
  help = await server.ssrLoadModule("/src/HelpPage.tsx");
  status = await server.ssrLoadModule("/src/ServerStatus.tsx");
  bibliography = await server.ssrLoadModule("/src/ResearchBibliography.tsx");
});
after(async () => { await server?.close(); });

test("favorites precede other sections without duplicating or whitelisting models", () => {
  const catalog = { models: [], recommendations: { model_ids: ["test/math"] } };
  assert.deepEqual(models.modelSections(["test/math", "test/other"], catalog, "", ["test/other"]), [
    { title: "Favorites", ids: ["test/other"] }, { title: "Best for math", ids: ["test/math"] },
  ]);
  assert.deepEqual(models.modelSections(["test/math", "test/other"], catalog, "math", ["test/other"]), [
    { title: "Best for math", ids: ["test/math"] },
  ]);
});

test("help covers real roles, scoped tools, approvals, test recipes and commands", () => {
  const html = renderToStaticMarkup(React.createElement(help.HelpPage, { onClose() {} }));
  for (const text of ["librarians", "formalizers", "verifiers", "make bot-doctor", "GitHub", "Lean", "CAS", "Shift+Enter", "/compute", "/goal"])
    assert.ok(html.includes(text), text);
});

test("help ends with a server status summary that is checking until the first response", () => {
  const html = renderToStaticMarkup(React.createElement(help.HelpPage, { onClose() {} }));
  const summary = html.slice(html.indexOf('aria-label="Server status"'));
  assert.ok(html.indexOf("Slash commands") < html.indexOf('aria-label="Server status"'), "status follows the help text");
  assert.ok(summary.includes("Checking…") && summary.includes("not yet") && summary.includes("disabled"));
});

const statusHtml = (check) => renderToStaticMarkup(React.createElement(status.ServerStatusView, {
  check: { error: "", checkedAt: new Date("2026-10-18T08:00:00Z"), checking: false, ...check }, onRefresh() {} }));

test("server status links only a verified commit and labels unknown identities explicitly", () => {
  const revision = "0123456789abcdef0123456789abcdef01234567", release = "f".repeat(64);
  const verified = statusHtml({ health: { status: "ok", initialized: true, mail_worker_running: true, release_id: release,
    source_revision: revision, source_modified: false, schema_version: 42, uptime_seconds: 3 * 3600 + 4 * 60 } });
  assert.ok(verified.includes(`href="${status.REPOSITORY_URL}/commit/${revision}"`));
  assert.ok(verified.includes("<code>0123456789ab</code>") && verified.includes(">ffffffffffff</code>"));
  for (const text of ["OK · initialized · mail worker running · schema 42 · up 3 h 4 min", 'dateTime="2026-10-18T08:00:00.000Z"', "Refresh"])
    assert.ok(verified.includes(text), text);
  assert.ok(!verified.includes("uncommitted"));
  assert.ok(statusHtml({ health: { status: "ok", initialized: true, release_id: release, source_revision: revision, source_modified: true } })
    .includes("with uncommitted changes"));
  const unverified = statusHtml({ health: { status: "ok", initialized: false, release_id: release, source_revision: null } });
  assert.ok(unverified.includes("unknown") && !unverified.includes("/commit/") && unverified.includes("awaiting setup"));
  const development = statusHtml({ health: { status: "ok", initialized: true, release_id: null, source_revision: null } });
  assert.ok(development.includes("development build") && !development.includes("/commit/") && !development.includes("release <code"));
  const forged = statusHtml({ health: { status: "ok", initialized: true, release_id: "../x", source_revision: "javascript:alert(1)" } });
  assert.ok(!forged.includes("javascript:") && !forged.includes("/commit/") && forged.includes("unknown"));
});

test("server status reports failures without stale values and never polls", async () => {
  const failed = statusHtml({ health: null, error: "server unreachable" });
  assert.ok(failed.includes("Unavailable: server unreachable") && failed.includes("unknown") && failed.includes("Refresh"));
  assert.equal(status.formatUptime(59), "under a minute");
  assert.equal(status.formatUptime(2 * 86400 + 5 * 3600), "2 d 5 h");
  const source = await readFile(new URL("../src/ServerStatus.tsx", import.meta.url), "utf8");
  assert.doesNotMatch(source, /setInterval|setTimeout/);
  const project = await readFile(new URL("../../pyproject.toml", import.meta.url), "utf8");
  assert.ok(project.includes(`Repository = "${status.REPOSITORY_URL}"`), "the commit link uses the project's repository URL");
});

test("a degraded server names its stopped workers, and disabled inference is not a stopped worker", () => {
  const degraded = statusHtml({ health: { status: "degraded", initialized: true, inference_enabled: true, inference_worker_running: true,
    workflow_worker_running: false, webhook_worker_running: true, mail_worker_running: true, schema_version: 62 } });
  assert.ok(degraded.includes('<span class="org-server-status-warning">Degraded</span> · initialized · workflow worker stopped · mail worker running · schema 62'),
    degraded);
  assert.ok(!degraded.includes("inference worker stopped") && !degraded.includes("webhook worker stopped"));
  const disabled = statusHtml({ health: { status: "ok", initialized: true, inference_enabled: false, inference_worker_running: false,
    workflow_worker_running: true, webhook_worker_running: true } });
  assert.ok(disabled.includes("OK · initialized · inference disabled") && !disabled.includes("worker stopped"), disabled);
  const healthy = statusHtml({ health: { status: "ok", initialized: true, inference_enabled: true, inference_worker_running: true,
    workflow_worker_running: true, webhook_worker_running: true } });
  assert.ok(healthy.includes("OK · initialized") && !healthy.includes("worker") && !healthy.includes("org-server-status-warning"));
});

test("the health check accepts the server's own degraded 503 report but not a proxy's error page", async () => {
  const original = globalThis.fetch;
  const report = { status: "degraded", initialized: true, webhook_worker_running: false };
  try {
    globalThis.fetch = async () => new Response(JSON.stringify(report), { status: 503, headers: { "Content-Type": "application/json" } });
    assert.deepEqual(await status.readHealth(), report);
    for (const [code, body, type] of [[503, "<h1>Service unavailable</h1>", "text/html"], [502, "<h1>Bad gateway</h1>", "text/html"],
      [503, JSON.stringify({ detail: "maintenance" }), "application/json"]]) {
      globalThis.fetch = async () => new Response(body, { status: code, headers: { "Content-Type": type } });
      await assert.rejects(status.readHealth(), error => error.status === code, `${code} ${body}`);
    }
  } finally { globalThis.fetch = original; }
});

test("the composer's one button and the model metadata under it use separate compact flex groups", async () => {
  const app = await readFile(new URL("../src/OrganizationApp.tsx", import.meta.url), "utf8");
  const css = await readFile(new URL("../src/organization.css", import.meta.url), "utf8");
  assert.ok(app.includes('className="org-composer-send"') && app.includes('className="org-composer-model"'));
  assert.match(css, /org-composer-model \.org-model-menu[^}]*min-width:\s*0/);
  assert.doesNotMatch(app, /<ReasoningEffort/);
  assert.ok(app.includes("stop_active: true"));
  assert.ok(!app.includes("Stop active work first:"));
});

test("bibliography explains statements instead of exposing technical hashes in its main heading", () => {
  const html = renderToStaticMarkup(React.createElement(bibliography.ResearchBibliography, { organizationId: "org", onAuthLost() {} }));
  assert.ok(html.includes("Sources, exact statements, and their ledger links"));
  assert.ok(!html.includes("SHA-256"));
});
