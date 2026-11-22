// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { createServer } from "vite";

let server, layout;
before(async () => {
  server = await createServer({ appType: "custom", logLevel: "silent", server: { middlewareMode: true } });
  layout = await server.ssrLoadModule("/src/sideChatLayout.ts");
});
after(async () => { await server?.close(); });

test("a stored layout keeps what the app can draw and replaces the rest with the default", () => {
  const { normalLayout, DEFAULT_LAYOUT } = layout;
  assert.deepEqual(normalLayout(null), DEFAULT_LAYOUT);
  assert.deepEqual(normalLayout([]), DEFAULT_LAYOUT);
  const saved = { desktop: { mode: "float", width: 0.5, floating: { x: 0.4, y: 0.05, w: 0.48, h: 0.82 } }, phone: { height: 1 } };
  assert.deepEqual(normalLayout(saved), saved);
  assert.deepEqual(normalLayout({ desktop: { mode: "sideways", width: 3, floating: { x: 0, y: 0, w: 0, h: 1 } }, phone: { height: 0.1 } }),
    DEFAULT_LAYOUT);
  assert.equal(normalLayout({ desktop: { mode: "full", width: "0.5" } }).desktop.width, null);
});

test("a resting width stays between a column and leaving the main chat 320 px, and starts as a column", () => {
  const { sideWidth, clampWidth, widestSide } = layout;
  assert.equal(sideWidth(null, 1170), 360);
  assert.equal(sideWidth(0.5, 1170), 585);
  assert.equal(sideWidth(0.95, 1170), 850, "the main chat keeps its 320 px floor");
  assert.equal(sideWidth(0.1, 1170), 360);
  assert.equal(widestSide(600), 360, "an area too small for both still gives the side chat its column");
  assert.equal(clampWidth(900, 1000, true), 840, "lying over the topic, it rests short of the whole area");
});

test("the edge drags all the way across: past the main chat's floor it slides over it, and the far end is the whole area", () => {
  const { dragView } = layout;
  assert.deepEqual(dragView(585, 1170), { width: 585, full: false, overlap: 0 });
  assert.deepEqual(dragView(850, 1170), { width: 850, full: false, overlap: 0 }, "at the floor nothing overlaps yet");
  assert.deepEqual(dragView(1000, 1170), { width: 1000, full: false, overlap: 150 });
  assert.deepEqual(dragView(5000, 1170), { width: 1170, full: true, overlap: 0 });
  assert.deepEqual(dragView(100, 1170), { width: 360, full: false, overlap: 0 });
  assert.deepEqual(dragView(900, 1000, true), { width: 900, full: false, overlap: 0 }, "over the topic on a narrower window it just widens");
});

test("a let-go edge rests where it is, except near the end and while sliding over the main chat", () => {
  const { restingWidth } = layout;
  assert.equal(restingWidth(700, 1170), 700, "anywhere beside the main chat it stays");
  assert.equal(restingWidth(900, 1170), 850, "early in the slide-over it settles at the main chat's floor");
  assert.equal(restingWidth(1011, 1170), 1170, "within 160 px of the end it takes the whole area");
  assert.equal(restingWidth(1170, 1170), 1170);
  assert.equal(restingWidth(1010, 1170), 850, "at exactly 160 px from the end it stays beside the main chat");
  assert.equal(restingWidth(840, 1000, true), 840, "over the topic, its widest resting width is not the whole area");
  assert.equal(restingWidth(900, 1000, true), 1000);
});

test("keys move the edge: arrows by 24 px, Shift by a tenth, past the widest to the whole area, Home and End to the ends", () => {
  const { keyWidth, widthText } = layout;
  assert.equal(keyWidth("ArrowLeft", false, 585, 1170), 609, "the edge is on the left, so ← widens");
  assert.equal(keyWidth("ArrowRight", false, 585, 1170), 561);
  assert.equal(keyWidth("ArrowRight", false, 370, 1170), 360);
  assert.equal(keyWidth("ArrowLeft", true, 400, 1170), 517);
  assert.equal(keyWidth("ArrowLeft", false, 840, 1170), 1170, "past the main chat's floor the next step is the whole area");
  assert.equal(keyWidth("ArrowRight", false, 1170, 1170), 850, "and back from there, the widest resting width");
  assert.equal(keyWidth("Home", false, 700, 1170), 360);
  assert.equal(keyWidth("End", false, 400, 1170), 1170);
  assert.equal(keyWidth("Tab", false, 400, 1170), null);
  assert.equal(widthText(585, 1170), "50 percent of the conversation area");
  assert.equal(widthText(1170, 1170), "Whole conversation area; the main chat is hidden");
});

test("a floating panel opens at the right, stays inside the area, and is saved as shares", () => {
  const { floatingBox, placeBox, boxShare } = layout;
  const laptop = { width: 1170, height: 856 };
  assert.deepEqual(floatingBox(null, laptop), { w: 560, h: 700, x: 586, y: 16 }, "it ends above the topic's message box");
  const wide = floatingBox(null, { width: 2674, height: 1502 });
  assert.equal(wide.w, 820, "on a wide window it fits beside the topic's reading column");
  assert.ok(wide.x > (2674 + 900) / 2, "and starts right of that column");
  assert.deepEqual(placeBox({ x: -50, y: 900, w: 100, h: 100 }, laptop), { w: 320, h: 240, x: 8, y: 608 });
  assert.deepEqual(placeBox({ x: 0, y: 0, w: 5000, h: 5000 }, laptop), { w: 1154, h: 840, x: 8, y: 8 });
  const share = boxShare({ x: 586, y: 134, w: 560, h: 702 }, laptop);
  assert.deepEqual(share, { x: 0.501, y: 0.157, w: 0.479, h: 0.82 });
  const back = floatingBox(share, laptop);
  assert.ok(Math.abs(back.x - 586) <= 1 && Math.abs(back.w - 560) <= 1, "a saved place comes back where it was");
});

test("a phone's sheet settles at a third, three fifths or the full screen", () => {
  const { settleSheet } = layout;
  assert.equal(settleSheet(0.2), 0.36);
  assert.equal(settleSheet(0.5), 0.6);
  assert.equal(settleSheet(0.85), 1);
  assert.equal(settleSheet(1), 1);
});

test("the main chat is hidden in Full on a wide window and by a full-height sheet on a phone", () => {
  const { hidesMainChat, DEFAULT_LAYOUT } = layout;
  const full = { ...DEFAULT_LAYOUT, desktop: { ...DEFAULT_LAYOUT.desktop, mode: "full" } };
  assert.equal(hidesMainChat(full, false), true);
  assert.equal(hidesMainChat(full, true), false);
  assert.equal(hidesMainChat({ ...DEFAULT_LAYOUT, phone: { height: 1 } }, true), true);
  assert.equal(hidesMainChat(DEFAULT_LAYOUT, false), false);
});
