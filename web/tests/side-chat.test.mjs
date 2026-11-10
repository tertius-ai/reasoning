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

test("the exchanges a reply read come newest first, each with the post that brought it", () => {
  sequence = 0;
  const first = message("human", "Which regulator?"), firstAnswer = message("assistant", "Beilinson's.", first.id);
  const second = { ...message("human", "Injective?"), brought: { message_id: "post", created_at: 5 } };
  const secondAnswer = message("assistant", "Yes.", second.id);
  const third = message("human", "Motivic?"), thirdAnswer = message("assistant", "It follows.", third.id);
  const listed = side.earlierExchanges([first, firstAnswer, second, secondAnswer, third, thirdAnswer], thirdAnswer);
  assert.deepEqual(listed.map(item => [item.question?.id, item.reply.id, item.brought?.message_id ?? null]),
    [[second.id, secondAnswer.id, "post"], [first.id, firstAnswer.id, null]], "a brought question brings its exchange");
  assert.deepEqual(side.earlierExchanges([first, firstAnswer], firstAnswer), []);
});

test("side messages group by speaker, and new speakers take colors after the conversation's", () => {
  sequence = 0;
  const asked = message("human", "One"), again = message("human", "Two");
  const first = message("assistant", "Three", asked.id), second = message("assistant", "Four", again.id);
  assert.deepEqual(side.sideTranscript([asked, again, first, second], [], "fixture/reasoner").map(group => group.map(item => item.id)),
    [[asked.id, again.id], [first.id, second.id]]);
  const tones = side.sideTones(new Map([["me", 0], ["model:other", 1]]), [asked, first]);
  assert.deepEqual([...tones], [["me", 0], ["model:other", 1], ["model:fixture/reasoner", 2]], "the conversation's colors stay put");
});

test("a reply that has not come waits after its question, on the side of the model that is to give it, until the reply comes", () => {
  sequence = 0;
  const asked = message("human", "One"), again = message("human", "Two"), answer = message("assistant", "Three", again.id);
  const shape = items => items.map(item => Array.isArray(item) ? item.map(entry => entry.id) : [item.state, item.question.id, item.model]);
  assert.deepEqual(shape(side.sideTranscript([asked, again, answer], [run(asked.id, "running")], "chosen/model")),
    [[asked.id], ["working", asked.id, "fixture/reasoner"], [again.id], [answer.id]], "it parts your messages; the model at work is named");
  assert.deepEqual(shape(side.sideTranscript([asked], [], "chosen/model")), [[asked.id], ["none", asked.id, "chosen/model"]],
    "with no reply asked for, the model named is the one Ask again asks");
  const replied = message("assistant", "Done", asked.id);
  assert.deepEqual(shape(side.sideTranscript([asked, replied], [run(asked.id, "succeeded")], "chosen/model")), [[asked.id], [replied.id]]);
});

test("a message added to a side reply shows in its steps while it works and folds under the answer that read it", () => {
  sequence = 0;
  const asked = message("human", "One"), working = run(asked.id, "running");
  const added = { ...message("human", "Also this"), addition: { run_id: working.id, read_at: null, read_step: null } };
  const ids = items => items.map(item => Array.isArray(item) ? item.map(entry => entry.id) : ["waiting", item.question.id]);
  assert.deepEqual(ids(side.sideTranscript([asked, added], [working], "chosen/model")), [[asked.id], ["waiting", asked.id]],
    "while the reply works, it is not a question of its own");
  const listed = { ...working, additions: [{ message_id: "elsewhere", run_id: working.id, added_by: "me", moved: false, cancelled_runs: [],
    created_at: 1, read_at: 2, read_step: 2, content: "Earlier" }] };
  assert.deepEqual(side.sideAdditions(listed, [asked, added]).map(item => [item.message_id, item.read_step, item.content]),
    [["elsewhere", 2, "Earlier"], [added.id, null, "Also this"]], "the run's list, then what was posted since");
  const answer = { ...message("assistant", "Done", asked.id), inference_run_id: working.id,
    additions: [{ ...listed.additions[0], message_id: added.id }] };
  assert.deepEqual(ids(side.sideTranscript([asked, added, answer], [run(asked.id, "succeeded")], "chosen/model")), [[asked.id], [answer.id]],
    "the answer that read it holds it");
  assert.deepEqual(side.exchange([asked, added, answer], answer).map(item => item.id), [asked.id, added.id, answer.id],
    "bringing the exchange brings it too");
  const unread = { ...answer, additions: [] };
  const finished = side.sideTranscript([asked, added, unread], [run(asked.id, "succeeded")], "chosen/model");
  assert.deepEqual(ids(finished), [[asked.id], [unread.id], [added.id], ["waiting", added.id]],
    "an answer that did not read it comes first; it follows as a question");
  assert.equal(finished.at(-1).model, "fixture/reasoner", "its note is named for the reply that did not read it");
  assert.deepEqual(ids(side.sideTranscript([asked, { ...added, addition: null }], [working], "chosen/model")),
    [[asked.id], ["waiting", asked.id], [added.id], ["waiting", added.id]], "taken back, it is a message of its own");
});

