// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
/**
 * The frame of your side chat. On a wide window its left edge drags all the way across the conversation area: the main chat
 * narrows to a floor, then the side chat slides over it, and at the far end it takes the whole area, as Expand does. Expand
 * fills the conversation area and hides the main chat until
 * “Show main chat”; Float lifts it over the topic, where its title bar moves it and its edges resize it. On a phone it is a
 * sheet from the bottom whose grab bar sets its height. The layout is saved with your account (`sideChatLayout`).
 */
import React, { useLayoutEffect, useRef, useState } from "react";
import {
  type Area, type Box, type SideChatLayout, COLUMN, FLOOR, OVERLAY, PHONE, SHEET_STOPS, boxShare, dragView, floatingBox, keyWidth,
  placeBox, restingWidth, saveSideChatLayout, settleSheet, sideWidth, useMedia, useSideChatLayout, widthText,
} from "./sideChatLayout";

const Icon = ({ children }: { children: React.ReactNode }) => <svg width="15" height="15" viewBox="0 0 24 24" fill="none"
  stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{children}</svg>;
const EXPAND = <Icon><path d="M15 3h6v6" /><path d="M9 21H3v-6" /><path d="M21 3l-7 7" /><path d="M3 21l7-7" /></Icon>;
const COLLAPSE = <Icon><path d="M4 14h6v6" /><path d="M20 10h-6V4" /><path d="M14 10l7-7" /><path d="M3 21l7-7" /></Icon>;
const FLOAT = <Icon><rect x="3" y="4" width="18" height="16" rx="2" /><rect x="11" y="11" width="7" height="6" rx="1" fill="currentColor" stroke="none" opacity=".5" /></Icon>;
const DOCK = <Icon><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M15 4v16" /><rect x="15" y="4" width="6" height="16" fill="currentColor" stroke="none" opacity=".35" /></Icon>;
const MOVE = <Icon><path d="M12 3v18" /><path d="M3 12h18" /><path d="M9 6l3-3 3 3" /><path d="M9 18l3 3 3-3" /><path d="M6 9l-3 3 3 3" /><path d="M18 9l3 3-3 3" /></Icon>;
const EDGES = ["n", "s", "e", "w", "ne", "nw", "se", "sw"] as const;

const messagesText = (count: number) => count === 1 ? "1 new message" : count + " new messages";

/** Follows one pointer from its press to its release on the element that captured it. */
function follow(event: React.PointerEvent<HTMLElement>, move: (next: PointerEvent) => void, done: () => void) {
  const target = event.currentTarget;
  target.setPointerCapture(event.pointerId);
  const end = () => {
    target.removeEventListener("pointermove", move);
    target.removeEventListener("pointerup", end);
    target.removeEventListener("pointercancel", end);
    done();
  };
  target.addEventListener("pointermove", move);
  target.addEventListener("pointerup", end);
  target.addEventListener("pointercancel", end);
}

