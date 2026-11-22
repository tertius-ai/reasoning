// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

// The PDF viewer ships pdf.js's JBIG2 and OpenJPEG decoders and two Foxit standard fonts as separate build files.
// Their notices are kept in public/licenses; an upgrade of pdfjs-dist that changes one fails here.
const NOTICES = {
  "pdf.js-JBIG2.txt": "wasm/LICENSE_JBIG2",
  "pdf.js-JBIG2-wasm.txt": "wasm/LICENSE_PDFJS_JBIG2",
  "pdf.js-OpenJPEG.txt": "wasm/LICENSE_OPENJPEG",
  "pdf.js-OpenJPEG-wasm.txt": "wasm/LICENSE_PDFJS_OPENJPEG",
  "pdf.js-Foxit-fonts.txt": "standard_fonts/LICENSE_FOXIT",
};

test("bundled pdf.js decoder and font notices match the installed pdfjs-dist", async () => {
  for (const [notice, upstream] of Object.entries(NOTICES)) {
    const [kept, shipped] = await Promise.all([
      readFile(new URL(`../public/licenses/${notice}`, import.meta.url), "utf8"),
      readFile(new URL(`../node_modules/pdfjs-dist/${upstream}`, import.meta.url), "utf8"),
    ]);
    assert.equal(kept, shipped, notice);
  }
});
