// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { extname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import react from "@vitejs/plugin-react";
import { chromium } from "playwright";
import { build } from "vite";

const root = fileURLToPath(new URL("../", import.meta.url));

// The policy the public server sends with every response, the PDF worker script included.
async function productionPolicy() {
  const source = await readFile(new URL("../../src/reasoning_ledger/server/api.py", import.meta.url), "utf8");
  const definition = /^APPLICATION_CSP = \(\n([\s\S]*?)\n\)/m.exec(source);
  assert.ok(definition, "APPLICATION_CSP is defined in api.py");
  return [...definition[1].matchAll(/"([^"]*)"/g)].map(match => match[1]).join("");
}

function pdfDocument(objects) {
  const parts = [Buffer.from("%PDF-1.4\n")], offsets = [];
  let length = parts[0].length;
  objects.forEach((value, index) => {
    offsets.push(length);
    const object = Buffer.concat([Buffer.from(`${index + 1} 0 obj\n`), Buffer.from(value), Buffer.from("\nendobj\n")]);
    parts.push(object); length += object.length;
  });
  const trailer = `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`
    + offsets.map(offset => String(offset).padStart(10, "0") + " 00000 n \n").join("")
    + `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${length}\n%%EOF\n`;
  return Buffer.concat([...parts, Buffer.from(trailer)]);
}

function onePage(width, height, resources, content) {
  return [
    "<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 ${width} ${height}] /Resources ${resources} /Contents 4 0 R >>`,
    `<< /Length ${content.length} >>\nstream\n${content}\nendstream`,
  ];
}

// A scanned page: one 64x32 bilevel image, CCITT Group 4 encoded, with a black 48x16 block at (8, 8) on white.
// The bytes are Pillow's Group 4 TIFF strip for that image (ImageDraw rectangle [8, 8, 55, 23]). Pillow stores
// bilevel TIFF as BlackIsZero, so its coded runs read with /BlackIs1 true.
const G4_BLOCK = Buffer.from("26a0786ffffc8a17fffffffffffffff8ffff001001", "hex");
function scannedPdf() {
  const image = Buffer.concat([
    Buffer.from("<< /Type /XObject /Subtype /Image /Width 64 /Height 32 /ColorSpace /DeviceGray /BitsPerComponent 1 "
      + `/Filter /CCITTFaxDecode /DecodeParms << /K -1 /Columns 64 /Rows 32 /BlackIs1 true >> /Length ${G4_BLOCK.length} >>\nstream\n`),
    G4_BLOCK, Buffer.from("\nendstream"),
  ]);
  return pdfDocument([...onePage(256, 128, "<< /XObject << /Im1 5 0 R >> >>", "q 256 0 0 128 0 0 cm /Im1 Do Q"), image]);
}

