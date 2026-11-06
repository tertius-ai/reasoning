// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
/**
 * A topic's board as one map: the question in the middle and the routes of cards that lead to it. Routes decide where
 * every card sits, so the other orderings are overlays on the same map: Rank numbers the cards, Rings marks their
 * maturity, Themes shades their groups and Quadrants adds a promise × effort mark. The model sets these only when
 * someone asks it to arrange the board.
 *
 * The map zooms continuously. From INNER_ZOOM on, a card with a board inside shows that board in place; zoom in
 * further to read it, or open it as the map. The box at the bottom asks your private side chat about the card most
 * in view, or the one you selected.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { type Box, CARD, type Layout, QUADRANTS, fitView, inside, layoutBoard, linkPath, nearestCard, quadrant, themeRegions } from "./boardLayout";
import { modelName } from "./ReplyAdditions";
import { ApiError, type Board, type BoardCard, type BoardContent, type BoardSummary, type Topic, aborted, errorMessage, organizationPath,
  request, topicPath } from "./organization-api";
import { startPolling } from "./polling";
import "./topic-board.css";

type Overlay = "rank" | "rings" | "themes" | "quadrants";
const OVERLAYS: { id: Overlay; label: string; hint: string }[] = [
  { id: "rank", label: "Rank", hint: "Number the cards from most to least promising" },
  { id: "rings", label: "Rings", hint: "Mark each card's maturity: idea, exploring or settled" },
  { id: "themes", label: "Themes", hint: "Shade the cards that share a theme" },
  { id: "quadrants", label: "Quadrants", hint: "Mark each card's promise and effort" },
];
const MIN_ZOOM = 0.2, MAX_ZOOM = 8;
/** From this zoom on, a card with a board inside shows that board in place. */
export const INNER_ZOOM = 1.6;
/** The part of a card that holds the board inside it, in board units below the card's title line. */
const INNER = { x: 8, y: 30, width: CARD.width - 16, height: CARD.height - 38 };
type View = { zoom: number; x: number; y: number };
type Selection = { board: string; card: string };

const clamp = (zoom: number) => Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, zoom));
const place = (box: Box): React.CSSProperties => ({ left: box.x, top: box.y, width: box.width, height: box.height });
/** The scale and offset that fit a board inside a card's inner area. */
export function innerFrame(layout: Layout): { scale: number; x: number; y: number } {
  const scale = Math.min(INNER.width / Math.max(1, layout.bounds.width), INNER.height / Math.max(1, layout.bounds.height));
  return { scale, x: INNER.x + (INNER.width - layout.bounds.width * scale) / 2 - layout.bounds.x * scale,
    y: INNER.y + (INNER.height - layout.bounds.height * scale) / 2 - layout.bounds.y * scale };
}
/** The board and card at the middle of the view: a card inside a card once that inner board is shown, else a card of the map. */
export function inView(layout: Layout, children: Record<string, { content: BoardContent; layout: Layout }>, point: { x: number; y: number },
  zoom: number, rootId: string): Selection | null {
  if (zoom >= INNER_ZOOM) {
    for (const [cardId, child] of Object.entries(children)) {
      const box = layout.cards[cardId];
      if (!box || !inside(box, point) || !child.content.cards.length) continue;
      const frame = innerFrame(child.layout);
      const local = { x: (point.x - box.x - frame.x) / frame.scale, y: (point.y - box.y - frame.y) / frame.scale };
      const nearest = nearestCard(child.layout, local);
      if (nearest) return { board: child.content.id, card: nearest.id };
    }
  }
  const nearest = nearestCard(layout, point);
  return nearest ? { board: rootId, card: nearest.id } : null;
}