test("bringing offers to answer the newest message from someone else since your own last one", () => {
  const post = (id, author_id, author_kind = "human") => ({ id, author_id, author_kind, content: "Message " + id });
  const name = item => item.author_id === "deven" ? "Deven" : "Someone";
  assert.deepEqual(group.answerTarget([post("1", "me"), post("2", "deven")], "me", name), { id: "2", name: "Deven", excerpt: "Message 2" });
  assert.equal(group.answerTarget([post("1", "deven"), post("2", "me")], "me", name), undefined, "you spoke last");
  assert.equal(group.answerTarget([post("1", "deven"), post("2", "me", "assistant")], "me", name).id, "1", "model replies are skipped");
  assert.equal(group.answerTarget([], "me", name), undefined);
});

const broughtGroup = (change = {}) => ({ owner_id: "me", words: "Here is the *motivic* case.", omitted: 2,
  linked: [{ message_id: "p1", created_at: 1_800_000_000 }], messages: [
    { id: "q", author_kind: "human", assistant_model: null, content: "And the **motivic** version?", created_at: 1_800_000_100 },
    { id: "a", author_kind: "assistant", assistant_model: "fixture/reasoner", content: "It follows.", created_at: 1_800_000_160 },
  ], ...change });

test("a brought post's card is titled by its first question and says whose side chat it came from, with no teaser", () => {
  const render = (own, change = {}, open = false) => renderToStaticMarkup(React.createElement(group.SideChatCard, { group: broughtGroup(change),
    owner: "Elden Ring", own, open, organizationId: "o", topicId: "t", onOpen: () => {} }));
  const theirs = render(false);
  assert.match(theirs, /<strong>And the motivic version\?<\/strong><small>Elden Ring&#x27;s side chat · 1 exchange · reasoner<\/small>/);
  assert.match(theirs, /aria-expanded="false"/);
  assert.match(theirs, /Builds on <a class="org-side-group-link" href="\/organization.html\?organization=o&amp;topic=t&amp;message=p1">Elden Ring&#x27;s post at [^<]+<\/a> and 2 side messages that stay private\./);
  assert.doesNotMatch(theirs, /It follows|clm_/, "no answer teaser and no ledger codes");
  const mine = render(true, { linked: [], omitted: 0 }, true);
  assert.match(mine, /Your side chat · 1 exchange · reasoner/);
  assert.match(mine, /data-open="true"/);
  assert.match(mine, /aria-expanded="true"/);
  assert.doesNotMatch(mine, /Builds on/);
  assert.equal(group.groupTitle({ messages: [{ author_kind: "assistant", content: "Only **an** answer" }] }), "Only an answer");
});

test("the panel shows the exchange in the side chat's style and offers to continue yours or ask about someone else's", () => {
  const tones = new Map([["me", 3], ["model:fixture/reasoner", 5]]);
  const render = (own, canAsk = true) => renderToStaticMarkup(React.createElement(group.BroughtPanel, {
    post: { id: "post", created_at: 1_800_000_200, side_chat: broughtGroup() }, owner: "Elden Ring", own, answers: "Deven",
    organizationId: "o", topicId: "t", tones, canAsk, renderMarkdown: text => React.createElement("p", null, text),
    onClose: () => {}, onContinue: () => {}, onAsk: () => {} }));
  const theirs = render(false);
  assert.match(theirs, /<h2>From Elden Ring&#x27;s side chat<\/h2><small>Brought [^<]+ · answers Deven<\/small>/);
  assert.match(theirs, /class="org-message-group org-speaker-human org-person-tone-3">/, "someone else's lines stay on the left");
  assert.match(theirs, /class="org-message-group org-speaker-assistant org-person-tone-5">/);
  assert.match(theirs, /Builds on <a[^>]*>Elden Ring&#x27;s post at /);
  assert.match(theirs, />Ask privately about this</);
  assert.doesNotMatch(theirs, /Continue in side chat/);
  const mine = render(true);
  assert.match(mine, /class="org-message-group org-speaker-human org-person-tone-3 org-message-group-mine">/, "your lines sit on the right");
  assert.match(mine, />You</);
  assert.match(mine, />Continue in side chat</);
  assert.doesNotMatch(render(false, false), /Ask privately/, "viewers cannot ask");
});
