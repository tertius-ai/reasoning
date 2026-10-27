// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { createServer } from "vite";

let server, replies;
before(async () => {
  server = await createServer({ appType: "custom", logLevel: "silent", server: { middlewareMode: true } });
  replies = await server.ssrLoadModule("/src/MessageReplies.tsx");
});
after(async () => { await server?.close(); });

const start = Date.UTC(2026, 9, 27, 13, 0) / 1000;
let sequence = 0;
const message = (author, minutes, extra = {}) => ({ id: "m" + (++sequence), sequence, author_id: author, author_kind: "human",
  content: "", created_at: start + minutes * 60, reply_to: "m1", ...extra });
const ids = groups => groups.map(group => group.map(item => item.id));

test("a thread groups consecutive replies from one person, though every reply answers the root", () => {
  sequence = 0;
  const root = message("me", 0, { reply_to: null });
  const model = message("me", 1, { author_kind: "assistant" });
  const first = message("ana", 2), second = message("ana", 3), mine = message("me", 4), again = message("me", 5);
  assert.deepEqual(ids(replies.threadGroups([root, model, first, second, mine, again])), [["m1"], ["m2"], ["m3", "m4"], ["m5", "m6"]]);
});

test("the root stays alone, and a model, a pause or a new day starts a new run", () => {
  sequence = 0;
  const root = message("me", 0, { reply_to: null }), mine = message("me", 1);
  const answer = message("me", 2, { author_kind: "assistant" }), next = message("me", 3, { author_kind: "assistant" });
  const late = message("me", 20), later = message("me", 24 * 60);
  assert.deepEqual(ids(replies.threadGroups([root, mine, answer, next, late, later])), [["m1"], ["m2"], ["m3"], ["m4"], ["m5"], ["m6"]]);
  assert.equal(replies.continuesRun(mine, late), false);
  assert.equal(replies.continuesRun(message("bot", 1, { author_kind: "bot" }), message("bot", 2, { author_kind: "bot" })), true);
});
