// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { after, before, test } from "node:test";
import katex from "katex";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

let server, math, macros, app;
before(async () => {
  server = await createServer({ appType: "custom", logLevel: "silent", server: { middlewareMode: true } });
  math = await server.ssrLoadModule("/src/markdownMath.ts");
  macros = await server.ssrLoadModule("/src/katexMacros.ts");
  app = await server.ssrLoadModule("/src/OrganizationApp.tsx");
});
after(async () => { await server?.close(); });

const render = text => renderToStaticMarkup(React.createElement(app.MessageMarkdown, { markdown: math.normalizeMathDelimiters(text) }));
const displays = html => html.match(/class="katex-display"/g)?.length ?? 0;
// KaTeX keeps each formula's source in a MathML annotation; only what is shown matters here.
const shown = html => html.replace(/<annotation[^>]*>[\s\S]*?<\/annotation>/g, "");

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
  const dollars = String.raw`$$\begin{aligned} a &= b \\[2mm] c &= d \end{aligned}$$`;
  assert.equal(math.normalizeMathDelimiters(dollars), dollars, "a $$ block with a spaced row break is unchanged");
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
  assert.ok(!shown(html).includes("2mm]"), "the row spacing is not printed as text");
  assert.ok(!shown(html).includes("$"), "no delimiter is left over as text");
  assert.match(html, /These are categorical statements in <span class="katex">/, "inline math after the cases block renders");
  assert.ok(!html.includes("katex-error"), "KaTeX accepts every formula");
});

test("paper-style operator macros such as \\CH render as operator names", () => {
  for (const formula of [String.raw`\CH^3(Y)`, String.raw`\CH^3_L(Y)`, String.raw`\CH_L^3(Y)`, String.raw`\CH^3(Y,1)`]) {
    const html = render(String.raw`The group \(` + formula + String.raw`\) and $$\Hom(\CH^3(Y), \colim_n A_n)$$`);
    assert.ok(!html.includes("katex-error") && !html.includes("#cc0000"), formula + " renders without a KaTeX error");
    assert.ok(!shown(html).includes("\\CH"), formula + " shows no raw macro");
    assert.match(shown(html), />CH</, formula + " shows the operator name");
  }
});

test("a message's own \\newcommand for a table macro still renders", () => {
  const html = render(String.raw`\(\newcommand{\CH}{\mathrm{A}} \CH^3(Y)\)`);
  assert.ok(!html.includes("katex-error") && !html.includes("#cc0000"));
  assert.match(shown(html), />A</);
});

test("the macro table only names commands KaTeX lacks and matches the server checker's copy", () => {
  const checker = JSON.parse(readFileSync(new URL("../../src/reasoning_ledger/assets/katex_macros.json", import.meta.url), "utf8"));
  assert.deepEqual(checker, { ...macros.KATEX_MACROS });
  for (const name of Object.keys(macros.KATEX_MACROS)) {
    assert.throws(() => katex.renderToString(name, { throwOnError: true }), /Undefined control sequence/, name + " is not a KaTeX command");
  }
});
