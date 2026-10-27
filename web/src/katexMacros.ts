// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
/** Operator names that papers define as macros (`\CH` for `\operatorname{CH}`) and KaTeX does not know. Each
 * expands to its own name set upright, so the table adds no meaning of its own; ambiguous names such as `\RR`
 * or `\HH` stay undefined. The server's checker reads the same table from
 * `src/reasoning_ledger/assets/katex_macros.json`, and a test keeps the two equal. This module imports nothing:
 * the static blog's math enhancement loads it beside KaTeX, without the Markdown renderer. */
export const KATEX_MACROS: Readonly<Record<string, string>> = Object.freeze({
  "\\CH": "\\operatorname{CH}", "\\Pic": "\\operatorname{Pic}", "\\Br": "\\operatorname{Br}",
  "\\NS": "\\operatorname{NS}", "\\Alb": "\\operatorname{Alb}", "\\Spec": "\\operatorname{Spec}",
  "\\Proj": "\\operatorname{Proj}", "\\Spf": "\\operatorname{Spf}", "\\Hom": "\\operatorname{Hom}",
  "\\End": "\\operatorname{End}", "\\Aut": "\\operatorname{Aut}", "\\Ext": "\\operatorname{Ext}",
  "\\Tor": "\\operatorname{Tor}", "\\Gal": "\\operatorname{Gal}", "\\Sym": "\\operatorname{Sym}",
  "\\coker": "\\operatorname{coker}", "\\codim": "\\operatorname{codim}", "\\Supp": "\\operatorname{Supp}",
  "\\Map": "\\operatorname{Map}", "\\Fun": "\\operatorname{Fun}", "\\Tot": "\\operatorname{Tot}",
  "\\Perf": "\\operatorname{Perf}", "\\Coh": "\\operatorname{Coh}", "\\QCoh": "\\operatorname{QCoh}",
  "\\THH": "\\operatorname{THH}", "\\TC": "\\operatorname{TC}", "\\TP": "\\operatorname{TP}",
  "\\colim": "\\operatorname*{colim}", "\\holim": "\\operatorname*{holim}", "\\hocolim": "\\operatorname*{hocolim}",
});

const NEWCOMMAND = /\\newcommand\*?\s*\{?\s*(\\[A-Za-z]+)/g;

/** The macros for one message: a fresh copy, so a `\gdef` lasts only for that message, without the names the
 * message defines itself with `\newcommand` (which refuses to redefine a known command). */
export function messageMacros(source: string): Record<string, string> {
  const macros = { ...KATEX_MACROS };
  for (const [, name] of source.matchAll(NEWCOMMAND)) delete macros[name];
  return macros;
}

