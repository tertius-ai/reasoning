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

test("the width stays between a column and leaving the topic a column, and starts as a column", () => {
  const { sideWidth, clampWidth, widestSide } = layout;
  assert.equal(sideWidth(null, 1170), 360);
  assert.equal(sideWidth(0.5, 1170), 585);
  assert.equal(sideWidth(0.95, 1170), 810);
  assert.equal(sideWidth(0.1, 1170), 360);
  assert.equal(widestSide(600), 360, "an area too small for both still gives the side chat its column");
  assert.equal(clampWidth(900, 1000, 48), 900, "over a narrower window the topic keeps only 48 px");
  assert.equal(clampWidth(990, 1000, 48), 952);
});

test("the edge snaps at Column, Half and Wide, and each snap point appears only where it differs", () => {
  const { widthSnaps, snapWidth, snapAt } = layout;
  assert.deepEqual(widthSnaps(1170), [{ name: "Column", width: 360 }, { name: "Half", width: 585 }, { name: "Wide", width: 810 }]);
  assert.deepEqual(widthSnaps(2674).map(snap => snap.width), [360, 1337, 2314], "the reporter's window");
  assert.deepEqual(widthSnaps(760).map(snap => snap.name), ["Column"], "Half would equal Wide, which is the column");
  assert.deepEqual(snapWidth(575, 1170), { width: 585, snap: { name: "Half", width: 585 } });
  assert.deepEqual(snapWidth(700, 1170), { width: 700, snap: null });
  assert.equal(snapAt(810, 1170).name, "Wide");
  assert.equal(snapAt(700, 1170), null);
});

test("keys move the edge: arrows by 24 px, Shift to the next snap point, Home and End to the ends", () => {
  const { keyWidth } = layout;
  assert.equal(keyWidth("ArrowLeft", false, 585, 1170), 609, "the edge is on the left, so ← widens");
  assert.equal(keyWidth("ArrowRight", false, 585, 1170), 561);
  assert.equal(keyWidth("ArrowRight", false, 370, 1170), 360);
  assert.equal(keyWidth("ArrowLeft", true, 400, 1170), 585);
  assert.equal(keyWidth("ArrowLeft", true, 585, 1170), 810);
  assert.equal(keyWidth("ArrowRight", true, 810, 1170), 585);
  assert.equal(keyWidth("ArrowRight", true, 360, 1170), 360);
  assert.equal(keyWidth("Home", false, 700, 1170), 360);
  assert.equal(keyWidth("End", false, 400, 1170), 810);
  assert.equal(keyWidth("Tab", false, 400, 1170), null);
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
