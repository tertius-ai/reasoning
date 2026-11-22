// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
/**
 * Where you keep your side chat. One layout belongs to your account, so it is the same on every device you sign in on:
 * on a wide window the side chat sits beside the topic at a width you drag, fills the conversation area (Full), or
 * floats over the topic; on a phone it is a sheet from the bottom at a height you drag. Sizes are shares of the
 * conversation area, so they carry across screens. Whether the side chat is open is kept per topic, with the topic list.
 */
import { useEffect, useSyncExternalStore } from "react";
import { request } from "./organization-api";

export type SideMode = "side" | "full" | "float";
/** A floating panel's place, as shares of the conversation area. */
export type Floating = { x: number; y: number; w: number; h: number };
export type SideChatLayout = { desktop: { mode: SideMode; width: number | null; floating: Floating | null }; phone: { height: number } };
export type Box = { x: number; y: number; w: number; h: number };
export type Area = { width: number; height: number };

/** The side chat's narrowest width. Beside the topic its edge drags all the way across the conversation area: the main
 * chat narrows to FLOOR pixels, then the side chat slides over it, and within FULL_REACH of the far end it takes the
 * whole area (Full). */
export const COLUMN = 360, FLOOR = 320, FULL_REACH = 160;
/** Arrow keys move the edge this far; with Shift, a tenth of the area. */
export const STEP = 24;
export const FLOAT_MIN = { w: 320, h: 240 };
/** The heights a phone's sheet settles at, as shares of the screen below the app bar. */
export const SHEET_STOPS = [0.36, 0.6, 1] as const;
/** Media queries for a phone, where the side chat is a sheet, and for a window where it floats over the topic. */
export const PHONE = "(max-width: 700px)", OVERLAY = "(max-width: 1000px)";
export const DEFAULT_LAYOUT: SideChatLayout = { desktop: { mode: "side", width: null, floating: null }, phone: { height: 0.6 } };

/** Whether the side chat hides the main chat: Full on a wide window, a full-height sheet on a phone. */
export const hidesMainChat = (layout: SideChatLayout, phone: boolean) => phone ? layout.phone.height >= 1 : layout.desktop.mode === "full";

const share = (value: unknown, low: number) => typeof value === "number" && Number.isFinite(value) && value >= low && value <= 1 ? value : null;

/** A stored or received layout, with anything the app cannot draw replaced by the default. */
export function normalLayout(value: unknown): SideChatLayout {
  const record = (value && typeof value === "object" ? value : {}) as Record<string, any>;
  const desktop = (record.desktop && typeof record.desktop === "object" ? record.desktop : {}) as Record<string, any>;
  const place = desktop.floating && typeof desktop.floating === "object" ? desktop.floating : null;
  const floating = place && [share(place.x, 0), share(place.y, 0), share(place.w, 0.01), share(place.h, 0.01)].every(item => item !== null)
    ? { x: place.x, y: place.y, w: place.w, h: place.h } : null;
  const mode: SideMode = desktop.mode === "full" || desktop.mode === "float" ? desktop.mode : "side";
  return { desktop: { mode, width: share(desktop.width, 0.01), floating }, phone: { height: share(record.phone?.height, 0.2) ?? DEFAULT_LAYOUT.phone.height } };
}

/** The widest the side chat rests beside the main chat: the main chat keeps FLOOR pixels, or, while the side chat lies
 * over it on a narrower window, at least FULL_REACH. */
export const widestSide = (area: number, over = false) => Math.max(COLUMN, area - (over ? FULL_REACH : FLOOR));
export const clampWidth = (width: number, area: number, over = false) => Math.round(Math.min(widestSide(area, over), Math.max(COLUMN, width)));
/** The width for a stored share of the area; a column until a width is chosen. */
export const sideWidth = (stored: number | null, area: number, over = false) => clampWidth(stored === null ? COLUMN : stored * area, area, over);
/** Where a dragged edge rests when let go, as a width; the whole area means Full. Within FULL_REACH of the far end it
 * takes the whole area. Beside the main chat, a width that leaves it less than its floor settles at the floor. */
export function restingWidth(width: number, area: number, over = false): number {
  if (width > area - FULL_REACH) return area;
  return clampWidth(width, area, over);
}
/** What a width looks like while the edge moves: Full at the whole area, and beside the main chat past its floor, the
 * side chat sliding over it by `overlap` pixels. */
export function dragView(width: number, area: number, over = false): { width: number; full: boolean; overlap: number } {
  const value = Math.round(Math.max(COLUMN, Math.min(area, width)));
  const full = value >= area;
  return { width: value, full, overlap: !over && !full ? Math.max(0, value - (area - FLOOR)) : 0 };
}
/** The width a key gives the edge, which is on the side chat's left: ← widens and → narrows by STEP pixels, or by a tenth
 * of the area with Shift. Past the widest resting width the next step is the whole area, and back from there the widest.
 * Home gives the column and End the whole area. Other keys give null. */
