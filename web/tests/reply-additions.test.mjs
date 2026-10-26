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

test("the thread line and receipts say what was added and what the reply has read", () => {
  assert.equal(additions.additionsSummary([]), "Add to this reply");
  assert.equal(additions.additionsSummary([added("a", 3), added("b")]), "2 additions · 1 read · 1 waiting");
  assert.equal(additions.additionReceipt(added("a"), "moonshotai/kimi-k3", true), "Added to kimi-k3's reply · waiting for its next step");
  assert.equal(additions.additionReceipt(added("a", 8), "moonshotai/kimi-k3", true), "Added to kimi-k3's reply · read at step 8");
  assert.equal(additions.additionReceipt(added("a", 0), "moonshotai/kimi-k3", false), "Added to kimi-k3's reply · read when the reply resumed");
  assert.equal(additions.additionReceipt(added("a"), "moonshotai/kimi-k3", false), "Added to kimi-k3's reply · it finished before reading this");
  assert.equal(additions.additionReceipt(added("a", null, { moved: true, cancelled_runs: ["own"] }), "moonshotai/kimi-k3", true),
    "Moved into kimi-k3's reply · waiting for its next step; its own reply was cancelled before it started");
});
