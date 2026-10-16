import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { createServer } from "vite";

let server;
let collectPdfText;
let storablePreview;

before(async () => {
  server = await createServer({ appType: "custom", logLevel: "silent", server: { middlewareMode: true } });
  ({ collectPdfText, storablePreview } = await server.ssrLoadModule("/src/pdfText.ts"));
});

after(async () => {
  await server?.close();
});

function pdf(pages, failAt = Infinity) {
  return {
    numPages: pages.length,
    async getPage(number) {
      if (number >= failAt) throw new Error("worker destroyed after the upload timeout");
      return { async getTextContent() { return { items: pages[number - 1].map(str => ({ str })).concat([{ type: "beginMarkedContent" }]) }; } };
    },
  };
}

test("extracts every page within the stored byte allowance, not only the first twenty", async () => {
  const pages = Array.from({ length: 40 }, (_, index) => [`Lemma ${index + 1}.`, "x".repeat(500)]);
  const text = await collectPdfText(pdf(pages));
  assert.match(text, /\[Page 40\]\nLemma 40\./);
  assert.ok(new TextEncoder().encode(text).length <= 60000);
});

test("stops at the byte allowance on a UTF-8 boundary", async () => {
  const text = await collectPdfText(pdf([["α".repeat(40000)], ["never reached"]]), 1001);
  assert.ok(new TextEncoder().encode(text).length <= 1001);
  assert.ok(!text.includes("�") && !text.includes("never reached"));
});

test("keeps pages read before a timeout or damaged page", async () => {
  const text = await collectPdfText(pdf([["First page."], ["Second page."], ["Third page."]], 3));
  assert.equal(text, "\n[Page 1]\nFirst page.\n[Page 2]\nSecond page.");
});

test("removes characters the preview endpoint refuses", () => {
  assert.equal(storablePreview("a\u0000b\uD800c"), "a b�c");
  assert.equal(storablePreview("𝔽 field"), "𝔽 field");
});
