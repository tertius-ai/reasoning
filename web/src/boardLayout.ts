// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
/**
 * Where a board's cards sit, in board units: the question in the middle, the routes that lead to it on either side,
 * evidence below it, and cards on no route in a tray above. Positions come from the links alone, so turning an
 * overlay on or off never moves a card.
 */
import type { BoardCard, BoardContent } from "./organization-api";

export const CARD = { width: 220, height: 112 };
export const QUESTION = { width: 260, height: 150 };
const COLUMN = 290, ROW = 140, FIRST = 70, GAP = 90, TRAY_ROW = 5;

/** A rectangle in board units; x and y are its top-left corner. */
export type Box = { x: number; y: number; width: number; height: number };
export type Layout = {
  question: Box; cards: Record<string, Box>;
  /** Cards whose route reaches the question, those that only give it evidence, and those on no route. */
  routed: string[]; evidence: string[]; tray: string[];
  bounds: Box;
};

const centered = (x: number, y: number, size: { width: number; height: number }): Box =>
  ({ x: x - size.width / 2, y: y - size.height / 2, width: size.width, height: size.height });
const order = (cards: BoardCard[]) => (a: string, b: string) => {
  const first = cards.find(card => card.id === a)!, second = cards.find(card => card.id === b)!;
  return (first.rank ?? Infinity) - (second.rank ?? Infinity) || first.created_at - second.created_at || a.localeCompare(b);
};

export function layoutBoard(board: Pick<BoardContent, "cards" | "links">): Layout {
  const ids = new Set(board.cards.map(card => card.id)), sort = order(board.cards);
  // Each card follows one link toward the question: a route before evidence, the first of each kind.
  const next = new Map<string, string | null>(), evidence = new Set<string>();
  for (const link of [...board.links].sort((a, b) => (a.kind === "route" ? 0 : 1) - (b.kind === "route" ? 0 : 1))) {
    const source = link.source_card_id, target = link.target_card_id;
    if (!ids.has(source) || next.has(source) || evidence.has(source) || target !== null && !ids.has(target)) continue;
    if (target === null && link.kind === "evidence") evidence.add(source); else next.set(source, target);
  }
  // A card's route reaches the question when following it ends there without a loop.
  const reaches = new Map<string, boolean>();
  const reach = (id: string, seen: Set<string> = new Set()): boolean => {
    if (reaches.has(id)) return reaches.get(id)!;
    if (seen.has(id) || !next.has(id)) { reaches.set(id, false); return false; }
    seen.add(id);
    const target = next.get(id)!;
    const result = target === null || reach(target, seen);
    reaches.set(id, result);
    return result;
  };
  const routed = board.cards.map(card => card.id).filter(id => !evidence.has(id) && reach(id));
  const children = new Map<string | null, string[]>();
  for (const id of routed) children.set(next.get(id)!, [...children.get(next.get(id)!) ?? [], id]);
  for (const list of children.values()) list.sort(sort);
  const leaves = new Map<string, number>();
  const count = (id: string): number => {
    const value = Math.max(1, (children.get(id) ?? []).reduce((sum, child) => sum + count(child), 0));
    leaves.set(id, value);
    return value;
  };
  const first = children.get(null) ?? [];
  first.forEach(count);
  // Routes go to the side with fewer leaves so far, starting on the left.
  const sides: Record<"-1" | "1", string[]> = { "-1": [], "1": [] }, weight = { "-1": 0, "1": 0 };
  for (const id of first) {
    const side = weight["-1"] <= weight["1"] ? "-1" : "1";
    sides[side].push(id); weight[side] += leaves.get(id)!;
  }
  const cards: Record<string, Box> = {};
  const place = (id: string, side: -1 | 1, depth: number, top: number) => {
    const span = leaves.get(id)! * ROW;
    const x = side * (QUESTION.width / 2 + FIRST + CARD.width / 2 + (depth - 1) * COLUMN);
    cards[id] = centered(x, top + span / 2, CARD);
    let cursor = top;
    for (const child of children.get(id) ?? []) { place(child, side, depth + 1, cursor); cursor += leaves.get(child)! * ROW; }
  };
  for (const side of ["-1", "1"] as const) {
    let top = -weight[side] * ROW / 2;
    for (const id of sides[side]) { place(id, Number(side) as -1 | 1, 1, top); top += leaves.get(id)! * ROW; }
  }
  const question = centered(0, 0, QUESTION);
  const routedBottom = Math.max(question.y + question.height, ...Object.values(cards).map(box => box.y + box.height));
  const routedTop = Math.min(question.y, ...Object.values(cards).map(box => box.y));
  const evidenceIds = board.cards.map(card => card.id).filter(id => evidence.has(id)).sort(sort);
  evidenceIds.forEach((id, index) => {
    cards[id] = centered((index - (evidenceIds.length - 1) / 2) * COLUMN, routedBottom + GAP + CARD.height / 2, CARD);
  });
  const tray = board.cards.map(card => card.id).filter(id => !cards[id]).sort(sort);
  const rows = Math.ceil(tray.length / TRAY_ROW);
  tray.forEach((id, index) => {
    const row = Math.floor(index / TRAY_ROW), inRow = Math.min(TRAY_ROW, tray.length - row * TRAY_ROW);
    cards[id] = centered((index % TRAY_ROW - (inRow - 1) / 2) * COLUMN, routedTop - GAP - (rows - row - 0.5) * ROW, CARD);
  });
  const boxes = [question, ...Object.values(cards)];
  const minX = Math.min(...boxes.map(box => box.x)), minY = Math.min(...boxes.map(box => box.y));
  const maxX = Math.max(...boxes.map(box => box.x + box.width)), maxY = Math.max(...boxes.map(box => box.y + box.height));
  return { question, cards, routed, evidence: evidenceIds, tray, bounds: { x: minX, y: minY, width: maxX - minX, height: maxY - minY } };
}

