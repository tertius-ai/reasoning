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
  assert.match(html, /Attach picture or PDF/);
  assert.match(html, /class="org-upload-plus"/); assert.match(html, /<svg width="18"/);
  assert.match(html, /<path d="M12 4.5v15M4.5 12h15"/, "the drawn plus shared by every upload button");
  assert.match(html, /accept="image\/png,image\/jpeg,image\/webp,application\/pdf"/);
  assert.doesNotMatch(html, /image\/svg|iframe/);
});