function Marks({ card, overlays }: { card: BoardCard; overlays: Set<Overlay> }) {
  const place = quadrant(card);
  return <span className="org-board-marks">
    {overlays.has("rings") && card.maturity && <span className={"org-board-maturity org-board-maturity-" + card.maturity}>{card.maturity}</span>}
    {overlays.has("quadrants") && place !== null && <span className="org-board-quadrant" role="img"
      aria-label={`${QUADRANTS[place]}: promise ${card.promise}, effort ${card.effort}`} title={`${QUADRANTS[place]} · promise ${card.promise} · effort ${card.effort}`}>
      {[0, 1, 2, 3].map(cell => <i key={cell} className={cell === place ? "is-on" : undefined} />)}</span>}
    {card.statements.length > 0 && <span className="org-board-statements" title={card.statements.join(", ")}>
      {card.statements.length} statement{card.statements.length === 1 ? "" : "s"}</span>}
  </span>;
}

function Links({ layout, content, marker }: { layout: Layout; content: BoardContent; marker: string }) {
  const { bounds } = layout;
  return <svg className="org-board-links" aria-hidden="true" style={{ left: bounds.x - 40, top: bounds.y - 40, width: bounds.width + 80, height: bounds.height + 80 }}
    viewBox={`${bounds.x - 40} ${bounds.y - 40} ${bounds.width + 80} ${bounds.height + 80}`}>
    <defs><marker id={marker} viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path d="M2 1L8 5L2 9" fill="none" stroke="context-stroke" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" /></marker></defs>
    {content.links.map(link => {
      const from = layout.cards[link.source_card_id], to = link.target_card_id ? layout.cards[link.target_card_id] : layout.question;
      if (!from || !to) return null;
      return <path key={link.id} d={linkPath(from, to)} className={"org-board-link org-board-link-" + link.kind} markerEnd={`url(#${marker})`} />;
    })}
  </svg>;
}

function InnerBoard({ content, layout, zoom, selected, onSelect }: {
  content: BoardContent; layout: Layout; zoom: number; selected: Selection | null; onSelect: (selection: Selection) => void;
}) {
  const frame = innerFrame(layout), readable = zoom * frame.scale >= 0.35;
  return <div className="org-board-inner" style={{ transform: `translate(${frame.x}px, ${frame.y}px) scale(${frame.scale})` }}>
    <Links layout={layout} content={content} marker={"board-arrow-" + content.id} />
    <div className="org-board-question org-board-question-inner" style={place(layout.question)}>{readable && content.question}</div>
    {content.cards.map(card => {
      const box = layout.cards[card.id], chosen = selected?.board === content.id && selected.card === card.id;
      return <button type="button" key={card.id} className={"org-board-card org-board-card-inner org-board-card-" + card.kind + (chosen ? " is-selected" : "")}
        style={place(box)} aria-label={card.text}
        onClick={event => { event.stopPropagation(); onSelect({ board: content.id, card: card.id }); }}>
        {readable && <span className="org-board-card-text">{card.text}</span>}
      </button>;
    })}
  </div>;
}