/** A card's place in the promise × effort grid: quick win, big bet, cheap check, or skip for now. */
export const QUADRANTS = ["Quick win", "Big bet", "Cheap check", "Skip for now"] as const;
export function quadrant(card: Pick<BoardCard, "promise" | "effort">): number | null {
  if (card.promise === null || card.effort === null) return null;
  return (card.promise >= 0.5 ? 0 : 2) + (card.effort >= 0.5 ? 1 : 0);
}

/** One region per theme around its cards, padded, for the Themes overlay. */
export function themeRegions(layout: Layout, cards: BoardCard[], padding = 22): { theme: string; box: Box }[] {
  const themes = new Map<string, Box[]>();
  for (const card of cards) if (card.theme && layout.cards[card.id]) themes.set(card.theme, [...themes.get(card.theme) ?? [], layout.cards[card.id]]);
  return [...themes].map(([theme, boxes]) => {
    const minX = Math.min(...boxes.map(box => box.x)), minY = Math.min(...boxes.map(box => box.y));
    const maxX = Math.max(...boxes.map(box => box.x + box.width)), maxY = Math.max(...boxes.map(box => box.y + box.height));
    return { theme, box: { x: minX - padding, y: minY - padding - 18, width: maxX - minX + padding * 2, height: maxY - minY + padding * 2 + 18 } };
  }).sort((a, b) => a.theme.localeCompare(b.theme));
}

/** A link from one box to another as a smooth curve, leaving and entering through the facing sides. */
export function linkPath(from: Box, to: Box): string {
  const fromX = from.x + from.width / 2, fromY = from.y + from.height / 2, toX = to.x + to.width / 2, toY = to.y + to.height / 2;
  if (Math.abs(toX - fromX) < (from.width + to.width) / 2) {
    const down = toY > fromY, startY = down ? from.y + from.height : from.y, endY = down ? to.y : to.y + to.height;
    const bend = (endY - startY) / 2;
    return `M${fromX} ${startY} C${fromX} ${startY + bend} ${toX} ${endY - bend} ${toX} ${endY}`;
  }
  const right = toX > fromX, startX = right ? from.x + from.width : from.x, endX = right ? to.x : to.x + to.width;
  const bend = (endX - startX) / 2;
  return `M${startX} ${fromY} C${startX + bend} ${fromY} ${endX - bend} ${toY} ${endX} ${toY}`;
}

/** The view that fits `bounds` in a `width` × `height` viewport: a zoom, and the screen position of board point (0, 0). */
export function fitView(bounds: Box, width: number, height: number, padding = 48, maximum = 1.2): { zoom: number; x: number; y: number } {
  const zoom = Math.max(0.05, Math.min(maximum, (width - padding * 2) / Math.max(1, bounds.width), (height - padding * 2) / Math.max(1, bounds.height)));
  return { zoom, x: width / 2 - (bounds.x + bounds.width / 2) * zoom, y: height / 2 - (bounds.y + bounds.height / 2) * zoom };
}

/** The card whose centre lies nearest a board point, with the distance. */
export function nearestCard(layout: Layout, point: { x: number; y: number }): { id: string; distance: number } | null {
  let best: { id: string; distance: number } | null = null;
  for (const [id, box] of Object.entries(layout.cards)) {
    const distance = Math.hypot(box.x + box.width / 2 - point.x, box.y + box.height / 2 - point.y);
    if (!best || distance < best.distance) best = { id, distance };
  }
  return best;
}

/** Whether a board point lies inside a box. */
export const inside = (box: Box, point: { x: number; y: number }) =>
  point.x >= box.x && point.x <= box.x + box.width && point.y >= box.y && point.y <= box.y + box.height;
