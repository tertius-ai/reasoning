// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { before, after, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";
let server, media, markdown;
before(async () => {
  server = await createServer({ appType: "custom", logLevel: "silent", server: { middlewareMode: true } });
  media = await server.ssrLoadModule("/src/TopicMedia.tsx");
  markdown = await server.ssrLoadModule("/src/MathematicsBrowser.tsx");
});
after(async () => { await server?.close(); });
const org = "a".repeat(32), topic = "b".repeat(32), id = "c".repeat(32), sha = "d".repeat(64);
const url = `/api/organizations/${org}/topics/${topic}/media/${id}/${sha}/content`;
test("only exact private immutable attachment routes are accepted", () => {
  assert.ok(media.mediaReference(url, org));
  for (const unsafe of ["https://tracking.example/img.png", "//example.org/image", "data:image/svg+xml,<svg/>", url + "?secret=x", url.replace(sha, "current")]) {
    assert.equal(media.mediaReference(unsafe, org), null);
  }
  assert.equal(media.mediaReference(url, "e".repeat(32)), null);
});
test("markdown blocks remote tracking and raw HTML, embeds only scoped uploaded media", () => {
  const render = (text, scope) => renderToStaticMarkup(React.createElement(media.MediaScope.Provider, { value: scope },
    React.createElement(markdown.ResearchMarkdown, null, text)));
  const unsafe = render("![track](https://tracking.example/img.png) <img src='https://tracking.example/a'>", { organizationId: org });
  assert.doesNotMatch(unsafe, /<img|tracking\.example/);
  assert.match(render(`![plot](${url})`, { organizationId: org }), /Loading attachment/);
  assert.doesNotMatch(render(`![plot](${url})`, null), /Loading attachment/);
  assert.doesNotMatch(render(`![plot](${url})`, { organizationId: "e".repeat(32) }), /Loading attachment/);
});
test("attachment control offers bounded supported media without native HTML uploads", () => {
  const html = renderToStaticMarkup(React.createElement(media.AttachmentButton, { organizationId: org, topicId: topic, onInsert() {} }));
  assert.match(html, /Attach a file/);
  assert.match(html, /class="org-upload-plus"/); assert.match(html, /<svg width="18"/);
  assert.match(html, /<path d="M12 4.5v15M4.5 12h15"/, "the drawn plus shared by every upload button");
  // A topic takes any file: a type the server does not read is sent as a data file.
  assert.doesNotMatch(html, /accept=/);
  assert.match(html, /title="Attach pictures, PDFs, Markdown, TeX or any other file"/);
  assert.doesNotMatch(html, /image\/svg|iframe/);
});
test("direct messages keep their chooser filter while a topic composer takes any file", async () => {
  const direct = await server.ssrLoadModule("/src/DirectMedia.tsx");
  const button = dataFiles => renderToStaticMarkup(React.createElement(direct.AttachFilesButton, { disabled: false, onFiles() {}, dataFiles }));
  assert.match(button(false), /accept="image\/png,image\/jpeg,image\/webp,application\/pdf,text\/markdown,\.md,\.markdown,application\/x-tex,text\/x-tex,\.tex"/);
  assert.match(button(false), /title="Attach pictures, PDFs, Markdown or TeX"/);
  assert.doesNotMatch(button(true), /accept=/);
  const bundle = new File(["# v2 git bundle\n"], "work.bundle");
  assert.equal(direct.pendingFile(bundle, true).mime, "application/octet-stream");
  assert.equal(direct.pendingFile(bundle).mime, "");
});
test("any other file is a topic data file, offered only as a download chip", async () => {
  const file = (name, type = "", body = "x") => new File([body], name, { type });
  for (const [name, type] of [["work.bundle", ""], ["notes.txt", "text/plain"], ["page.html", "text/html"], ["vector.svg", "image/svg+xml"]]) {
    assert.equal(media.attachmentMime(file(name, type), true), "application/octet-stream", name);
    assert.notEqual(media.attachmentMime(file(name, type)), "application/octet-stream", name);
  }
  for (const [name, type, mime] of [["plot.png", "image/png", "image/png"], ["paper.pdf", "", "application/pdf"], ["notes.md", "", "text/markdown"],
    ["draft.tex", "", "application/x-tex"]]) assert.equal(media.attachmentMime(file(name, type), true), mime, name);
  await assert.rejects(media.uploadAttachment(file("empty.bin", "", ""), "/unused", undefined, true), /Choose a non-empty file of at most 8 MiB\./);
  await assert.rejects(media.uploadAttachment(file("work.bundle"), "/unused"), /Choose a PNG, JPEG, WebP, PDF, Markdown or TeX file/);
  assert.equal(media.fileBadge("application/octet-stream"), "FILE");
  const render = (text, scope) => renderToStaticMarkup(React.createElement(media.MediaScope.Provider, { value: scope },
    React.createElement(markdown.ResearchMarkdown, null, text)));
  const chip = render(`[work.bundle](${url})`, { organizationId: org });
  assert.match(chip, /class="topic-file-chip"/);
  assert.match(chip, new RegExp(`href="${url}" download="work.bundle"`));
  assert.match(chip, /<strong>work.bundle<\/strong>/);
  assert.doesNotMatch(chip, /<img|<iframe|canvas|Loading PDF/);
  const plain = render(`[work.bundle](${url})`, { organizationId: "e".repeat(32) });
  assert.doesNotMatch(plain, /topic-file-chip/);
  const replies = await server.ssrLoadModule("/src/MessageReplies.tsx");
  assert.equal(replies.replyExcerpt(`Here [work.bundle](${url}) and ![plot](${url})`), "Here 📎 work.bundle and 📎 plot");
});
test("Markdown and TeX files are recognised by extension and described by their size", async () => {
  const file = (name, type = "") => new File(["# Notes"], name, { type });
  assert.equal(media.attachmentMime(file("notes.md")), "text/markdown", "browsers often report no type for .md");
  assert.equal(media.attachmentMime(file("NOTES.Markdown", "text/x-markdown")), "text/markdown");
  assert.equal(media.attachmentMime(file("paper.pdf")), "application/pdf");
  assert.equal(media.attachmentMime(file("draft.tex")), "application/x-tex", "browsers often report no type for .tex");
  assert.equal(media.attachmentMime(file("DRAFT.TEX", "text/x-tex")), "application/x-tex");
  await assert.rejects(media.uploadAttachment(file("notes.txt", "text/plain"), "/unused"),
    /Choose a PNG, JPEG, WebP, PDF, Markdown or TeX file of at most 8 MiB\./);
  assert.equal(media.texLabel(18 * 1024, { status: "ready", method: "text", truncated: false }), "TeX source available for research · 18 KB.");
  assert.deepEqual(media.texTokens("\\section{A} $x$ % note\n50\\%").map(token => token.kind + ":" + token.text),
    ["command:\\section", "text:{A} ", "math:$x$", "text: ", "comment:% note", "text:\n50", "command:\\%"]);
  assert.deepEqual(["image/png", "text/markdown", "application/x-tex", "application/pdf", "application/octet-stream"].map(media.fileBadge),
    ["IMG", "MD", "TEX", "PDF", "FILE"]);
  assert.equal(media.markdownLabel(3 * 1024, { status: "ready", method: "text", truncated: false }), "Text available for research · 3 KB.");
  assert.equal(media.markdownLabel(3 * 1024 * 1024, { status: "ready", method: "text", truncated: true }),
    "Text available for research up to its first 2 MiB · 3.0 MB.");
});
test("a PDF whose extraction stopped at its time limit says how many pages were read", () => {
  assert.equal(media.extractionLabel({ status: "partial", method: "server", error: "time_limit", truncated: true, pages_read: 87, total_pages: 254 }),
    "Text extraction reached its time limit after 87 of 254 pages; that text is available for research. Provide a TeX/text source for the rest.");
  assert.equal(media.extractionLabel({ status: "partial", method: "server", error: null, truncated: true, pages_read: 200, total_pages: 254 }),
    "Only part of this PDF is readable as text · 200/254 pages. Check coverage or provide its TeX/text source.");
  assert.match(media.extractionLabel({ status: "unavailable", method: "server", error: "time_limit", truncated: true, pages_read: 0, total_pages: 9 }),
    /reached its resource limit/);
});