export function TopicBoard({ organizationId, topic, canWrite, model, effort, onAsk, onAuthLost }: {
  organizationId: string; topic: Topic; canWrite: boolean;
  /** The conversation's reply model and effort, used to arrange the board. */
  model: string; effort: string | null;
  /** Ask your private side chat about a card. */
  onAsk: (text: string, cardId: string) => Promise<void>;
  onAuthLost: () => void;
}) {
  const [boards, setBoards] = useState<BoardSummary[] | null>(null);
  const [boardId, setBoardId] = useState<string | null>(null);
  const [board, setBoard] = useState<Board | null>(null);
  const [view, setView] = useState<View>({ zoom: 1, x: 0, y: 0 });
  const [size, setSize] = useState({ width: 0, height: 0 });
  const [overlays, setOverlays] = useState<Set<Overlay>>(() => new Set(["rank", "rings"]));
  const [selected, setSelected] = useState<Selection | null>(null);
  const [question, setQuestion] = useState("");
  const [idea, setIdea] = useState<string | null>(null);
  const [ask, setAsk] = useState("");
  const [linkTarget, setLinkTarget] = useState("question");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const viewport = useRef<HTMLDivElement>(null), poller = useRef<{ wake(): void } | null>(null);
  const fitted = useRef<string | null>(null), drag = useRef<{ x: number; y: number; view: View } | null>(null);
  const failed = useCallback((cause: unknown) => {
    if (aborted(cause)) return;
    if (cause instanceof ApiError && cause.status === 401) onAuthLost(); else setError(errorMessage(cause));
  }, [onAuthLost]);
  const boardPath = (id: string) => organizationPath(organizationId) + "/boards/" + encodeURIComponent(id);

  useEffect(() => {
    const controller = new AbortController();
    request<BoardSummary[]>(topicPath(organizationId, topic.id) + "/boards", { signal: controller.signal }).then(list => {
      setBoards(list); setBoardId(current => current ?? list[0]?.id ?? null);
    }).catch(failed);
    return () => controller.abort();
  }, [organizationId, topic.id, failed]);
  useEffect(() => {
    if (!boardId) return;
    setNotice(""); setError("");
    const controller = new AbortController();
    poller.current = startPolling(async () => {
      try {
        const next = await request<Board>(boardPath(boardId), { signal: controller.signal });
        setBoard(current => current && JSON.stringify(current) === JSON.stringify(next) ? current : next);
        // An arrangement in progress is checked often; otherwise the map refreshes for other people's changes.
        return next.arranging && ["queued", "running"].includes(next.arranging.status) ? 2000 : 15000;
      } catch (cause) {
        if (controller.signal.aborted) return null;
        failed(cause); return 15000;
      }
    }, controller.signal);
    return () => controller.abort();
  }, [boardId, organizationId]);
  useEffect(() => {
    const element = viewport.current;
    if (!element) return;
    const observer = new ResizeObserver(() => setSize({ width: element.clientWidth, height: element.clientHeight }));
    observer.observe(element);
    return () => observer.disconnect();
  }, [board !== null]);

  const layout = useMemo(() => board ? layoutBoard(board) : null, [board?.cards, board?.links]);
  const children = useMemo(() => Object.fromEntries(Object.entries(board?.children ?? {})
    .map(([card, content]) => [card, { content, layout: layoutBoard(content) }])), [board?.children]);
  const regions = useMemo(() => layout && board ? themeRegions(layout, board.cards) : [], [layout, board?.cards]);
  // The map stays fitted as the board grows until you pan or zoom; Fit returns to that.
  const following = useRef(true);
  const fit = useCallback(() => {
    following.current = true;
    if (layout && size.width) setView(fitView(layout.bounds, size.width, size.height));
  }, [layout, size.width, size.height]);
  useEffect(() => {
    if (!board || !layout || !size.width) return;
    if (fitted.current !== board.id) { fitted.current = board.id; following.current = true; }
    if (following.current) setView(fitView(layout.bounds, size.width, size.height));
  }, [board?.id, layout, size.width, size.height]);
  const moveView = useCallback((change: (current: View) => View) => { following.current = false; setView(change); }, []);

  const zoomAt = useCallback((factor: number, point = { x: size.width / 2, y: size.height / 2 }) => moveView(current => {
    const zoom = clamp(current.zoom * factor), ratio = zoom / current.zoom;
    return { zoom, x: point.x - (point.x - current.x) * ratio, y: point.y - (point.y - current.y) * ratio };
  }), [size.width, size.height, moveView]);
  useEffect(() => {
    const element = viewport.current;
    if (!element) return;
    // Pinching, or scrolling with Ctrl/⌘, zooms around the pointer; scrolling pans.
    const wheel = (event: WheelEvent) => {
      event.preventDefault();
      const bounds = element.getBoundingClientRect();
      if (event.ctrlKey || event.metaKey) zoomAt(Math.exp(-event.deltaY * 0.01), { x: event.clientX - bounds.left, y: event.clientY - bounds.top });
      else moveView(current => ({ ...current, x: current.x - event.deltaX, y: current.y - event.deltaY }));
    };
    element.addEventListener("wheel", wheel, { passive: false });
    return () => element.removeEventListener("wheel", wheel);
  }, [zoomAt, moveView, board !== null]);

  const center = { x: (size.width / 2 - view.x) / view.zoom, y: (size.height / 2 - view.y) / view.zoom };
  const focus = board && layout ? inView(layout, children, center, view.zoom, board.id) : null;
  const target = selected ?? focus;
  const cardOf = (selection: Selection | null): BoardCard | undefined => {
    if (!selection || !board) return undefined;
    const content = selection.board === board.id ? board : Object.values(board.children).find(child => child.id === selection.board);
    return content?.cards.find(card => card.id === selection.card);
  };
  const targetCard = cardOf(target), selectedCard = cardOf(selected);
  const selectedBoard: BoardContent | undefined = selected && board
    ? selected.board === board.id ? board : Object.values(board.children).find(child => child.id === selected.board) : undefined;

  const change = async (path: string, method: string, body?: unknown) => {
    setBusy(true); setError(""); setNotice("");
    try {
      const next = await request<Board>(path, { method, body: body ?? {} });
      if (next.id === board?.id) setBoard(next); else poller.current?.wake();
      return next;
    } catch (cause) { failed(cause); return null; } finally { setBusy(false); }
  };
  const create = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!question.trim()) return;
    setBusy(true); setError("");
    try {
      const made = await request<Board>(topicPath(organizationId, topic.id) + "/boards", { method: "POST", body: { question } });
      setBoards(current => [{ id: made.id, question: made.question, created_by: made.created_by, created_at: made.created_at,
        updated_at: made.updated_at, arranged_at: null, cards: 0 }, ...current ?? []]);
      setBoard(made); setBoardId(made.id); setQuestion("");
    } catch (cause) { failed(cause); } finally { setBusy(false); }
  };
  const addIdea = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!idea?.trim() || !board) return;
    // A new idea joins the board in view: the board inside a card once you have zoomed into it.
    const into = focus && focus.board !== board.id ? focus.board : board.id;
    if (await change(boardPath(into) + "/cards", "POST", { text: idea })) {
      setIdea(null);
      if (into !== board.id) { setNotice("Added inside the card in view."); poller.current?.wake(); }
    }
  };
  const arrange = async () => {
    if (!board || !model || model.startsWith("bot/")) return;
    setBusy(true); setError(""); setNotice("");
    try {
      await request(boardPath(board.id) + "/arrange", { method: "POST", body: { models: [model], client_id: crypto.randomUUID(),
        ...(effort ? { effort } : {}) } });
      poller.current?.wake();
    } catch (cause) { failed(cause); } finally { setBusy(false); }
  };
  const openBoard = async (selection: Selection) => {
    const card = cardOf(selection);
    if (!card) return;
    const opened = card.board ? { id: card.board.id } : await change(boardPath(selection.board) + "/cards/" + encodeURIComponent(card.id) + "/board", "POST");
    if (opened) { setSelected(null); setBoard(null); setBoardId(opened.id); }
  };
  // Zooming into a card hands the box to whatever is in view there, such as a card of the board inside it.
  const zoomToCard = (id: string) => {
    const box = layout?.cards[id];
    if (box && size.width) { setSelected(null); moveView(() => fitView(box, size.width, size.height, 32, MAX_ZOOM)); }
  };
  const submitAsk = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!ask.trim() || !target) return;
    setBusy(true); setError("");
    try { await onAsk(ask, target.card); setAsk(""); setNotice("Asked in your side chat."); }
    catch (cause) { failed(cause); } finally { setBusy(false); }
  };
  const arranging = board?.arranging && ["queued", "running"].includes(board.arranging.status) ? board.arranging : null;
  const unsupported = !model ? "Choose a reply model in the conversation to arrange the board."
    : model.startsWith("bot/") ? "Boards are arranged by OpenRouter models; choose one in the conversation." : "";

  if (boards === null) return <section className="org-board" aria-label="Board"><p role="status" className="org-board-empty">Loading board…</p></section>;
  if (!boardId) return <section className="org-board" aria-label="Board"><form className="org-board-start" onSubmit={event => void create(event)}>
    <h2>Make a board from a question</h2>
    <p>Put a question in the middle and gather ideas around it. Ask the model to arrange them into routes that lead to the question.</p>
    <label className="org-visually-hidden" htmlFor="org-board-question">Board question</label>
    <textarea id="org-board-question" rows={3} value={question} maxLength={1000} disabled={!canWrite || busy}
      placeholder="How do we show the motivic regulator is injective for number fields?" onChange={event => setQuestion(event.target.value)} />
    <button type="submit" disabled={!canWrite || busy || !question.trim()}>Make board</button>
    {error && <p role="alert" className="org-error">{error}</p>}
  </form></section>;

  return <section className="org-board" aria-label="Board">
    <header className="org-board-bar">
      <nav className="org-board-path" aria-label="Board path">
        {board?.path.map(step => <React.Fragment key={step.board_id}><button type="button" onClick={() => { setBoard(null); setBoardId(step.board_id); }}>
          {step.question}</button><span aria-hidden="true">›</span></React.Fragment>)}
        <h2>{board?.question ?? "Loading…"}</h2>
        {!board?.path.length && boards.length > 1 && <select aria-label="Choose a board" value={boardId} onChange={event => { setBoard(null); setBoardId(event.target.value); }}>
          {boards.map(item => <option key={item.id} value={item.id}>{item.question}</option>)}</select>}
      </nav>
      <div className="org-board-tools">
        <span className="org-board-overlays" role="group" aria-label="Overlays">{OVERLAYS.map(overlay => <button type="button" key={overlay.id}
          aria-pressed={overlays.has(overlay.id)} title={overlay.hint} onClick={() => setOverlays(current => {
            const next = new Set(current);
            if (next.has(overlay.id)) next.delete(overlay.id); else next.add(overlay.id);
            return next;
          })}>{overlay.label}</button>)}</span>
        <span className="org-board-zoom">
          <button type="button" aria-label="Zoom out" title="Zoom out · −" onClick={() => zoomAt(1 / 1.25)}>−</button>
          <output aria-label="Zoom">{Math.round(view.zoom * 100)}%</output>
          <button type="button" aria-label="Zoom in" title="Zoom in · +" onClick={() => zoomAt(1.25)}>+</button>
          <button type="button" title="Fit the board · 0" onClick={fit}>Fit</button>
        </span>
        {canWrite && board && <button type="button" className="org-board-arrange" disabled={busy || Boolean(arranging) || Boolean(unsupported)}
          title={unsupported || "The model sets rank, maturity, themes, promise and effort, adds missing steps and lays out the routes"}
          onClick={() => void arrange()}>{arranging ? `Arranging with ${modelName(arranging.model)}…` : `Arrange with ${modelName(model || "a model")}`}</button>}
      </div>
    </header>
    {board && !board.arranged_at && board.cards.length > 1 && !arranging && <p className="org-board-hint">
      Link cards into routes yourself, or arrange the board to have the model rank, ring, theme and place them.</p>}
    {board?.arranging?.status === "succeeded" && !board.arranging.applied_at && <p role="status" className="org-board-hint">
      {modelName(board.arranging.model)} finished without arranging the board. Arrange again, or choose a model that uses tools.</p>}
    {board?.arranging?.status === "failed" && <p role="status" className="org-board-hint">The last arrangement did not finish
      {board.arranging.error_code ? ` (${board.arranging.error_code})` : ""}. Arrange again to retry.</p>}
    <div className="org-board-viewport" ref={viewport} tabIndex={0} aria-label="Board map" onKeyDown={event => {
      if (event.target !== event.currentTarget) return;
      const step = 60;
      if (event.key === "+" || event.key === "=") zoomAt(1.25);
      else if (event.key === "-") zoomAt(1 / 1.25);
      else if (event.key === "0") fit();
      else if (event.key.startsWith("Arrow")) moveView(current => ({ ...current,
        x: current.x + (event.key === "ArrowLeft" ? step : event.key === "ArrowRight" ? -step : 0),
        y: current.y + (event.key === "ArrowUp" ? step : event.key === "ArrowDown" ? -step : 0) }));
      else if (event.key === "Escape") setSelected(null);
      else return;
      event.preventDefault();
    }} onPointerDown={event => {
      if (event.button !== 0 || (event.target as HTMLElement).closest(".org-board-card, .org-board-details")) return;
      drag.current = { x: event.clientX, y: event.clientY, view };
      event.currentTarget.setPointerCapture(event.pointerId);
    }} onPointerMove={event => {
      const start = drag.current;
      if (start) moveView(() => ({ ...start.view, x: start.view.x + event.clientX - start.x, y: start.view.y + event.clientY - start.y }));
    }} onPointerUp={event => {
      const start = drag.current;
      drag.current = null;
      if (start && Math.hypot(event.clientX - start.x, event.clientY - start.y) < 4) setSelected(null);
    }}>
      {board && layout && <div className="org-board-world" style={{ transform: `translate(${view.x}px, ${view.y}px) scale(${view.zoom})` }}>
        {overlays.has("themes") && regions.map(region => <div key={region.theme} className="org-board-theme" style={place(region.box)}>
          <span>{region.theme}</span></div>)}
        <Links layout={layout} content={board} marker={"board-arrow-" + board.id} />
        <div className="org-board-question" style={place(layout.question)}><span>{board.path.length ? "Board of a card" : "Question"}</span>
          <p>{board.question}</p></div>
        {board.cards.map(card => {
          const box: Box = layout.cards[card.id], child = children[card.id];
          const chosen = selected?.board === board.id && selected.card === card.id, aimed = !selected && focus?.board === board.id && focus.card === card.id;
          const open = Boolean(child && view.zoom >= INNER_ZOOM && child.content.cards.length);
          return <article key={card.id} data-card-id={card.id} tabIndex={0} aria-label={card.text}
            className={"org-board-card org-board-card-" + card.kind + (overlays.has("rings") && card.maturity ? " org-board-ring-" + card.maturity : "")
              + (chosen ? " is-selected" : "") + (aimed ? " is-aimed" : "") + (open ? " is-open" : "")}
            style={place(box)}
            onClick={() => setSelected({ board: board.id, card: card.id })} onDoubleClick={() => zoomToCard(card.id)}
            onKeyDown={event => { if (event.key === "Enter") setSelected({ board: board.id, card: card.id }); }}>
            {overlays.has("rank") && card.rank !== null && <span className="org-board-rank" aria-label={"Rank " + card.rank}>{card.rank}</span>}
            <p className="org-board-card-text">{card.text}</p>
            {!open && <footer><Marks card={card} overlays={overlays} />
              {card.board && card.board.cards > 0 && <span className="org-board-inside">{card.board.cards} card{card.board.cards === 1 ? "" : "s"}
                {card.board.gaps ? ` · ${card.board.gaps} gap${card.board.gaps === 1 ? "" : "s"}` : ""} inside</span>}</footer>}
            {open && child && <InnerBoard content={child.content} layout={child.layout} zoom={view.zoom} selected={selected} onSelect={setSelected} />}
          </article>;
        })}
      </div>}
      {board && !board.cards.length && <p className="org-board-empty org-board-empty-cards">Add the first idea with + Add idea below.</p>}
      {selectedCard && selectedBoard && <aside className="org-board-details" aria-label="Card details">
        <header><strong>{selectedCard.kind === "gap" ? "Missing step" : "Idea"}{selectedBoard.id !== board?.id ? " inside a card" : ""}</strong>
          <button type="button" aria-label="Close card details" onClick={() => setSelected(null)}>×</button></header>
        <p>{selectedCard.text}</p>
        {(selectedCard.rank !== null || selectedCard.maturity || selectedCard.theme) && <p className="org-board-facts">
          {[selectedCard.rank !== null ? `Rank ${selectedCard.rank}` : "", selectedCard.maturity ?? "", selectedCard.theme ?? "",
            quadrant(selectedCard) !== null ? QUADRANTS[quadrant(selectedCard)!] : ""].filter(Boolean).join(" · ")}</p>}
        {selectedCard.reason && <p className="org-board-reason">{selectedCard.reason}</p>}
        {selectedCard.statements.length > 0 && <p className="org-board-facts">Ledger: {selectedCard.statements.join(", ")}</p>}
        <div className="org-board-actions">
          <button type="button" onClick={() => void openBoard(selected!)} disabled={busy || (!canWrite && !selectedCard.board)}>
            {selectedCard.board ? "Open its board" : "Open as a board"}</button>
          {canWrite && <span className="org-board-link-form">
            <label className="org-visually-hidden" htmlFor="org-board-link-target">Leads to</label>
            <select id="org-board-link-target" value={linkTarget} onChange={event => setLinkTarget(event.target.value)}>
              <option value="question">the question</option>
              {selectedBoard.cards.filter(card => card.id !== selectedCard.id).map(card => <option key={card.id} value={card.id}>{card.text}</option>)}
              <option value="evidence">evidence for the question</option>
            </select>
            <button type="button" disabled={busy} onClick={() => void change(boardPath(selectedBoard.id) + "/links", "POST", {
              source_card_id: selectedCard.id, ...(linkTarget === "question" || linkTarget === "evidence" ? {} : { target_card_id: linkTarget }),
              kind: linkTarget === "evidence" ? "evidence" : "route" })}>Link</button></span>}
          {canWrite && <button type="button" disabled={busy} onClick={() => void change(boardPath(selectedBoard.id) + "/cards/"
            + encodeURIComponent(selectedCard.id), "DELETE").then(next => { if (next) setSelected(null); })}>Remove</button>}
        </div>
      </aside>}
    </div>
    {error && <p role="alert" className="org-error org-board-error">{error}</p>}
    {notice && <p role="status" className="org-board-notice">{notice}</p>}
    <div className="org-board-footer">
      {canWrite && board && (idea === null ? <button type="button" className="org-board-add" onClick={() => setIdea("")}>+ Add idea</button>
        : <form className="org-board-idea" onSubmit={event => void addIdea(event)}>
          <label className="org-visually-hidden" htmlFor="org-board-idea">New idea</label>
          <input id="org-board-idea" autoFocus value={idea} maxLength={1000} placeholder={focus && focus.board !== board.id ? "An idea inside the card in view…" : "An idea toward the question…"}
            onChange={event => setIdea(event.target.value)} onKeyDown={event => { if (event.key === "Escape") setIdea(null); }} />
          <button type="submit" disabled={busy || !idea.trim()}>Add</button><button type="button" onClick={() => setIdea(null)}>Cancel</button>
        </form>)}
      <form className="org-board-ask" onSubmit={event => void submitAsk(event)}>
        <label className="org-visually-hidden" htmlFor="org-board-ask">Ask about a card</label>
        <input id="org-board-ask" value={ask} maxLength={16000} disabled={!canWrite || !targetCard}
          placeholder={targetCard ? `Ask privately about “${targetCard.text}”, the card ${selected ? "you selected" : "most in view"}…` : "Add a card to ask about it…"}
          onChange={event => setAsk(event.target.value)} />
        <button type="submit" disabled={!canWrite || busy || !ask.trim() || !targetCard}>Ask</button>
      </form>
    </div>
  </section>;
}
