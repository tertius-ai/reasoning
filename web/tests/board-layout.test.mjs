// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { createServer } from "vite";

let server, layout;
before(async () => {
  server = await createServer({ appType: "custom", logLevel: "silent", server: { middlewareMode: true } });
  layout = await server.ssrLoadModule("/src/boardLayout.ts");
});
after(async () => { await server?.close(); });

const card = (id, extra = {}) => ({ id, kind: "idea", text: id, author_kind: "human", created_by: "me", created_at: id.charCodeAt(0),
  updated_at: 0, rank: null, maturity: null, theme: null, promise: null, effort: null, reason: null, statements: [], board: null, ...extra });
const link = (source, target, kind = "route") => ({ id: source + ">" + target, source_card_id: source, target_card_id: target, kind,
  author_kind: "human" });
const center = box => [box.x + box.width / 2, box.y + box.height / 2];
const overlap = (a, b) => a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;

test("routes run into the question from both sides, evidence sits below it and cards on no route wait above", () => {
  const board = { cards: ["A", "B", "C", "D", "E"].map(id => card(id)),
    links: [link("A", "B"), link("B", null), link("C", null), link("D", null, "evidence")] };
  const result = layout.layoutBoard(board);
  assert.deepEqual(center(result.question), [0, 0]);
  assert.deepEqual(result.routed.sort(), ["A", "B", "C"]);
  assert.deepEqual(result.evidence, ["D"]);
  assert.deepEqual(result.tray, ["E"]);
  const [bx, by] = center(result.cards.B), [ax, ay] = center(result.cards.A), [cx] = center(result.cards.C);
  assert.ok(bx < 0 && cx > 0, "the two routes take opposite sides");
  assert.ok(ax < bx && ay === by, "a route runs outward from the question in one row");
  assert.equal(Math.abs(bx), Math.abs(cx), "first steps sit at the same distance on both sides");
  assert.ok(result.cards.D.y > result.question.y + result.question.height, "evidence sits below the question");
  assert.ok(result.cards.E.y + result.cards.E.height < result.question.y, "the tray sits above everything");
});

test("no two cards overlap, and branching routes stack their steps", () => {
  const ids = "ABCDEFGHIJKLMNOP".split("");
  const links = [link("A", null), link("B", null), link("C", null), link("D", "A"), link("E", "A"), link("F", "D"), link("G", "D"),
    link("H", "B"), link("I", "H"), link("J", null, "evidence"), link("K", null, "evidence"), link("L", "E")];
  const result = layout.layoutBoard({ cards: ids.map(id => card(id)), links });
  const boxes = [result.question, ...Object.values(result.cards)];
  for (let first = 0; first < boxes.length; first++)
    for (let second = first + 1; second < boxes.length; second++) assert.ok(!overlap(boxes[first], boxes[second]), `${first} and ${second} overlap`);
  assert.equal(Object.keys(result.cards).length, ids.length);
  assert.ok(result.cards.D.y !== result.cards.E.y, "two steps into one card stack");
});

test("a loop of links reaches no question, so its cards wait in the tray; rank orders siblings", () => {
  const looped = layout.layoutBoard({ cards: [card("A"), card("B")], links: [link("A", "B"), link("B", "A")] });
  assert.deepEqual(looped.tray, ["A", "B"]);
  const ranked = layout.layoutBoard({ cards: [card("A", { rank: 2 }), card("B", { rank: 1 }), card("C", { rank: 3 })],
    links: [link("A", null), link("B", null), link("C", null)] });
  assert.ok(ranked.cards.B.x < 0, "the best-ranked route takes the left side first");
});

test("quadrants, themes, link paths and the fitted view", () => {
  assert.equal(layout.quadrant(card("A", { promise: 0.9, effort: 0.2 })), 0);
  assert.equal(layout.quadrant(card("A", { promise: 0.9, effort: 0.8 })), 1);
  assert.equal(layout.quadrant(card("A", { promise: 0.1, effort: 0.1 })), 2);
  assert.equal(layout.quadrant(card("A", { promise: 0.1, effort: 0.9 })), 3);
  assert.equal(layout.quadrant(card("A")), null);
  const cards = [card("A", { theme: "Via Borel" }), card("B", { theme: "Via Borel" }), card("C", { theme: "Via Suslin" }), card("D")];
  const result = layout.layoutBoard({ cards, links: [link("A", "B"), link("B", null), link("C", null)] });
  const regions = layout.themeRegions(result, cards);
  assert.deepEqual(regions.map(region => region.theme), ["Via Borel", "Via Suslin"]);
  for (const id of ["A", "B"]) {
    const box = result.cards[id], region = regions[0].box;
    assert.ok(box.x > region.x && box.x + box.width < region.x + region.width, id + " sits inside its theme");
  }
  assert.match(layout.linkPath({ x: 0, y: 0, width: 100, height: 50 }, { x: 300, y: 0, width: 100, height: 50 }), /^M100 25 C/);
  assert.match(layout.linkPath({ x: 0, y: 0, width: 100, height: 50 }, { x: 0, y: 200, width: 100, height: 50 }), /^M50 50 C/);
  const view = layout.fitView({ x: -100, y: -50, width: 200, height: 100 }, 400, 300, 0, 10);
  assert.equal(view.zoom, 2);
  assert.deepEqual([view.x, view.y], [200, 150], "board point (0, 0) lands in the middle");
  assert.equal(layout.nearestCard(result, { x: result.cards.C.x + 5, y: result.cards.C.y + 5 }).id, "C");
});
