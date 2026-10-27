import renderMathInElement from "katex/contrib/auto-render";
import "katex/dist/katex.min.css";
import { messageMacros } from "./katexMacros";

// The static blog is readable without JavaScript; this progressively enhances
// its TeX. Never render author-provided HTML or enable trusted TeX commands.
const root = document.querySelector("main") ?? document.body;
if (document.body.hasAttribute("data-research-blog")) renderMathInElement(root, {
  delimiters: [{ left: "$$", right: "$$", display: true }, { left: "\\[", right: "\\]", display: true },
    { left: "$", right: "$", display: false }, { left: "\\(", right: "\\)", display: false }],
  throwOnError: false, trust: false, maxExpand: 1000, maxSize: 20, macros: messageMacros(root.textContent ?? ""),
});
