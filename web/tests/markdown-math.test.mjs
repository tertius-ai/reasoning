// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

let server, math, app;
before(async () => {
  server = await createServer({ appType: "custom", logLevel: "silent", server: { middlewareMode: true } });
  math = await server.ssrLoadModule("/src/markdownMath.ts");
  app = await server.ssrLoadModule("/src/OrganizationApp.tsx");
});
after(async () => { await server?.close(); });

const render = text => renderToStaticMarkup(React.createElement(app.MessageMarkdown, { markdown: math.normalizeMathDelimiters(text) }));
const displays = html => html.match(/class="katex-display"/g)?.length ?? 0;

test("conventional delimiters become dollar delimiters outside code", () => {
  assert.equal(math.normalizeMathDelimiters(String.raw`Let \(x\) be \[y\]`), "Let $x$ be \n$$\ny\n$$\n");
  assert.equal(math.normalizeMathDelimiters(String.raw`code \`\[x\]\` and
\`\`\`
\(y\)
\`\`\``), String.raw`code \`\[x\]\` and
\`\`\`
\(y\)
\`\`\``);
});

test("a row break with spacing is not a display-math delimiter", () => {
  for (const spacing of ["[2mm]", "[4pt]", "[1ex]"]) {
    const source = String.raw`\begin{cases} a \\` + spacing + String.raw` b \end{cases}`;
    assert.equal(math.normalizeMathDelimiters(String.raw`\[` + source + String.raw`\]`), "\n$$\n" + source + "\n$$\n");
  }
  assert.equal(math.normalizeMathDelimiters(String.raw`a \\(b\\)`), String.raw`a \\(b\\)`, "an escaped backslash before ( stays text");
  assert.equal(math.normalizeMathDelimiters(String.raw`\\\[x\\\]`), String.raw`\\` + "\n$$\nx" + String.raw`\\` + "\n$$\n",
    "an escaped backslash followed by a real delimiter keeps both");
});

test("math after a spaced row break still renders, and prose stays prose", () => {
  const reply = String.raw`Consequently, for every $q$:
\[
\begin{cases} \{N_qK_3(R/I^r)\}_r\longrightarrow \{N_qK_3(B/I^r)\}_r &\text{is an epimorphism},\\[2mm]
\{N_qK_2(R/I^r)\}_r\longrightarrow \{N_qK_2(B/I^r)\}_r &\text{is a monomorphism}. \end{cases} \tag{**}
\]
These are categorical statements in $\operatorname{Pro}(\mathrm{Ab})$. The same sequence proves
\[
N_qK_2(R)\xrightarrow{\;\sim\;}N_qK_2(\widehat R).
\]`;
  const html = render(reply);
  assert.equal(displays(html), 2, "both display formulas render");
  // KaTeX keeps each formula's source in a MathML annotation; only what is shown matters here.
  const shown = html.replace(/<annotation[^>]*>[\s\S]*?<\/annotation>/g, "");
  assert.ok(!shown.includes("2mm]"), "the row spacing is not printed as text");
  assert.ok(!shown.includes("$"), "no delimiter is left over as text");
  assert.match(html, /These are categorical statements in <span class="katex">/, "inline math after the cases block renders");
  assert.ok(!html.includes("katex-error"), "KaTeX accepts every formula");
});
