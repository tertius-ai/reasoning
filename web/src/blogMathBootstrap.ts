import renderMathInElement from "katex/contrib/auto-render";
import "katex/dist/katex.min.css";

// The static blog is readable without JavaScript; this progressively enhances
// its TeX. Never render author-provided HTML or enable trusted TeX commands.
if (document.body.hasAttribute("data-research-blog")) renderMathInElement(document.querySelector("main") ?? document.body, {
  delimiters: [{ left: "$$", right: "$$", display: true }, { left: "\\[", right: "\\]", display: true },
    { left: "$", right: "$", display: false }, { left: "\\(", right: "\\)", display: false }],
  throwOnError: false, trust: false, maxExpand: 1000, maxSize: 20,
});
