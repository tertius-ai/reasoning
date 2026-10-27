// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { realpath } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

const org = "a".repeat(32), topic = "b".repeat(32), root = fileURLToPath(new URL("../", import.meta.url));
function pdf() {
  const text = "BT /F1 12 Tf 20 100 Td (A proof of one plus one equals two.) Tj ET";
  const objects = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 144] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
    `<< /Length ${text.length} >>\nstream\n${text}\nendstream`, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"];
  let body = "%PDF-1.4\n", offsets = [0];
  objects.forEach((value, index) => { offsets.push(Buffer.byteLength(body)); body += `${index + 1} 0 obj\n${value}\nendobj\n`; });
  const xref = Buffer.byteLength(body);
  body += `xref\n0 6\n0000000000 65535 f \n` + offsets.slice(1).map(offset => String(offset).padStart(10, "0") + " 00000 n \n").join("");
  return Buffer.from(body + `trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`);
}

test("real browser uploads, scoped image rendering, local PDF canvas/text and Markdown previews", { timeout: 60000 }, async () => {
  const entry = `import React, {useState} from "react"; import {createRoot} from "react-dom/client";
import {AttachmentButton,MediaScope} from "/src/TopicMedia.tsx"; import {ResearchMarkdown} from "/src/MathematicsBrowser.tsx";
function Fixture(){ const [text,setText]=useState(''); return React.createElement(MediaScope.Provider,{value:{organizationId:'${org}',topicId:'${topic}'}},
React.createElement('main',null,React.createElement(AttachmentButton,{organizationId:'${org}',topicId:'${topic}',onInsert:value=>setText(old=>old+'\\n'+value)}),
React.createElement('textarea',{'aria-label':'Markdown',value:text,onChange:event=>setText(event.target.value)}), React.createElement(ResearchMarkdown,null,text))); }
createRoot(document.getElementById('root')).render(React.createElement(Fixture));`;
  const plugin = { name: "media-fixture", resolveId(id) { if (id === "/media-fixture.tsx") return "\0media-fixture"; },
    load(id) { if (id === "\0media-fixture") return entry; }, configureServer(server) {
      server.middlewares.use(async (req, res, next) => { if (req.url !== "/media-fixture.html") return next();
        res.setHeader("Content-Type", "text/html"); res.end(await server.transformIndexHtml(req.url,
          '<!doctype html><div id="root"></div><script type="module" src="/media-fixture.tsx"></script>')); });
    } };
  const server = await createServer({ root, plugins: [plugin], server: { host: "127.0.0.1", port: 0,
    fs: { allow: [root, await realpath(root + "node_modules")] } } });
  await server.listen(); const browser = await chromium.launch({ headless: true });
  const uploads = new Map(), errors = [], requested = [];
  try {
    const page = await browser.newPage(); page.on("pageerror", error => errors.push(error.message));
    await page.route("**/api/**", async route => {
      const request = route.request(), path = new URL(request.url()).pathname; requested.push(path);
      let data = {}, status = 200;
      const match = /\/media\/([a-f0-9]{32})(?:\/(.*))?$/.exec(path);
      if (path.endsWith("/media") && request.method() === "POST") {
        const id = String(uploads.size + 1).padStart(32, "0"); uploads.set(id, { ...request.postDataJSON(), chunks: [] });
        data = { id, chunk_bytes: 32768 };
      } else if (match) {
        const file = uploads.get(match[1]), action = match[2] ?? "";
        if (!file) status = 404;
        else if (action.startsWith("chunks/")) file.chunks[Number(action.split("/")[1])] = Buffer.from(request.postDataJSON().data_base64, "base64");
        else if (action === "finish") {
          file.bytes = Buffer.concat(file.chunks); Object.assign(file, request.postDataJSON());
          assert.equal(createHash("sha256").update(file.bytes).digest("hex"), file.sha256);
          const url = `/api/organizations/${org}/topics/${topic}/media/${match[1]}/${file.sha256}/content`;
          file.extraction = file.mime === "application/pdf" ? { status: "ready", method: "server", pages_read: 1, total_pages: 1 }
            : file.mime === "text/markdown" ? { status: "ready", method: "text", truncated: false } : undefined;
          data = { ...file, chunks: undefined, bytes: undefined, id: match[1], url, markdown: `![${file.name}](${url})` };
        } else if (action.endsWith("/read")) data = { mime: file.mime, size_bytes: file.size_bytes, extraction: file.extraction };
        else if (action.endsWith("/content")) {
          await route.fulfill({ status: 200, contentType: file.mime, body: file.bytes }); return;
        }
      } else status = 404;
      await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(data) });
    });
    await page.goto(server.resolvedUrls.local[0] + "media-fixture.html");
    const png = Buffer.from(await page.evaluate(() => {
      const canvas = document.createElement("canvas"); canvas.width = 64; canvas.height = 32;
      const context = canvas.getContext("2d"); context.fillStyle = "red"; context.fillRect(0, 0, 64, 32);
      return canvas.toDataURL("image/png").split(",")[1];
    }), "base64");
    const input = page.locator('input[type="file"]');
    await input.setInputFiles({ name: "plot.png", mimeType: "image/png", buffer: png });
    const image = page.locator("img.topic-image"); await image.waitFor();
    await page.waitForFunction(() => document.querySelector("img.topic-image")?.naturalWidth === 64);
    const picture = [...uploads.values()][0];
    assert.ok(picture.vision_base64 && Buffer.from(picture.vision_base64, "base64").length <= 32768);
    await input.setInputFiles({ name: "proof.pdf", mimeType: "application/pdf", buffer: pdf() });
    await page.getByText("proof.pdf", { exact: true }).waitFor();
    const document = [...uploads.values()][1]; assert.equal(document.preview_text, "");
    await page.getByText(/Text available for research/).waitFor();
    assert.equal(await page.locator("canvas").count(), 0, "closed PDFs must not render pages");
    await page.getByText("proof.pdf", { exact: true }).click();
    await page.getByRole("img", { name: "proof.pdf, page 1" }).waitFor();
    await page.waitForFunction(() => document.querySelector(".topic-pdf canvas")?.width > 0);
    await page.waitForFunction(() => document.querySelector(".topic-pdf canvas") && !document.querySelector(".topic-pdf canvas").classList.contains("loading"));
    assert.match(await page.locator(".topic-pdf").innerText(), /page 1 of 1/);
    assert.equal(await page.locator("iframe,object,embed").count(), 0);
    // Browsers often report no type for a .md file; it is recognised by its extension.
    const notes = "# Lemma 2\n\nIf $V$ is finite-dimensional, then $\\dim\\ker T + \\dim\\operatorname{im} T = \\dim V$.\n\n"
      + "![remote](https://example.invalid/tracker.png) <b>raw</b>\n";
    await input.setInputFiles({ name: "notes.md", mimeType: "", buffer: Buffer.from(notes) });
    await page.getByText("notes.md", { exact: true }).waitFor();
    assert.equal([...uploads.values()][2].mime, "text/markdown");
    await page.getByText("Text available for research · 1 KB.", { exact: true }).waitFor();
    await page.getByText("notes.md", { exact: true }).click();
    const preview = page.locator(".topic-markdown-preview");
    await preview.getByRole("heading", { name: "Lemma 2" }).waitFor();
    assert.ok(await preview.locator(".katex").count() >= 1, "math renders with KaTeX");
    assert.equal(await preview.locator("img,b").count(), 0, "a previewed file shows no images or raw HTML");
    await page.getByRole("link", { name: "Download Markdown" }).waitFor();
    if (process.env.MEDIA_SCREENSHOT) await page.locator(".topic-markdown").screenshot({ path: process.env.MEDIA_SCREENSHOT });
    const unavailable = `/api/organizations/${org}/topics/${topic}/media/${"f".repeat(32)}/${"a".repeat(64)}/content`;
    await page.getByLabel("Markdown", { exact: true }).fill(`![private](${unavailable})`);
    await page.getByText("Attachment unavailable", { exact: true }).waitFor();
    assert.ok(!requested.includes(unavailable), "denied metadata must not request attachment bytes");
    await page.getByLabel("Markdown", { exact: true }).fill("![track](https://example.invalid/tracker.png)\n![foreign](/api/organizations/" + "e".repeat(32) + "/topics/" + topic + "/media/" + "f".repeat(32) + "/" + "a".repeat(64) + "/content)");
    assert.equal(await page.locator("img.topic-image,details.topic-pdf").count(), 0);
    assert.ok(requested.every(url => !url.includes("example.invalid") && !url.includes("/organizations/" + "e".repeat(32))));
    assert.deepEqual(errors, []);
  } finally { await browser.close(); await server.close(); }
});
