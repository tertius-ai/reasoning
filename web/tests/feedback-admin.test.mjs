// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

let server, feedback;
before(async () => {
  server = await createServer({ appType: "custom", logLevel: "silent", server: { middlewareMode: true } });
  feedback = await server.ssrLoadModule("/src/Feedback.tsx");
});
after(async () => { await server?.close(); });

const at = Date.UTC(2026, 9, 26, 14, 3) / 1000;
const bug = {
  id: "r1", kind: "bug", body: "Stop left the spinner running.\n\nExpected: the run stops.", created_at: at, resolved_at: null, resolved_by: null,
  reporter: { id: "u1", name: "Maya Chen" },
  context: { view: "Research › #algebra", release: "3f9a1c2b7e44", server_release: "1c07d9e3a5b2" + "0".repeat(52), browser: "Firefox/131.0", viewport: "1440×900",
    requests: [{ at: at - 60, method: "POST", path: "/api/organizations/o/runs/r/interrupt", status: 502, detail: "upstream timeout" },
      { at: at - 30, method: "GET", path: "/api/organizations/o/topics/t/events", status: 0, detail: "" }],
    errors: [{ at: at - 20, message: "TypeError: x is undefined", stack: "at applyRunPatch (topicEvents.ts:96:18)" }] },
};
const idea = { id: "r2", kind: "idea", body: "Pin a ledger claim.", created_at: at - 3600, resolved_at: null, resolved_by: null, reporter: { id: "u2", name: "Tomás Ruiz" }, context: {} };
const done = { ...idea, id: "r3", body: "A shortcut to the next unread topic.", resolved_at: at + 600, resolved_by: "u0" };

test("a report copies as Markdown with its diagnostics folded and times in UTC", () => {
  assert.equal(feedback.reportMarkdown(bug), [
    "### Bug report from Maya Chen · 2026-10-26 14:03 UTC", "", "Stop left the spinner running.\n\nExpected: the run stops.", "",
    "<details><summary>Diagnostics</summary>", "",
    "- View: Research › #algebra", "- Release: 3f9a1c2b7e44 (server 1c07d9e3a5b2)", "- Browser: Firefox/131.0", "- Window: 1440×900", "",
    "**Failed requests (2)**", "```", "14:02 POST /api/organizations/o/runs/r/interrupt → 502 upstream timeout", "14:02 GET /api/organizations/o/topics/t/events → no response", "```", "",
    "**Page errors (1)**", "```", "TypeError: x is undefined", "  at applyRunPatch (topicEvents.ts:96:18)", "```", "", "</details>",
  ].join("\n"));
  assert.equal(feedback.reportMarkdown(done), "### Idea from Tomás Ruiz · 2026-10-26 13:03 UTC\n\nA shortcut to the next unread topic.\n\nResolved 2026-10-26 14:13 UTC.");
});

test("a diagnostic containing backticks cannot close its code block", () => {
  const text = feedback.reportMarkdown({ ...bug, context: { requests: [], errors: [{ at, message: "SyntaxError: unexpected ````", stack: "" }] } });
  assert.match(text, /\n`{5}\nSyntaxError: unexpected `{4}\n`{5}\n/);
  assert.match(text, /\*\*Failed requests \(0\)\*\*\nNone\./);
});

test("reports are grouped by status; open reports are listed collapsed and resolved ones start hidden", () => {
  const html = renderToStaticMarkup(React.createElement(feedback.FeedbackReports, { reports: [bug, idea, done], busy: "", onResolve() {}, onCopyFailed() {} }));
  assert.match(html, /2 open · 1 resolved/);
  assert.match(html, /aria-expanded="true" aria-controls="org-feedback-group-open">.*Open<span class="org-feedback-count">2<\/span>/);
  assert.match(html, /aria-expanded="false" aria-controls="org-feedback-group-resolved">.*Resolved<span class="org-feedback-count">1<\/span>/);
  assert.ok(!html.includes("A shortcut to the next unread topic."), "the resolved group starts collapsed");
  assert.match(html, /<span class="org-feedback-summary">Stop left the spinner running.<\/span>/, "a collapsed report shows its first line");
  assert.ok(!html.includes("Expected: the run stops."), "the rest of the message waits until the report is expanded");
  assert.ok(html.includes("2 failed requests, 1 page error"), "collapsed rows summarize the diagnostics");
  assert.equal((html.match(/aria-expanded="false" aria-controls="org-feedback-report-/g) ?? []).length, 2);
  assert.ok(!html.includes("Copy report"), "copying is offered in an expanded report");
});
