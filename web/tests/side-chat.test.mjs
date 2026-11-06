// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

let server, side, group;
before(async () => {
  server = await createServer({ appType: "custom", logLevel: "silent", server: { middlewareMode: true } });
  side = await server.ssrLoadModule("/src/SideChat.tsx");
  group = await server.ssrLoadModule("/src/SideChatGroup.tsx");
});
after(async () => { await server?.close(); });

let sequence = 0;
const message = (author_kind, content, reply_to = null) => ({ id: "s" + (++sequence), sequence, topic_id: "t", owner_id: "me", author_kind,
  content, client_id: "c" + sequence, created_at: 1_800_000_000 + sequence, assistant_model: author_kind === "assistant" ? "fixture/reasoner" : null,
  inference_run_id: author_kind === "assistant" ? "run-" + sequence : null, reply_to });
const run = (question_id, status) => ({ id: "run-" + question_id, model: "fixture/reasoner", status, created_at: 1_800_000_000,
  side_chat: { question_id } });

test("a reply builds on every earlier side message, and its exchange is its question and itself", () => {
  sequence = 0;
  const first = message("human", "Is the Borel regulator injective?"), firstAnswer = message("assistant", "Yes, by Borel.", first.id);
  const second = message("human", "And the motivic version?"), secondAnswer = message("assistant", "It follows from the Borel case.", second.id);
  const messages = [first, firstAnswer, second, secondAnswer];
  assert.deepEqual(side.earlierMessages(messages, secondAnswer).map(item => item.id), [first.id, firstAnswer.id]);
  assert.deepEqual(side.exchange(messages, secondAnswer).map(item => item.id), [second.id, secondAnswer.id]);
  assert.deepEqual(side.earlierMessages(messages, firstAnswer), []);
  // An answer whose question is out of view stands alone.
  assert.deepEqual(side.exchange([secondAnswer], secondAnswer).map(item => item.id), [secondAnswer.id]);
});

test("merging side messages keeps order and replaces a message by its id", () => {
  sequence = 0;
  const first = message("human", "one"), second = message("assistant", "two", first.id);
  const merged = side.mergeSide([second], [first, { ...second, content: "two, revised" }]);
  assert.deepEqual(merged.map(item => [item.id, item.content]), [[first.id, "one"], [second.id, "two, revised"]]);
  const existing = [first];
  assert.equal(side.mergeSide(existing, []), existing, "an empty delta keeps the same array");
});

test("an unanswered question shows its reply at work, a reply that ended, or that none was asked", () => {
  sequence = 0;
  const asked = message("human", "Asked"), answered = message("human", "Answered"), answer = message("assistant", "Done", answered.id);
  const messages = [asked, answered, answer];
  assert.equal(side.pending(messages, [run(asked.id, "running")], asked).state, "working");
  assert.equal(side.pending(messages, [run(asked.id, "failed")], asked).state, "ended");
  assert.equal(side.pending(messages, [], asked).state, "none");
  assert.equal(side.pending(messages, [], answered), null, "an answered question needs nothing");
  assert.equal(side.pending(messages, [], answer), null, "an answer is not a question");
});

test("a brought group folds to one line per message and names whose side chat it came from", () => {
  const brought = { owner_id: "me", omitted: 2, omitted_statements: [{ claim_id: "clm_0123456789abcdef", statement_text: "The Borel regulator is injective." }],
    messages: [
      { id: "q", author_kind: "human", assistant_model: null, content: "And the **motivic** version?", created_at: 1, statements: [] },
      { id: "a", author_kind: "assistant", assistant_model: "fixture/reasoner", content: "It follows.", created_at: 2,
        statements: [{ claim_id: "clm_fedcba9876543210", statement_text: "The motivic regulator is injective." }] },
    ] };
  const render = own => renderToStaticMarkup(React.createElement(group.SideChatGroup, { group: brought, owner: "Elden Ring", own,
    renderMarkdown: text => React.createElement("p", null, text) }));
  const folded = render(false);
  assert.match(folded, /From Elden Ring&#x27;s side chat · 2 messages/);
  assert.match(folded, /aria-expanded="false"/);
  assert.match(folded, /Elden Ring<\/strong> And the motivic version\?/, "the preview is a plain excerpt");
  assert.match(folded, /reasoner<\/strong> It follows\./);
  assert.match(folded, /Builds on 2 earlier side messages that\s+stayed private, which recorded:/);
  assert.match(folded, /clm_01234567/);
  assert.doesNotMatch(folded, /clm_fedcba98/, "a reply's own statements show once the group is opened");
  assert.match(render(true), /From your side chat · 2 messages/);
  assert.match(render(true), /You<\/strong> And the motivic version\?/);
});