export function keyWidth(key: string, shift: boolean, width: number, area: number, over = false): number | null {
  const step = shift ? Math.round(area / 10) : STEP, widest = widestSide(area, over);
  if (key === "ArrowLeft") return width >= area || width + step > widest ? area : width + step;
  if (key === "ArrowRight") return width >= area ? widest : Math.max(COLUMN, width - step);
  if (key === "Home") return COLUMN;
  if (key === "End") return area;
  return null;
}
/** How a width reads to a screen reader. */
export const widthText = (width: number, area: number) =>
  width >= area ? "Whole conversation area; the main chat is hidden" : `${Math.round(width / area * 100)} percent of the conversation area`;

/** A floating panel kept inside the area, at least FLOAT_MIN in size, with 8 pixels around it. */
export function placeBox(box: Box, area: Area): Box {
  const w = Math.round(Math.min(Math.max(FLOAT_MIN.w, area.width - 16), Math.max(FLOAT_MIN.w, box.w)));
  const h = Math.round(Math.min(Math.max(FLOAT_MIN.h, area.height - 16), Math.max(FLOAT_MIN.h, box.h)));
  return { w, h, x: Math.round(Math.max(8, Math.min(area.width - w - 8, box.x))), y: Math.round(Math.max(8, Math.min(area.height - h - 8, box.y))) };
}
/** Where a floating panel opens: its saved place, or at the top right of the area, ending above the topic's message box.
 * On a wide window it is 820 px wide, which fits beside the topic's centered reading column; otherwise 560 px. */
export function floatingBox(stored: Floating | null, area: Area): Box {
  if (stored) return placeBox({ x: stored.x * area.width, y: stored.y * area.height, w: stored.w * area.width, h: stored.h * area.height }, area);
  const w = area.width >= 2000 ? 820 : 560, h = Math.round(Math.min(area.height * 0.82, area.height - 156));
  return placeBox({ w, h, x: area.width - w - 24, y: 16 }, area);
}
const rounded = (value: number) => Math.round(value * 1000) / 1000;
export const boxShare = (box: Box, area: Area): Floating =>
  ({ x: rounded(box.x / area.width), y: rounded(box.y / area.height), w: rounded(box.w / area.width), h: rounded(box.h / area.height) });
/** The phone sheet's height settles at the nearest of its stops. */
export const settleSheet = (height: number) =>
  SHEET_STOPS.reduce<number>((best, stop) => Math.abs(stop - height) < Math.abs(best - height) ? stop : best, SHEET_STOPS[1]);

// The layout of the signed-in person: the browser's copy draws the first frame, then the account's copy replaces it.
let owner = "", current: SideChatLayout = DEFAULT_LAYOUT, loading: Promise<void> | null = null, changed = false;
let sending = false, queued: SideChatLayout | null = null;
const listeners = new Set<() => void>();
const cacheKey = () => "reasoning.side-chat-layout." + owner;

function publish(next: SideChatLayout) {
  current = next;
  try { localStorage.setItem(cacheKey(), JSON.stringify(next)); } catch { /* The browser's copy is optional. */ }
  listeners.forEach(listener => listener());
}
function adopt(user: string) {
  if (user === owner) return;
  owner = user; loading = null; changed = false;
  try { current = normalLayout(JSON.parse(localStorage.getItem(cacheKey()) ?? "null")); } catch { current = DEFAULT_LAYOUT; }
}
/** Reads the account's layout once per page and person. */
export function loadSideChatLayout(user: string): Promise<void> {
  adopt(user);
  loading ??= request<unknown>("/auth/side-chat").then(value => { if (!changed && user === owner) publish(normalLayout(value)); })
    .catch(() => { loading = null; });
  return loading;
}
/** Uses a new layout at once and saves it to the account right away. One save is sent at a time; changes made while it
 * is on its way are sent together after it, so the account ends with the newest layout. */
export function saveSideChatLayout(next: SideChatLayout) {
  changed = true;
  publish(next);
  queued = next;
  flush();
}
function flush() {
  if (sending || !queued) return;
  const body = queued;
  queued = null; sending = true;
  void request("/auth/side-chat", { method: "PUT", body }).catch(() => { /* The next change saves again. */ })
    .finally(() => { sending = false; flush(); });
}
const subscribe = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; };
/** The signed-in person's side chat layout; it changes for everyone using it when any of them saves a new one. With `load`,
 * it also reads the account's copy, which only an open side chat needs; until then the browser's copy stands in. */
export function useSideChatLayout(user: string, load = false): SideChatLayout {
  adopt(user);
  useEffect(() => { if (load) void loadSideChatLayout(user); }, [user, load]);
  return useSyncExternalStore(subscribe, () => current, () => DEFAULT_LAYOUT);
}
/** Whether a media query matches, kept up to date. */
export function useMedia(query: string): boolean {
  return useSyncExternalStore(listener => {
    const list = window.matchMedia(query);
    list.addEventListener("change", listener);
    return () => list.removeEventListener("change", listener);
  }, () => window.matchMedia(query).matches, () => false);
}