export function useSideChatFrame({ user, aside, mainUnread }: { user: string; aside: React.RefObject<HTMLElement | null>; mainUnread: number }) {
  const layout = useSideChatLayout(user, true);
  const phone = useMedia(PHONE), overlay = useMedia(OVERLAY);
  const latest = useRef(layout);
  latest.current = layout;
  const mode = layout.desktop.mode;
  const [area, setArea] = useState<Area>({ width: 0, height: 0 });
  const [wide, setWide] = useState(false);
  const [announcement, setAnnouncement] = useState("");
  const previousWidth = useRef<number | null>(null), back = useRef<"side" | "float">("side");
  const lastSheet = useRef(layout.phone.height < 1 ? layout.phone.height : SHEET_STOPS[1]);

  // The conversation area the side chat sits in: widths and a floating panel are measured against it. Lying over the topic
  // on a narrower window, the side chat reads the area's width from the row.
  useLayoutEffect(() => {
    const element = aside.current, row = element?.parentElement;
    if (!element || !row) return;
    const measure = () => {
      const box = row.getBoundingClientRect();
      setArea(current => current.width === box.width && current.height === box.height ? current : { width: box.width, height: box.height });
      row.style.setProperty("--side-chat-area", box.width + "px");
      setWide(element.getBoundingClientRect().width >= 600);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(row); observer.observe(element);
    return () => observer.disconnect();
  }, [aside]);

  const say = (text: string) => { setAnnouncement(""); requestAnimationFrame(() => setAnnouncement(text)); };
  const save = (desktop: Partial<SideChatLayout["desktop"]>) =>
    saveSideChatLayout({ ...latest.current, desktop: { ...latest.current.desktop, ...desktop } });
  const saveSheet = (height: number) => saveSideChatLayout({ ...latest.current, phone: { height } });

  // Full hides the main chat; “Show main chat” and Esc bring it back to the layout before.
  const full = phone ? layout.phone.height >= 1 : mode === "full";
  const toggleFull = () => {
    if (phone) {
      const next = full ? lastSheet.current : 1;
      saveSheet(next);
      say(next >= 1 ? "Side chat full screen; Show main chat goes back." : `Side chat ${Math.round(next * 100)} percent of the screen.`);
      return;
    }
    if (full) { save({ mode: back.current }); say("The main chat is back."); }
    else { back.current = mode === "float" ? "float" : "side"; save({ mode: "full" }); say("Side chat full; the main chat is hidden. Show main chat brings it back."); }
  };

  // The edge: the width is a share of the conversation area, and dragging goes all the way across it. Beside the main chat,
  // past its floor the side chat slides over it; on a narrower window the side chat already lies over the topic and just
  // widens. At the far end it takes the whole area, which is Full. Nothing is saved until the edge is let go.
  const span = area.width || COLUMN;
  const width = full ? span : sideWidth(layout.desktop.width, span, overlay);
  /** Draws a width while the edge moves, outside React so the conversation does not redraw on every move. */
  const show = (value: number) => {
    const element = aside.current, row = element?.parentElement;
    const view = dragView(value, span, overlay);
    if (!element || !row) return view.width;
    const topic = row.querySelector<HTMLElement>(":scope > .org-conversation");
    row.classList.toggle("org-side-full-row", view.full);
    row.classList.toggle("org-side-over-row", view.overlap > 0);
    element.classList.toggle("org-side-full", view.full);
    element.classList.toggle("org-side-side", !view.full);
    element.style.width = view.full ? "" : view.width + "px";
    element.style.marginLeft = view.overlap ? -view.overlap + "px" : "";
    if (topic) topic.style.width = view.overlap ? FLOOR + "px" : "";
    return view.width;
  };
  /** Lets the edge rest where it was left and saves that; the whole area means Full. */
  const rest = (value: number) => {
    const next = restingWidth(value, span, overlay), whole = next >= span;
    const element = aside.current, row = element?.parentElement;
    if (element && row) {
      // Draw the resting state as React will once the layout is saved, so nothing jumps in between.
      const topic = row.querySelector<HTMLElement>(":scope > .org-conversation");
      row.classList.remove("org-side-over-row");
      element.style.width = ""; element.style.marginLeft = "";
      if (topic) topic.style.width = "";
      row.classList.toggle("org-side-full-row", whole);
      element.classList.toggle("org-side-full", whole);
      element.classList.toggle("org-side-side", !whole);
      if (!whole) row.style.setProperty("--side-chat-share", String(next / span));
    }
    if (whole) { back.current = "side"; save({ mode: "full" }); }
    else {
      if (next > COLUMN) previousWidth.current = next;
      save({ mode: "side", width: Math.round(next / span * 1000) / 1000 });
    }
    say(widthText(next, span));
  };
  const toggleColumn = () => {
    if (full) rest(sideWidth(layout.desktop.width, span, overlay));
    else if (width > COLUMN + 1) { previousWidth.current = width; rest(COLUMN); }
    else rest(previousWidth.current ?? Math.round(span / 2));
  };
  const edgeDown = (event: React.PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    event.preventDefault();
    event.currentTarget.focus({ preventScroll: true });
    const startX = event.clientX, startWidth = full ? span : aside.current?.getBoundingClientRect().width ?? width;
    let value = startWidth;
    document.documentElement.classList.add("org-side-resizing");
    follow(event, next => { value = show(startWidth + startX - next.clientX); }, () => {
      document.documentElement.classList.remove("org-side-resizing");
      rest(value);
    });
  };
  const edgeKey = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Enter") { event.preventDefault(); toggleColumn(); return; }
    const next = keyWidth(event.key, event.shiftKey, width, span, overlay);
    if (next === null) return;
    event.preventDefault();
    rest(next);
  };

  // Floating: placed by shares of the conversation area, moved by the title bar and resized from any edge or corner.
  const box: Box | null = !phone && mode === "float" && area.width ? floatingBox(layout.desktop.floating, area) : null;
  const toggleFloat = () => {
    if (mode === "float") { save({ mode: "side" }); say("Side chat docked to the right."); return; }
    const opening = floatingBox(layout.desktop.floating, area);
    save({ mode: "float", floating: boxShare(opening, area) });
    say("Side chat floating over the topic.");
  };
  const place = (next: Box) => {
    const fitted = placeBox(next, area);
    if (aside.current) Object.assign(aside.current.style, { left: fitted.x + "px", top: fitted.y + "px", width: fitted.w + "px", height: fitted.h + "px" });
    return fitted;
  };
  const headerDown = (event: React.PointerEvent<HTMLElement>) => {
    if (!box || event.button !== 0 || (event.target as Element).closest("button, a, input, textarea")) return;
    event.preventDefault();
    const start = { ...box }, x = event.clientX, y = event.clientY;
    let moved = start;
    follow(event, next => { moved = place({ ...start, x: start.x + next.clientX - x, y: start.y + next.clientY - y }); },
      () => save({ floating: boxShare(moved, area) }));
  };
  const edgeResize = (edge: typeof EDGES[number]) => (event: React.PointerEvent<HTMLDivElement>) => {
    if (!box || event.button !== 0) return;
    event.preventDefault();
    const start = { ...box }, x = event.clientX, y = event.clientY;
    let resized = start;
    follow(event, next => {
      const dx = next.clientX - x, dy = next.clientY - y, size = { ...start };
      if (edge.includes("e")) size.w = start.w + dx;
      if (edge.includes("s")) size.h = start.h + dy;
      if (edge.includes("w")) { size.w = Math.max(320, start.w - dx); size.x = start.x + start.w - size.w; }
      if (edge.includes("n")) { size.h = Math.max(240, start.h - dy); size.y = start.y + start.h - size.h; }
      resized = place(size);
    }, () => save({ floating: boxShare(resized, area) }));
  };
  const moveKey = (event: React.KeyboardEvent<HTMLButtonElement>) => {
    const step = ({ ArrowLeft: [-24, 0], ArrowRight: [24, 0], ArrowUp: [0, -24], ArrowDown: [0, 24] } as Record<string, number[]>)[event.key];
    if (!step || !box) return;
    event.preventDefault();
    const next = placeBox(event.shiftKey ? { ...box, w: box.w + step[0], h: box.h + step[1] } : { ...box, x: box.x + step[0], y: box.y + step[1] }, area);
    save({ floating: boxShare(next, area) });
    say(`Side chat ${next.w} by ${next.h} pixels, ${next.x} from the left.`);
  };

  // A phone's sheet: its grab bar sets the height, which settles at a third, three fifths or the full screen.
  const sheetDown = (event: React.PointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    const room = window.innerHeight - 44;
    let height = layout.phone.height;
    follow(event, next => {
      height = Math.min(1, Math.max(0.2, (window.innerHeight - next.clientY) / room));
      aside.current?.style.setProperty("--side-sheet-height", String(height));
    }, () => {
      const settled = settleSheet(height);
      if (settled < 1) lastSheet.current = settled;
      aside.current?.style.setProperty("--side-sheet-height", String(settled));
      saveSheet(settled);
      say(`Side chat ${Math.round(settled * 100)} percent of the screen.`);
    });
  };
  const sheetKey = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Enter") { event.preventDefault(); toggleFull(); return; }
    const index = SHEET_STOPS.findIndex(stop => stop >= layout.phone.height - 0.01);
    const next = ({ ArrowUp: SHEET_STOPS[Math.min(SHEET_STOPS.length - 1, index + 1)], ArrowDown: SHEET_STOPS[Math.max(0, index - 1)],
      Home: SHEET_STOPS[0], End: SHEET_STOPS[SHEET_STOPS.length - 1] } as Record<string, number>)[event.key];
    if (next === undefined) return;
    event.preventDefault();
    if (next < 1) lastSheet.current = next;
    saveSheet(next);
    say(`Side chat ${Math.round(next * 100)} percent of the screen.`);
  };

  const view = phone ? "sheet" : mode;
  const className = " org-side-" + view + (full && view !== "full" ? " org-side-full" : "") + (wide ? " org-side-wide" : "");
  const style = (phone ? { "--side-sheet-height": String(layout.phone.height) }
    : box ? { left: box.x, top: box.y, width: box.w, height: box.h } : {}) as React.CSSProperties;
  const percent = Math.round(layout.phone.height * 100);
  const before = <>
    {!phone && mode !== "float" && <div className="org-side-edge" role="separator" aria-orientation="vertical" aria-label="Side chat width"
      aria-controls="org-side-chat" tabIndex={0} aria-valuemin={COLUMN} aria-valuemax={Math.round(span)} aria-valuenow={Math.round(width)}
      aria-valuetext={widthText(width, span)} title="Drag to resize, all the way across · double-click: column or your last width"
      onPointerDown={edgeDown} onKeyDown={edgeKey} onDoubleClick={toggleColumn}><span /></div>}
    {phone && <div className="org-side-grabber" role="separator" aria-orientation="horizontal" aria-label="Side chat height"
      aria-controls="org-side-chat" tabIndex={0} aria-valuemin={20} aria-valuemax={100} aria-valuenow={percent}
      aria-valuetext={`${percent} percent of the screen`} onPointerDown={sheetDown} onKeyDown={sheetKey}><span /></div>}
    <p className="org-visually-hidden" role="status" aria-live="polite">{announcement}</p>
  </>;
  const after = box ? EDGES.map(edge => <div key={edge} className={"org-side-resize org-side-resize-" + edge} aria-hidden="true"
    onPointerDown={edgeResize(edge)} />) : null;
  const tools = <>
    {box && <button type="button" className="org-icon-button org-side-move" aria-label="Move or resize the side chat"
      title="Arrows move · Shift+arrows resize · or drag the title bar" onKeyDown={moveKey}>{MOVE}</button>}
    {full ? <button type="button" className="org-icon-button org-side-show-main" onClick={toggleFull}
      aria-label={"Show main chat" + (mainUnread ? ", " + messagesText(mainUnread) : "")} title={phone ? "Show main chat" : "Show main chat · Esc"}>
      {COLLAPSE}<span>Show main chat</span>{mainUnread > 0 && <span className="org-unread">{mainUnread}</span>}
      {!phone && <kbd aria-hidden="true">Esc</kbd>}</button>
      : <button type="button" className="org-icon-button org-side-expand" onClick={toggleFull}
        aria-label={phone ? "Full screen" : "Expand: hide the main chat"} title={phone ? "Full screen" : "Expand over the main chat"}>{EXPAND}</button>}
    {!phone && <button type="button" className="org-icon-button org-side-float-button" onClick={toggleFloat} aria-pressed={mode === "float"}
      aria-label={mode === "float" ? "Dock the side chat to the right" : "Float the side chat over the topic"}
      title={mode === "float" ? "Dock to the right" : "Float over the topic"}>{mode === "float" ? DOCK : FLOAT}</button>}
  </>;
  const onKeyDown = (event: React.KeyboardEvent<HTMLElement>) => {
    if (event.key !== "Escape" || event.defaultPrevented || !full) return;
    event.preventDefault();
    toggleFull();
  };
  return { className, style, before, after, tools, onKeyDown, onHeaderPointerDown: headerDown, floating: Boolean(box) };
}
