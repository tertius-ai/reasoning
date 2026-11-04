import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { readFileSync } from "node:fs";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

let server, mathematics, files, bibliography;
before(async () => {
  server = await createServer({ appType: "custom", logLevel: "silent", server: { middlewareMode: true } });
  mathematics = await server.ssrLoadModule("/src/MathematicsBrowser.tsx");
  files = await server.ssrLoadModule("/src/ResearchFiles.tsx");
  bibliography = await server.ssrLoadModule("/src/ResearchBibliography.tsx");
});
after(async () => { await server?.close(); });

test("mathematical browser has scoped read-only and human writing states", () => {
  const reader = renderToStaticMarkup(React.createElement(mathematics.MathematicsBrowser, {
    organizationId: "org", topicId: "beta", onAuthLost: () => {},
  }));
  assert.match(reader, /Mathematical research ledger/);
  assert.match(reader, /Search mathematical claims/);
  assert.doesNotMatch(reader, /Record statement/);
  const writer = renderToStaticMarkup(React.createElement(mathematics.MathematicsBrowser, {
    organizationId: "org", topicId: "beta", canWrite: true, onAuthLost: () => {},
  }));
  assert.match(writer, /Record statement/);
});

test("research math rendering blocks injected HTML and remote tracking images", () => {
  const rendered = renderToStaticMarkup(React.createElement(mathematics.ResearchMarkdown, {},
    "An identity: $x^2$. <script>bad()</script> ![tracking](https://example.org/pixel)"));
  assert.match(rendered, /katex/);
  assert.doesNotMatch(rendered, /<script|<img|example\.org\/pixel/);
});

test("files and bibliography retain compact useful empty states", () => {
  const view = renderToStaticMarkup(React.createElement(files.ResearchFiles, {
    organizationId: "org", topicId: "beta", canWrite: false, onAuthLost: () => {},
  }));
  assert.match(view, /Project files/);
  assert.doesNotMatch(view, />New file</);
  const refs = renderToStaticMarkup(React.createElement(bibliography.ResearchBibliography, {
    organizationId: "org", onAuthLost: () => {},
  }));
  assert.match(refs, /Search research bibliography/);
});

test("file recovery separates retained source and unfinished drafts from completed files", () => {
  const props = {
    retained: [{ path: "survey.tex", recovery_path: "survey.recovered-42-0.tex", event_sequence: 42, tool_index: 0,
      size_bytes: 90609, sha256: "a".repeat(64) }],
    drafts: [{ draft_id: "unfinished", path: "sections.tex", size_bytes: 31000 }],
    root: "/organizations/org/topics/beta/research-files", canWrite: true, busy: false,
    recover: () => { throw new Error("Rendering must never recover or replay work"); },
  };
  const writer = renderToStaticMarkup(React.createElement(files.FileRecovery, props));
  assert.match(writer, /Recover a copy/);
  assert.match(writer, /without another model call/);
  assert.match(writer, /source may need review/);
  assert.match(writer, /Unfinished drafts/);
  assert.match(writer, /Download unfinished source/);
  assert.match(writer, /\/api\/organizations\/org\/topics\/beta\/research-files\/drafts\/unfinished\/content/);
  const reader = renderToStaticMarkup(React.createElement(files.FileRecovery, { ...props, canWrite: false }));
  assert.doesNotMatch(reader, /<button/);
  assert.doesNotMatch(writer, /Download complete file/);
});

test("ledger layout becomes a bounded single column on phones", () => {
  const css = readFileSync(new URL("../src/mathematics-browser.css", import.meta.url), "utf8");
  assert.match(css, /max-width:\s*650px/);
  assert.match(css, /grid-template-columns:\s*1fr/);
  assert.match(css, /max-height:\s*35vh/);
});
