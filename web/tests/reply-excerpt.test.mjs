// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { createServer } from "vite";

let server, replies, math;
before(async () => {
  server = await createServer({ appType: "custom", logLevel: "silent", server: { middlewareMode: true } });
  replies = await server.ssrLoadModule("/src/MessageReplies.tsx");
  math = await server.ssrLoadModule("/src/plainMath.ts");
});
after(async () => { await server?.close(); });

const ada = "a".repeat(32), topic = "b".repeat(32);

test("a model answer's summary quotes its math as readable text", () => {
  const answer = "Summary: $\\chi\\big(\\mathrm{Gr}(2,5)\\big) = 10$, and the Euler characteristic counts the Schubert cells.\n\n"
    + "The Grassmannian $\\mathrm{Gr}(2,5)$ has $\\binom{5}{2}$ Schubert cells.";
  assert.equal(replies.replyExcerpt(answer), "χ(Gr(2,5)) = 10, and the Euler characteristic counts the Schubert cells. The Grassmannian…");
});

test("Greek letters, operators and subscripts read in one line", () => {
  const message = "I'd also fix $\\sigma_1$ for the hyperplane class, so Pieri reads $\\sigma_1 \\cdot \\sigma_\\lambda = \\sum \\sigma_\\mu$.";
  assert.equal(replies.replyExcerpt(message), "I'd also fix σ₁ for the hyperplane class, so Pieri reads σ₁ · σ_λ = ∑ σ_μ.");
  assert.equal(replies.replyExcerpt(message, 60), "I'd also fix σ₁ for the hyperplane class, so Pieri reads σ₁…");
});

test("mentions and references keep their names beside math", () => {
  const message = `@[${ada}:Ada Lovelace] is $\\binom{5}{2} = 10$ right? See #[topic:${topic}] and @[${ada}].`;
  assert.equal(replies.replyExcerpt(message), "@Ada Lovelace is C(5,2) = 10 right? See topic and @member.");
});

test("a message without math reads as before", () => {
  assert.equal(replies.replyExcerpt("**Bold** and _italic_ and `a_b`\n\n> quoted\n- item (one)"), "Bold and italic and ab quoted - item one");
  assert.equal(replies.replyExcerpt("See ![plot](/api/media/plot.png) and [notes](https://example.com/a)"),
    "See 📎 plot and noteshttps://example.com/a");
  assert.equal(replies.replyExcerpt("word ".repeat(40)), "word ".repeat(18).trim() + "…");
  assert.equal(replies.replyExcerpt("It costs $5 and $10, or \\$7."), "It costs 5 and 10, or 7.");
  // A cut never splits a character written as a surrogate pair.
  assert.equal(replies.replyExcerpt("🙂".repeat(50)), "🙂".repeat(44) + "…");
});

test("every math delimiter the renderer accepts is converted", () => {
  assert.equal(replies.replyExcerpt("Inline \\(x \\in \\mathbb{R}^n\\) and display\n\n$$\n\\int_0^1 f(x)\\,dx \\le \\frac{n+1}{2}\n$$\n\ndone"),
    "Inline x ∈ ℝⁿ and display ∫₀¹ f(x) dx ≤ (n+1)/2 done");
  assert.equal(replies.replyExcerpt("Then \\[\\mathcal{O}_{X,x} \\to \\hat{\\mathcal{O}}\\] holds."), "Then 𝒪_(X,x) → 𝒪̂ holds.");
  assert.equal(replies.replyExcerpt("Run `echo $HOME` with $p$ odd."), "Run echo HOME with p odd.");
});

test("TeX commands become Unicode text", () => {
  const cases = [
    ["\\binom{n}{k} \\le 2^{n}", "C(n,k) ≤ 2ⁿ"],
    ["\\frac12 + \\frac{a}{b+c}", "1/2 + a/(b+c)"],
    ["\\sqrt{x+1} = \\sqrt[3]{y}", "√(x+1) = ∛y"],
    ["\\mathbb{F}_p \\subset \\overline{\\mathbb{F}}_p", "𝔽ₚ ⊂ 𝔽̅ₚ"],
    ["\\log_2 n", "log₂ n"],
    ["\\sin^2\\theta + \\cos^2\\theta = 1", "sin² θ + cos² θ = 1"],
    ["\\lim_{n\\to\\infty} a_n", "lim_(n→∞) aₙ"],
    ["\\Hom_R(M,N) \\cong \\operatorname{Spec} A", "Hom_R(M,N) ≅ Spec A"],
    ["\\left( \\frac{a}{b} \\right)", "(a/b)"],
    ["\\Bigl\\{ x \\Bigr\\}", "{x}"],
    ["H^{2k}(X;\\mathbb{Z})", "H²ᵏ(X;ℤ)"],
    ["f'(x)^{-1}, M^T, V^\\vee, 90^\\circ, e^{2\\pi i}", "f′(x)⁻¹, Mᵀ, V^∨, 90°, e^(2πi)"],
    ["a \\not= b, x \\not\\in A, \\mathfrak{g}", "a ≠ b, x ∉ A, 𝔤"],
    ["\\begin{pmatrix} a & b \\\\ c & d \\end{pmatrix}", "(a b; c d)"],
    ["|x| = \\begin{cases} x & \\text{if } x > 0 \\\\ -x & \\text{otherwise} \\end{cases}", "|x| = {x if x > 0; -x otherwise"],
    ["A \\xrightarrow{f} B", "A -f→ B"],
    ["\\constructor \\toString", "constructor toString"],
  ];
  for (const [tex, text] of cases) assert.equal(math.plainMath(tex), text, tex);
});
