// KaTeX renderability checker. Reads a JSON array of {span, display} on
// stdin and writes a JSON array of {ok, error} — one per input, in order.
// `error` is KaTeX's message for the first parse failure, or null.
// The spans of one call are one text: they share the web renderer's macro
// table (katex_macros.json), less the names the text defines itself with
// \newcommand, so the checker accepts what the chat renders.
//
// Invoked by latex_check.py; katex.min.js sits beside this file (both
// vendored into the package, so this runs in the sandbox where only the
// package source and the system Node are visible).
"use strict";

const path = require("path");
const katex = require(path.join(__dirname, "katex.min.js"));
const MACROS = require(path.join(__dirname, "katex_macros.json"));
const NEWCOMMAND = /\\newcommand\*?\s*\{?\s*(\\[A-Za-z]+)/g;

let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => {
  input += chunk;
});
process.stdin.on("end", () => {
  let spans;
  try {
    spans = JSON.parse(input);
  } catch (e) {
    process.stdout.write(JSON.stringify({ fatal: String(e) }));
    return;
  }
  const macros = { ...MACROS };
  for (const s of spans) {
    for (const [, name] of String(s.span).matchAll(NEWCOMMAND)) delete macros[name];
  }
  const results = spans.map((s) => {
    try {
      katex.renderToString(String(s.span), {
        displayMode: !!s.display,
        throwOnError: true,
        strict: false,
        macros,
      });
      return { ok: true, error: null };
    } catch (e) {
      return { ok: false, error: e && e.message ? String(e.message) : String(e) };
    }
  });
  process.stdout.write(JSON.stringify(results));
});
