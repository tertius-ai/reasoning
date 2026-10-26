import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

let server, files;
before(async () => {
  server = await createServer({ appType: "custom", logLevel: "silent", server: { middlewareMode: true } });
  files = await server.ssrLoadModule("/src/ReplyFiles.tsx");
});
after(async () => { await server?.close(); });

const file = (path, revision, total_characters) => {
  const url = `/api/organizations/o/topics/t/research-files/content?path=${encodeURIComponent(path)}&revision=${revision}`;
  return { path, revision, sha256: "a".repeat(64), total_characters, url, download_url: url + "&download=true" };
};

test("a reply shows one chip per saved file with its revision, size, download and open", () => {
  const html = renderToStaticMarkup(React.createElement(files.ReplyFiles, {
    topicId: "t", files: [file("notes/dm-reconstruction-roadmap.tex", 1, 18214), file("refs.bib", 3, 412)],
  }));
  assert.match(html, /aria-label="Files saved by this reply"/);
  assert.match(html, /title="notes\/dm-reconstruction-roadmap.tex">dm-reconstruction-roadmap.tex</);
  assert.match(html, /rev 1 · 18k chars/); assert.match(html, /rev 3 · 412 chars/);
  assert.match(html, />TEX</); assert.match(html, />BIB</);
  assert.match(html, /href="[^"]*revision=1&amp;download=true" download="dm-reconstruction-roadmap.tex" aria-label="Download dm-reconstruction-roadmap.tex"/);
  assert.match(html, /aria-label="Open refs.bib in project files"/);
});
