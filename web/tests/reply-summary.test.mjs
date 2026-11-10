import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { createServer } from "vite";

let server, summary;
before(async () => {
  server = await createServer({ appType: "custom", logLevel: "silent", server: { middlewareMode: true } });
  summary = await server.ssrLoadModule("/src/replySummary.ts");
});
after(async () => { await server?.close(); });

const answer = [
  "**Yes.** Let $R$ be a finite integral domain and let $a \\in R$ be nonzero. Define",
  "",
  "$$\\mu_a \\colon R \\to R, \\qquad x \\mapsto ax.$$",
  "",
  "If $ax = ay$, then $a(x-y) = 0$, so $x = y$. An injective map from a finite set to itself is surjective.",
].join("\n");

test("a summary line folds the reply to it and leaves the answer after it", () => {
  const parts = summary.replyParts("Summary: Yes: multiplication by $a \\neq 0$ is injective, hence bijective.\n\n" + answer);
  assert.equal(parts.summary, "Yes: multiplication by $a \\neq 0$ is injective, hence bijective.");
  assert.equal(parts.body, answer);
  assert.equal(parts.fallback, false);
  assert.equal(parts.fold, true);
});

test("bold and spacing variants of the summary line are recognized", () => {
  for (const line of ["**Summary:** Yes.", "**Summary**: Yes.", "**Summary: Yes.**", "summary:   Yes.", "  \nSummary: Yes."]) {
    assert.equal(summary.replyParts(line + "\n\n" + answer).summary, "Yes.", line);
  }
  assert.equal(summary.replyParts("**Summary:** Yes, **finite** rings.\n\n" + answer).summary, "Yes, **finite** rings.");
  const plain = summary.replyParts("Summary of the argument follows.\n\n" + answer);
  assert.deepEqual([plain.summary, plain.fallback], ["Summary of the argument follows.", true]);
});

test("a reply without the line folds to its first sentence, outside math and abbreviations", () => {
  const parts = summary.replyParts(answer);
  assert.equal(parts.fallback, true);
  assert.equal(parts.summary, "**Yes.** Let $R$ be a finite integral domain and let $a \\in R$ be nonzero.");
  assert.equal(parts.body, answer);
  assert.equal(summary.firstSentence("For $x = 1.5$ the bound holds, e.g. by Thm. 2. The rest follows."),
    "For $x = 1.5$ the bound holds, e.g. by Thm. 2.");
  assert.equal(summary.firstSentence("Is $\\mathbb{F}_4 \\cong \\mathbb{Z}/4$? No."), "Is $\\mathbb{F}_4 \\cong \\mathbb{Z}/4$?");
});

test("an unending first sentence is cut near 200 characters, never inside math", () => {
  const long = "word ".repeat(30) + "$a + b + c + d + e + f + g$ " + "more ".repeat(40);
  const cut = summary.firstSentence(long);
  assert.ok(cut.endsWith("…") && cut.length <= 202, cut);
  assert.equal((cut.match(/\$/g) ?? []).length % 2, 0);
});

test("short replies, lists and bot replies without the line stay whole", () => {
  assert.equal(summary.replyParts("Yes. It is a field because it is a finite domain.").fold, false);
  assert.equal(summary.replyParts("- one\n- two\n\n" + answer).fold, false);
  assert.equal(summary.replyParts(answer, { fallback: false }).fold, false);
  const shortWithLine = summary.replyParts("Summary: Yes.\n\nIt is a finite domain.");
  assert.deepEqual([shortWithLine.summary, shortWithLine.fold], ["Yes.", false]);
});

test("an overlong summary or one with display math is cut to its first sentence", () => {
  const long = "Summary: " + "The quotient is a field. " + "x ".repeat(250) + "\n\n" + answer;
  assert.equal(summary.replyParts(long).summary, "The quotient is a field.");
  assert.equal(summary.replyParts("Summary: It holds. $$x$$\n\n" + answer).summary, "It holds.");
});

test("a streaming reply shows its summary once the first line is complete", () => {
  assert.deepEqual(summary.streamParts("Summ"), { summary: null, body: "", pending: true });
  assert.deepEqual(summary.streamParts("**Summary:** Yes: for a nonzero"), { summary: null, body: "", pending: true });
  assert.deepEqual(summary.streamParts("Summary: Yes.\n\n**Yes, directly.** Let"), { summary: "Yes.", body: "**Yes, directly.** Let", pending: false });
  assert.deepEqual(summary.streamParts("The quotient is"), { summary: null, body: "The quotient is", pending: false });
});

test("the folded size counts words, displays and tables", () => {
  assert.equal(summary.bodySize(answer), "39 words · 1 display");
  assert.equal(summary.bodySize("| p | q |\n|---|---|\n| 2 | 3 |"), "11 words · 1 table");
});
