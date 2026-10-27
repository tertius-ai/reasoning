// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

let server, mentions, models, browser, ledger, tex;
before(async () => {
  server = await createServer({ appType: "custom", logLevel: "silent", server: { middlewareMode: true } });
  mentions = await server.ssrLoadModule("/src/composerMentions.ts");
  models = await server.ssrLoadModule("/src/botModels.ts");
  browser = await server.ssrLoadModule("/src/ModelBrowser.tsx");
  ledger = await server.ssrLoadModule("/src/Ledger.tsx");
  tex = await server.ssrLoadModule("/src/TexRequestDialog.tsx");
});
after(async () => { await server?.close(); });

test("readable mentions retain exact bot identities, including duplicate names", () => {
  const text = "@Math Bot and @Math Bot";
  const bindings = [{ start: 0, end: 9, id: "a".repeat(32), label: "@Math Bot" },
    { start: 14, end: 23, id: "b".repeat(32), label: "@Math Bot" }];
  assert.equal(mentions.encodeMentions(text, bindings), `@[${"a".repeat(32)}] and @[${"b".repeat(32)}]`);
  const edited = "Hello " + text;
  assert.equal(mentions.encodeMentions(edited, mentions.editMentions(text, edited, bindings)),
    `Hello @[${"a".repeat(32)}] and @[${"b".repeat(32)}]`);
  const changed = "@Math Boy and @Math Bot";
  assert.equal(mentions.encodeMentions(changed, mentions.editMentions(text, changed, bindings)),
    `@Math Boy and @[${"b".repeat(32)}]`);
});

test("per-invocation model mentions retain stable routing metadata", () => {
  const label = "@Math Bot-gpt-6.1", binding = { start: 0, end: label.length, id: "a".repeat(32), label, model: "gpt-6.1" };
  assert.equal(mentions.encodeMentions(label + " explain", [binding]), `@[${binding.id}:gpt-6.1] explain`);
  assert.equal(mentions.encodeMentions("@Math Bot-gpt-6.2 explain", mentions.editMentions(label, "@Math Bot-gpt-6.2 explain", [binding])), "@Math Bot-gpt-6.2 explain");
});

test("bot effort mentions retain the exact model and level", () => {
  const label = "@Math Bot-gpt-5.5!high";
  const binding = { start: 0, end: label.length, id: "a".repeat(32), label, model: "gpt-5.5", effort: "high" };
  assert.equal(mentions.encodeMentions(label + " explain", [binding]), `@[${binding.id}:gpt-5.5!high] explain`);
  const id = `bot/${binding.id}/gpt-5.5`;
  const catalog = models.withBotModels(null, [{ id, name: "Math Bot · gpt-5.5", engine: "codex", efforts: ["high"] }]);
  assert.deepEqual(catalog.models[0].reasoning_efforts, ["high"]);
});

test("discovered long-context aliases and ultra effort retain exact routing", () => {
  for (const [model, effort] of [["opus[1m]", "max"], ["gpt-discovered", "ultra"]]) {
    const label = `@Bot-${model}!${effort}`, id = "a".repeat(32);
    assert.equal(mentions.encodeMentions(label, [{ start: 0, end: label.length, id, label, model, effort }]),
      `@[${id}:${model}!${effort}]`);
  }
});

test("bot model presentation is readable and does not invent OpenRouter prices", () => {
  const id = `bot/${"a".repeat(32)}/default`;
  const catalog = models.withBotModels(null, [{ id, name: "Math Bot · default", engine: "codex" }]);
  assert.equal(browser.modelSections([id], catalog)[0].title, "Your connected bots");
  const html = renderToStaticMarkup(React.createElement(browser.ModelLine, { id, model: catalog.models[0], allowed: true }));
  assert.match(html, /Math Bot/); assert.doesNotMatch(html, /bot\/[a-f0-9]{32}|\$0/);
});

test("TeX setup offers only connected locally-enabled bots and preserves the exact source", () => {
  const id = "a".repeat(32), source = "\\documentclass{article}\n\\begin{document}Exact source.\\end{document}";
  const request = tex.texRequest(source);
  assert.ok(request.includes("```tex\n" + source + "\n```"));
  assert.match(request, /tex_compile|owner.*approval|Do not compile on the chat server/);
  const bots = [
    { id: "first", bot_id: id, name: "Local · default", online: true, local_tools: true },
    { id: "second", bot_id: id, name: "Local · alternate", online: true, local_tools: true },
    { id: "offline", bot_id: "b", name: "Offline", online: false, local_tools: true },
    { id: "ungranted", bot_id: "c", name: "No execution grant", online: true, local_tools: false },
  ];
  const html = renderToStaticMarkup(React.createElement(tex.TexRequestDialog, { bots, names: { [id]: "Local" }, onClose() {}, onPrepare() {} }));
  assert.match(html, /nothing runs until you send it/);
  assert.doesNotMatch(html, /Offline|No execution grant|alternate/);
  assert.equal((html.match(/<option /g) ?? []).length, 1);
  const empty = renderToStaticMarkup(React.createElement(tex.TexRequestDialog, { bots: [], names: {}, onClose() {}, onPrepare() {} }));
  assert.match(empty, /No connected bot advertises local execution/);
  assert.match(empty, /<button[^>]*disabled=""[^>]*>Prepare bot message/);
});

test("organization reference inspector blocks executable source URLs and local-source controls", () => {
  const html = renderToStaticMarkup(React.createElement(ledger.RefDetail, { projectId: "org", sourceEnabled: false,
    reference: { reference_id: "ref_1", author: "Author", title: "Title", url: "javascript:alert(1)" }, citing: [],
    onBack() {}, onOpenClaim() {}, onChanged() {},
  }));
  assert.match(html, /Author/); assert.match(html, /Title/);
  assert.doesNotMatch(html, /javascript:|upload source|Source file|\/api\/projects/);
});
