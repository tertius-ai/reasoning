// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
/**
 * A model reply folds to one sentence. The reply prompt asks the model to begin with a line `Summary: <one sentence>`;
 * the stored message keeps that line, and the reply is split here, the same way while it streams and once it is saved.
 * Without the line, an assistant reply folds to its first sentence instead, marked as such.
 */

/** A reply split for display: the sentence it folds to, the rest, and whether folding hides anything worth a click. */
export type ReplyParts = {
  /** The model's summary, or the answer's first sentence when `fallback`; null when the reply does not fold. */
  summary: string | null;
  /** The answer without the summary line. */
  body: string;
  /** The summary is the answer's first sentence because the model wrote no summary line. */
  fallback: boolean;
  /** Folding hides enough to need "Show full answer"; a short reply shows whole. */
  fold: boolean;
};

const LINE = /^(?:\*\*|__)?summary(?:\*\*|__)?\s*:(?:\*\*|__)?[ \t]*/i;
const DISPLAY = /\$\$|\\\[/;
/** A reply this short (one paragraph, no display math) is shown whole. */
const SHORT = 300;
/** A summary longer than this is cut to its first sentence. */
const LONG = 400;
const EXCERPT = 200;
/** A line that starts a list item, quote, table, heading or code block rather than a paragraph. */
const BLOCK = /^\s*(?:[-*+]\s|>|\||#{1,6}\s|\d+[.)]\s|```)/;
const ABBREVIATIONS = /(?:^|[\s(])(?:e\.g|i\.e|cf|resp|vs|etc|al|Thm|Prop|Lem|Cor|Def|Rem|Ex|Fig|Sec|Eq|Ch|No|Vol|pp|ibid)\.$/;

/** `text` without the "Summary:" label of its first line, for excerpts that quote a reply from its start. */
export const withoutSummaryLabel = (text: string) => text.trimStart().replace(LINE, "");

/** The summary written in the first line of `text`, if that line is one; `rest` is what follows it. */
function summaryLine(text: string): { summary: string; rest: string } | null {
  const start = text.trimStart(), end = start.indexOf("\n");
  const line = end < 0 ? start : start.slice(0, end), marker = LINE.exec(line);
  if (!marker) return null;
  let summary = line.slice(marker[0].length).trim();
  // "**Summary: …**" bolds the whole line; drop the closing marker left without its opening one.
  const unmatched = (bold: string) => summary.split(bold).length % 2 === 0;
  if (unmatched("**")) summary = summary.replace(/\*\*\s*$/, "").trim();
  if (unmatched("__")) summary = summary.replace(/__\s*$/, "").trim();
  return summary ? { summary, rest: end < 0 ? "" : start.slice(end + 1).replace(/^\s*\n/, "") } : null;
}

/** Whether `text` may still become a summary line once more of it streams in. */
function summaryPrefix(text: string): boolean {
  const probe = text.trimStart().replace(/^(\*\*|__)/, "").toLowerCase();
  return "summary".startsWith(probe) || LINE.test(text.trimStart());
}

/**
 * The first sentence of `paragraph`: up to the first `.`, `?` or `!` followed by a space, outside math and code, that
 * does not end a common abbreviation; at most about 200 characters. Null when nothing precedes such an end.
 */
export function firstSentence(paragraph: string): string | null {
  const text = paragraph.replace(/\s+/g, " ").trim();
  let math: "" | "$" | "$$" | "\\(" | "\\[" | "`" = "";
  for (let i = 0; i < text.length; i++) {
    const c = text[i], next = text[i + 1];
    if (c === "\\" && math !== "`") {
      if (!math && (next === "(" || next === "[")) math = next === "(" ? "\\(" : "\\[";
      else if ((math === "\\(" && next === ")") || (math === "\\[" && next === "]")) math = "";
      i++;
      continue;
    }
    if (c === "`" && (!math || math === "`")) { math = math ? "" : "`"; continue; }
    if (c === "$" && (!math || math === "$" || math === "$$")) {
      if (next === "$") { math = math === "$$" ? "" : math ? math : "$$"; i++; } else if (math !== "$$") math = math ? "" : "$";
      continue;
    }
    if (math || !".?!".includes(c) || (i + 1 < text.length && next !== " ")) continue;
    const head = text.slice(0, i + 1);
    if (c === "." && ABBREVIATIONS.test(head)) continue;
    return head.length <= EXCERPT + 40 ? head : cut(text);
  }
  return text.length > EXCERPT ? cut(text) : null;
}

/** About 200 characters of `text`, cut at a space and never inside inline math. */
function cut(text: string): string {
  let head = text.slice(0, EXCERPT);
  const space = head.lastIndexOf(" ");
  if (space > EXCERPT / 2) head = head.slice(0, space);
  if ((head.match(/(?<!\\)\$/g) ?? []).length % 2) head = head.slice(0, head.lastIndexOf("$"));
  return head.replace(/[\s,;:]+$/, "") + "…";
}

/** Whether `body` is short enough to show whole: one paragraph of plain text, no display math, lists or tables. */
function short(body: string): boolean {
  const text = body.trim();
  return text.length <= SHORT && !text.includes("\n\n") && !DISPLAY.test(text) && !text.split("\n").some(line => BLOCK.test(line));
}

/** Whether the first block of `body` is a plain paragraph, so that its first sentence can stand for the reply. */
function plainStart(body: string): boolean {
  return !BLOCK.test(body) && !/^\s*(?:\$\$|\\\[)/.test(body);
}

/**
 * Split a saved reply. `fallback` lets a reply without a summary line fold to its first sentence (model replies);
 * without it such a reply never folds (bot replies, which fold only when they write the line).
 */
export function replyParts(content: string, { fallback = true }: { fallback?: boolean } = {}): ReplyParts {
  const line = summaryLine(content);
  if (line) {
    let summary = line.summary;
    if (summary.length > LONG || DISPLAY.test(summary)) summary = firstSentence(summary) ?? summary;
    return { summary, body: line.rest, fallback: false, fold: !short(line.rest) };
  }
  const body = content.trim();
  if (!fallback || short(body) || !plainStart(body)) return { summary: null, body: content, fallback: false, fold: false };
  const sentence = firstSentence(body.split(/\n\s*\n/)[0]);
  return sentence ? { summary: sentence, body: content, fallback: true, fold: true } : { summary: null, body: content, fallback: false, fold: false };
}

/** A streaming reply's text so far: its summary once the first line is complete, and the text after it. */
export function streamParts(text: string): { summary: string | null; body: string; pending: boolean } {
  const start = text.trimStart();
  if (!start.includes("\n")) return summaryPrefix(start) ? { summary: null, body: "", pending: true } : { summary: null, body: text, pending: false };
  const line = summaryLine(start);
  return line ? { summary: line.summary, body: line.rest, pending: false } : { summary: null, body: text, pending: false };
}

/** "190 words · 1 display · 1 table": how much a folded answer holds. */
export function bodySize(body: string): string {
  const displays = (body.match(/\$\$[\s\S]*?\$\$|\\\[[\s\S]*?\\\]/g) ?? []).length;
  const tables = (body.match(/^\s*\|?\s*:?-{3,}:?\s*\|/gm) ?? []).length;
  const words = body.replace(/\$\$[\s\S]*?\$\$|\\\[[\s\S]*?\\\]/g, " ").match(/\S+/g)?.length ?? 0;
  const count = (n: number, one: string, many = one + "s") => n ? `${n} ${n === 1 ? one : many}` : "";
  return [count(words, "word"), count(displays, "display"), count(tables, "table")].filter(Boolean).join(" · ");
}