// Text in a standard font the file does not embed, drawn with system fonts because the viewer disables font faces.
function textPdf() {
  return pdfDocument([...onePage(300, 144, "<< /Font << /F1 5 0 R >> >>", "BT /F1 24 Tf 20 60 Td (One plus one is two.) Tj ET"),
    "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]);
}

const TYPES = { ".html": "text/html", ".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css",
  ".wasm": "application/wasm", ".pdf": "application/pdf" };

test("the PDF viewer draws scanned (CCITT) page images and non-embedded standard-font text under the production policy", { timeout: 120000 }, async () => {
  const policy = await productionPolicy();
  const outDir = await mkdtemp(join(tmpdir(), "pdf-viewer-"));
  const entry = `import React from "react"; import {createRoot} from "react-dom/client"; import {PdfViewer} from "/src/PdfViewer.tsx";
createRoot(document.getElementById("root")).render(React.createElement("main", null,
  React.createElement("section", {className: "scan"}, React.createElement(PdfViewer, {url: "/files/scan.pdf", name: "scan.pdf"})),
  React.createElement("section", {className: "text"}, React.createElement(PdfViewer, {url: "/files/text.pdf", name: "text.pdf"}))));`;
  // A production build, named like the release build, so the viewer loads the same hashed worker, decoder and font files.
  await build({
    root, configFile: false, publicDir: false, logLevel: "warn",
    plugins: [react(), { name: "pdf-viewer-fixture", resolveId(id) { if (id === "pdf-viewer-fixture") return "\0pdf-viewer-fixture"; },
      load(id) { if (id === "\0pdf-viewer-fixture") return entry; } }],
    build: { outDir, emptyOutDir: true, rollupOptions: { input: { fixture: "pdf-viewer-fixture" }, output: {
      entryFileNames: "assets/[name].js", chunkFileNames: "assets/[name]-[hash].js", assetFileNames: "assets/[name]-[hash][extname]" } } },
  });
  const files = { "/files/scan.pdf": scannedPdf(), "/files/text.pdf": textPdf(),
    "/index.html": Buffer.from('<!doctype html><div id="root"></div><script type="module" src="/assets/fixture.js"></script>') };
  const requests = [], missing = [];
  const server = createServer(async (request, response) => {
    const url = new URL(request.url, "http://localhost").pathname, path = url === "/" ? "/index.html" : url;
    requests.push(path);
    response.setHeader("Content-Security-Policy", policy);
    response.setHeader("X-Content-Type-Options", "nosniff");
    let body = files[path];
    if (!body && path.startsWith("/assets/")) body = await readFile(join(outDir, normalize(path))).catch(() => undefined);
    if (!body) { missing.push(path); response.statusCode = 404; response.end(); return; }
    response.setHeader("Content-Type", TYPES[extname(path)] ?? "application/octet-stream");
    response.end(body);
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const browser = await chromium.launch({ headless: true });
  const errors = [], messages = [];
  try {
    const page = await browser.newPage();
    page.on("pageerror", error => errors.push(error.message));
    page.on("console", message => { if (["error", "warning"].includes(message.type())) messages.push(message.text()); });
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    for (const name of ["scan.pdf", "text.pdf"]) {
      await page.getByRole("img", { name: `${name}, page 1` }).waitFor();
      await page.waitForFunction(label => {
        const canvas = document.querySelector(`canvas[aria-label="${label}"]`);
        return canvas && canvas.width > 0 && !canvas.classList.contains("loading");
      }, `${name}, page 1`);
    }
    const pixels = await page.evaluate(() => Object.fromEntries(["scan.pdf", "text.pdf"].map(name => {
      const canvas = document.querySelector(`canvas[aria-label="${name}, page 1"]`);
      const { data, width, height } = canvas.getContext("2d").getImageData(0, 0, canvas.width, canvas.height);
      const dark = (x, y) => { const at = (Math.floor(y) * width + Math.floor(x)) * 4; return data[at] + data[at + 1] + data[at + 2] < 192; };
      let count = 0;
      for (let y = 0; y < height; y++) for (let x = 0; x < width; x++) if (dark(x, y)) count++;
      return [name, { fraction: count / (width * height), centre: dark(width / 2, height / 2), corner: dark(width * 0.05, height * 0.1) }];
    })));
    // The block covers 48x16 of the 64x32 image: 37.5% of the page, the centre included, the corner not.
    assert.ok(pixels["scan.pdf"].centre && !pixels["scan.pdf"].corner, `scanned page image is drawn: ${JSON.stringify(pixels["scan.pdf"])}`);
    assert.ok(Math.abs(pixels["scan.pdf"].fraction - 0.375) < 0.05, `scanned block size: ${pixels["scan.pdf"].fraction}`);
    assert.ok(pixels["text.pdf"].fraction > 0.005, `standard-font text is drawn: ${pixels["text.pdf"].fraction}`);
    assert.ok(requests.some(path => /^\/assets\/jbig2-[A-Za-z0-9_-]{8}\.wasm$/.test(path)), "the CCITT decoder loads from its hashed build file");
    assert.deepEqual(missing, []);
    assert.deepEqual(errors, []);
    assert.deepEqual(messages.filter(text => /Unable to decode|wasm|Content Security Policy|Refused/i.test(text)), []);
  } finally {
    await browser.close(); await new Promise(resolve => server.close(resolve)); await rm(outDir, { recursive: true, force: true });
  }
});
