import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { createServer } from "vite";

let server, additions;
before(async () => {
  server = await createServer({ appType: "custom", logLevel: "silent", server: { middlewareMode: true } });
  additions = await server.ssrLoadModule("/src/ReplyAdditions.ts");
});
after(async () => { await server?.close(); });

const run = (id, status, sequence, extra = {}) => ({ id, status, context_through_sequence: sequence, model: "moonshotai/kimi-k3",
  requested_by: "me", cancel_requested: 0, additions: [], ...extra });
const added = (message, read_step = null, extra = {}) => ({ message_id: message, run_id: "root", added_by: "me", moved: false,
  cancelled_runs: [], created_at: 1, read_at: read_step === null ? null : 2, read_step, ...extra });

test("a reply and its agent continuations form one thread that runs while any of its runs works", () => {
  const runs = [run("root", "succeeded", 4), run("librarian", "running", 0, { agent: { role: "librarian", root_run: "root" } })];
  const [thread] = additions.replyThreads(runs);
  assert.equal(thread.root.id, "root"); assert.equal(thread.active, true);
  assert.equal(additions.threadAnswering([thread], 4), thread);
  assert.equal(additions.replyThreads([run("root", "succeeded", 4)])[0].active, false);
});

test("a later message moves into the latest running reply, unless its own reply has started", () => {
  const threads = additions.replyThreads([run("old", "running", 2), run("root", "running", 4)]);
  assert.equal(additions.moveTarget(threads, { sequence: 5 }).root.id, "root");
  assert.equal(additions.moveTarget(threads, { sequence: 4 }).root.id, "old");
  assert.equal(additions.moveTarget(threads, { sequence: 2 }), undefined);
  assert.equal(additions.moveBlocked([run("own", "queued", 5)], { sequence: 5 }, "me"), null);
  assert.match(additions.moveBlocked([run("own", "running", 5)], { sequence: 5 }, "me"), /has started/);
});

test("receipts say where an addition went and whether the reply has read it", () => {
  assert.equal(additions.additionReceipt(added("a"), "moonshotai/kimi-k3", true), "Added to kimi-k3's reply · waiting for its next step");
  assert.equal(additions.additionReceipt(added("a", 8), "moonshotai/kimi-k3", true), "Added to kimi-k3's reply · read at step 8");
  assert.equal(additions.additionReceipt(added("a", 0), "moonshotai/kimi-k3", false), "Added to kimi-k3's reply · read when the reply resumed");
  assert.equal(additions.additionReceipt(added("a"), "moonshotai/kimi-k3", false), "Added to kimi-k3's reply · it finished before reading this");
  assert.equal(additions.additionReceipt(added("a", null, { moved: true, cancelled_runs: ["own"] }), "moonshotai/kimi-k3", true),
    "Moved into kimi-k3's reply · waiting for its next step; its own reply was cancelled before it started");
});

const event = (sequence, phase, extra = {}) => ({ sequence, phase, ...extra });

test("the timeline describes each step and places additions where the reply read them", () => {
  const activity = [event(1, "round", { round: 1 }),
    event(2, "tool_requested", { tool: "arxiv_search", detail: "searched arXiv for “regulators”" }),
    event(3, "tool_completed", { tool: "arxiv_search", detail: "searched arXiv for “regulators” · 4 results" }),
    event(4, "additions_read", { step: 2, message_ids: ["a"] }),
    event(5, "round", { round: 2 }), event(6, "tool_completed", { tool: "arxiv_read", detail: "read “Higher regulators”" }),
    event(7, "tool_completed", { tool: "arxiv_read", detail: "read “Beilinson 1985”" }),
    event(8, "tool_completed", { tool: "web_fetch", detail: "fetched arxiv.org" }),
    event(9, "round", { round: 3 }), event(10, "tool_requested", { tool: "web_fetch", detail: "fetched ncatlab.org" }),
    event(11, "answer_ready")];
  const list = [added("a", 2, { content: "Skip French sources." }), added("b", null, { content: "Also cover p-adic regulators." })];
  const entries = additions.replyTimeline(activity, list, true);
  assert.deepEqual(entries.map(entry => entry.kind === "step" ? `${entry.icon}:${entry.label}` : `${entry.waiting ? `waiting@${entry.nextStep}` : "read"}:${entry.addition.content}`), [
    "search:Step 1 · searched arXiv for “regulators” · 4 results", "read:Skip French sources.",
    "read:Step 2 · read “Higher regulators”; read “Beilinson 1985” +1 more",
    "read:Step 3 · fetched ncatlab.org · writing the answer", "waiting@4:Also cover p-adic regulators.",
  ]);
  assert.deepEqual(entries.filter(entry => entry.kind === "step").map(entry => entry.current), [false, false, true]);
  const finished = additions.replyTimeline(activity, list, false);
  assert.equal(finished.some(entry => entry.kind === "addition" && entry.waiting), false, "a finished reply lists no waiting additions");
  assert.equal(finished.some(entry => entry.kind === "step" && entry.current), false);
  const record = additions.replyTimeline([event(1, "round", { round: 1 })], [added("c", 1, { content: "Read by another run." })], false, true);
  assert.equal(record.at(-1).addition.content, "Read by another run.", "the folded record also lists additions read by other runs");
});

test("an added message leaves the transcript only while its reply runs or once an answer lists it", () => {
  const folded = new Set(["a"]);
  assert.equal(additions.shownInReply(undefined, true, folded), false);
  assert.equal(additions.shownInReply(added("b"), true, folded), true, "shown in the running reply's timeline");
  assert.equal(additions.shownInReply(added("a", 2), false, folded), true, "folded under the answer that read it");
  assert.equal(additions.shownInReply(added("c", 2), false, folded), false, "a reply that failed after reading it has no answer");
  assert.equal(additions.shownInReply(added("d"), false, folded), false, "a reply that finished before reading it");
});

