import React, { memo, useCallback, useEffect, useId, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { ModelPrice, ModelBenchmarks, OrganizationPrices, request, errorMessage } from "./organization-api";
import { formatModelPrice } from "./modelPriceFormat";
import { catalogModel, chatModel, usefulChatModel } from "./modelRecommendations";
import { availableEfforts } from "./ReasoningEffort";

export function modelSelectionLabel(catalog: OrganizationPrices | null, model: string, effort = "") {
  const metadata = catalogModel(catalog, model);
  const label = metadata?.execution_source === "bot" ? metadata.name : model;
  return label + (availableEfforts(catalog, model).includes(effort) ? "/" + effort : "");
}

export function compactTokens(value: number | null | undefined): string {
  if (value == null) return "—";
  if (value >= 1_000_000) return `${Number((value / 1_000_000).toFixed(1))}M`;
  if (value >= 1000) return `${Number((value / 1000).toFixed(value >= 10000 ? 0 : 1))}k`;
  return String(value);
}

export function benchmarkScores(id: string, model: ModelPrice | undefined, snapshot: ModelBenchmarks | undefined) {
  // Only exact upstream identities; no stripped variants, fuzzy names, or vendor guesses.
  return snapshot?.models[id] ?? (model?.canonical_slug ? snapshot?.models[model.canonical_slug] : undefined);
}

/** Chat-model IDs whose ID or catalog name contains the query, deduplicated and sorted. */
function matchingModels(ids: string[], catalog: OrganizationPrices | null, query: string) {
  const needle = query.trim().toLowerCase();
  return [...new Set(ids)].filter(id => chatModel(id) && (id + " " + (catalogModel(catalog, id)?.name ?? "")).toLowerCase().includes(needle)).sort();
}

export function modelSections(ids: string[], catalog: OrganizationPrices | null, query = "", favorites: string[] = []) {
  const math = new Set(catalog?.recommendations?.model_ids ?? []), favorite = new Set(favorites);
  const seen = new Set<string>();
  const candidates = matchingModels(ids, catalog, query);
  const section = (title: string, test: (id: string) => boolean) => {
    const choices = candidates.filter(id => !seen.has(id) && test(id)); choices.forEach(id => seen.add(id));
    return { title, ids: choices };
  };
  return [section("Favorites", id => favorite.has(id)), section("Your connected bots", id => catalogModel(catalog, id)?.execution_source === "bot"), section("Best for math", id => math.has(id)), section("Best open-weights", id => !!catalogModel(catalog, id)?.weights_repository),
    section("Other models", () => true)].filter(group => group.ids.length);
}

export function ModelLine({ id, model, allowed, math, scores }: { id: string; model?: ModelPrice; allowed: boolean; math: boolean; scores?: OrganizationPrices["benchmarks"] }) {
  if (model?.execution_source === "bot") return <span className="org-model-line"><strong>{model.name}</strong>
    <span className={model.bot_online === false ? "org-model-unavailable" : "org-model-available"} title={model.bot_online === false
      ? "Bot offline; start its local runner before requesting a reply" : "Advertised by a bot; CLI account capacity is checked locally"}>{model.bot_online === false ? "○" : "✓"}</span>
    <span className="org-model-tags">{model.provider}, local agent, tools</span></span>;
  const tags = [math && "math", model?.weights_repository && "open-weights", model?.supports_reasoning && "reasoning", model?.supports_tools && "tools",
    model?.input_modalities?.includes("image") && "vision"].filter(Boolean).join(", ");
  const indices = benchmarkScores(id, model, scores);
  return <span className="org-model-line"><strong title={id}>{id}</strong>
    <span className={allowed && model ? "org-model-available" : "org-model-unavailable"} title={allowed && model
      ? "Listed by OpenRouter; not a live provider or credit check" : allowed ? "Catalog metadata unavailable" : "Not in the current model catalog"}>
      {allowed && model ? "✓" : allowed ? "?" : "×"}</span>
    {tags && <span className="org-model-tags">{tags}</span>}
    {model?.routing_availability?.zdr && <span className="org-model-route" title="ZDR endpoints available; enforcement is chosen in organization provider routing">zdr</span>}
    {model?.routing_availability?.regions.map(region => <span className="org-model-route" key={region} title={`${region} regional endpoints available; enforcement is chosen in organization provider routing`}>
      {region === "US" ? "🇺🇸" : region === "EU" ? "🇪🇺" : region}</span>)}
    <span className="org-model-price" title="Advertised input/output USD per million tokens">{formatModelPrice(model?.input_usd_per_million ?? null)}/{formatModelPrice(model?.output_usd_per_million ?? null).replace(/^\$/, "")}</span>
    <span className="org-model-capacity" title="Context / maximum output tokens">{compactTokens(model?.context_length)}/{compactTokens(model?.max_completion_tokens)}</span>
    {indices && <span className="org-model-scores" title={`Artificial Analysis intelligence/agentic indices via OpenRouter · ${scores?.meta?.as_of ?? ""} · ${scores?.status}`}>
      {indices.intelligence_index != null && <span aria-label={`Intelligence index ${indices.intelligence_index}`}>I {indices.intelligence_index.toFixed(1)}</span>}
      {indices.agentic_index != null && <span aria-label={`Agentic index ${indices.agentic_index}`}>A {indices.agentic_index.toFixed(1)}</span>}
    </span>}
  </span>;
}

export function ModelDetails({ id, model, catalog }: { id: string; model?: ModelPrice; catalog: OrganizationPrices | null }) {
  if (model?.execution_source === "bot") return <p className="org-help">Uses the bot owner's CLI account, not the organization's OpenRouter key.
    CLI charges are not measured here. Local execution requires owner approval; no OpenRouter fallback is made.</p>;
  const evidence = catalog?.recommendations?.evidence?.filter(item => item.model_id === id) ?? [];
  const scores = benchmarkScores(id, model, catalog?.benchmarks);
  return <div className="org-model-detail">
    {model?.description && <p>{model.description}</p>}
    <p>Input/output prices are USD per million tokens. Context/output figures are provider capacities, not application limits.</p>
    {model && <p>Context {model.context_length?.toLocaleString() ?? "—"} / maximum output {model.max_completion_tokens?.toLocaleString() ?? "—"} tokens.</p>}
    {model && <p>{model.input_modalities?.join(", ")} → {model.output_modalities?.join(", ")} · {model.supported_parameters?.join(", ")}</p>}
    {scores && <p>Artificial Analysis indices: intelligence {scores.intelligence_index ?? "—"} · agentic {scores.agentic_index ?? "—"} · coding {scores.coding_index ?? "—"}.
      {" "}These benchmarks are not research correctness guarantees.</p>}
    {model?.weights_repository && <p><a href={`https://huggingface.co/${model.weights_repository}`} target="_blank" rel="noreferrer">Weights repository reported by OpenRouter</a></p>}
    {evidence.map(item => <p key={item.model_id}><a href={item.evidence_url} target="_blank" rel="noreferrer">Mathematical evaluation</a> · {item.evaluated_setting}</p>)}
    {model?.routing_availability && <p>{model.routing_availability.source}. Regional residency may require an eligible OpenRouter plan and regional API origin.
      Badges describe available routes, not the enforced policy. Choose ZDR and region in organization provider routing.</p>}
    {model?.routing_availability && <p>Available routing options: {[
      model.routing_availability.zdr && "ZDR", ...model.routing_availability.regions,
    ].filter(Boolean).join(", ") || "Not reported"}.</p>}
    {model && <dl>{[["Cache read / 1M", model.cache_read_usd_per_million], ["Cache write / 1M", model.cache_write_usd_per_million],
      ["Reasoning / 1M", model.reasoning_usd_per_million], ["Per request", model.request_usd], ["Per image", model.image_usd],
      ["Per web search", model.web_search_usd]].filter(([, value]) => value != null && value !== 0).map(([label, value]) =>
        <div key={String(label)}><dt>{label}</dt><dd>{formatModelPrice(value as number)}</dd></div>)}</dl>}
  </div>;
}

/*
 * The signed-in person's favorites, shared by every mounted picker: one read when the first picker mounts, again when
 * the window regains focus (another device may have changed them), and the server's answer after each toggle.
 */
let favoriteModels: string[] = [];
let favoriteRead: AbortController | null = null;
const favoriteListeners = new Set<() => void>();
const noFavorites: string[] = [];
function publishFavorites(models: string[]) { favoriteModels = models; favoriteListeners.forEach(listener => listener()); }
function readFavorites() {
  favoriteRead?.abort();
  const controller = favoriteRead = new AbortController();
  void request<{ user_id: string; models: string[] }>("/auth/model-favorites", { signal: controller.signal })
    .then(value => { if (!controller.signal.aborted && Array.isArray(value.models)) publishFavorites(value.models); })
    .catch(() => { /* Selecting models does not require preferences. */ })
    .finally(() => { if (favoriteRead === controller) favoriteRead = null; });
}
function subscribeFavorites(listener: () => void) {
  favoriteListeners.add(listener);
  if (favoriteListeners.size === 1) { readFavorites(); window.addEventListener("focus", readFavorites); }
  return () => {
    favoriteListeners.delete(listener);
    if (favoriteListeners.size) return;
    // The next subscriber may belong to another account; it starts empty, as a fresh picker did.
    window.removeEventListener("focus", readFavorites); favoriteRead?.abort(); favoriteRead = null; favoriteModels = noFavorites;
  };
}
const ignoreFavorites = () => () => { /* Nothing to release. */ };
/** Favorite model IDs; `enabled` false neither reads nor subscribes. */
function useModelFavorites(enabled = true): string[] {
  return useSyncExternalStore(enabled ? subscribeFavorites : ignoreFavorites, () => enabled ? favoriteModels : noFavorites, () => noFavorites);
}
async function toggleFavorite(id: string, favorite: boolean) {
  favoriteRead?.abort(); favoriteRead = null; // An older read must not overwrite this answer.
  const value = await request<{ user_id: string; models: string[] }>("/auth/model-favorites", { method: "PUT", body: { model_id: id, favorite } });
  publishFavorites(value.models);
}

export function ModelRows({ ids, catalog, allowedModels, selected, onSelect, name, disabled = false, query = "", grouped = true, empty }: {
  ids: string[]; catalog: OrganizationPrices | null; allowedModels: string[]; selected?: string; onSelect?: (id: string) => void;
  name?: string; disabled?: boolean; query?: string; grouped?: boolean; empty?: React.ReactNode;
}) {
  // Favorites order grouped pickers and are toggled from selectable rows; the ungrouped browser needs neither.
  const favorites = useModelFavorites(grouped || !!onSelect);
  const [favoriteError, setFavoriteError] = useState(""), [savingFavorite, setSavingFavorite] = useState(false);
  const favoriteSet = useMemo(() => new Set(favorites), [favorites]);
  const allowed = useMemo(() => new Set(allowedModels), [allowedModels]);
  const math = useMemo(() => new Set(grouped ? catalog?.recommendations?.model_ids ?? [] : []), [catalog, grouped]);
  const groups = useMemo(() => grouped ? modelSections(ids, catalog, query, favorites) : [{ title: "Models", ids: matchingModels(ids, catalog, query) }],
    [ids, catalog, query, favorites, grouped]);
  // Rows are memoized, so their callbacks stay stable while always calling the latest handlers.
  const latest = useRef({ onSelect, favoriteSet });
  latest.current = { onSelect, favoriteSet };
  const select = useCallback((id: string) => latest.current.onSelect?.(id), []);
  const favorite = useCallback(async (id: string) => {
    setSavingFavorite(true); setFavoriteError("");
    try { await toggleFavorite(id, !latest.current.favoriteSet.has(id)); }
    catch (cause) { setFavoriteError(errorMessage(cause)); } finally { setSavingFavorite(false); }
  }, []);
  const count = useRowBudget(groups.reduce((total, group) => total + group.ids.length, 0));
  let remaining = count;
  const shown = groups.map(group => { const ids = group.ids.slice(0, Math.max(0, remaining)); remaining -= group.ids.length; return { ...group, ids }; });
  return <div className="org-model-rows">{!count && empty}{shown.map(group => group.ids.length > 0 && <section key={group.title}>
    <h5>{group.title}</h5>{group.ids.map(id => <ModelRow key={id} id={id} catalog={catalog} allowed={allowed.has(id)} math={math.has(id)}
      selectable={!!onSelect} selected={selected === id} favorite={favoriteSet.has(id)} savingFavorite={savingFavorite} disabled={disabled}
      name={name} onSelect={select} onFavorite={favorite} />)}</section>)}
    {favoriteError && <p role="alert" className="org-error">{favoriteError}</p>}</div>;
}

/**
 * How many rows to mount now. A long list mounts one batch at once and a batch per tick after that, so opening a picker
 * or the capabilities browser never blocks on the whole catalog; every row is mounted shortly after (for find in page,
 * Tab order and screen readers). A narrowed search lowers the budget, so widening it again also grows in batches.
 */
function useRowBudget(total: number, batch = 40) {
  const [count, setCount] = useState(batch);
  useEffect(() => {
    if (count > Math.max(batch, total)) { setCount(Math.max(batch, total)); return; }
    if (count >= total) return;
    const timer = window.setTimeout(() => setCount(current => Math.min(current + batch, total)), 30);
    return () => window.clearTimeout(timer);
  }, [count, total, batch]);
  return Math.min(count, total);
}

const ModelRow = memo(function ModelRow({ id, catalog, allowed, math, selectable, selected, favorite, savingFavorite, disabled, name, onSelect, onFavorite }: {
  id: string; catalog: OrganizationPrices | null; allowed: boolean; math: boolean; selectable: boolean; selected: boolean; favorite: boolean;
  savingFavorite: boolean; disabled: boolean; name?: string; onSelect: (id: string) => void; onFavorite: (id: string) => void;
}) {
  const model = catalogModel(catalog, id);
  const line = <ModelLine id={id} model={model} allowed={allowed} math={math} scores={catalog?.benchmarks} />;
  return <ModelCard details={<ModelDetails id={id} model={model} catalog={catalog} />}>
    <summary>{selectable && <button type="button" className="org-model-favorite" aria-label={`Favorite ${id}`} aria-pressed={favorite}
      disabled={savingFavorite} onClick={event => { event.preventDefault(); event.stopPropagation(); onFavorite(id); }}>{favorite ? "★" : "☆"}</button>}
      {selectable ? <label onClick={event => event.stopPropagation()}><input type="radio" name={name} aria-label={id} checked={selected}
        disabled={disabled || !allowed} onChange={() => { /* Selection also opens effort for the current model. */ }}
        onClick={() => onSelect(id)} />{line}</label> : line}
      <span className="org-model-chevron" aria-hidden="true">⌄</span></summary>
  </ModelCard>;
});

/**
 * A model row whose capability details (thousands of elements across a catalog) mount when the row is first
 * expanded. A click on the row's summary mounts them before the browser opens it; toggle covers other openings.
 */
function ModelCard({ children, details }: { children: React.ReactNode; details: React.ReactNode }) {
  const [shown, setShown] = useState(false);
  return <details className="org-model-card" onClick={() => setShown(true)}
    onToggle={event => { if (event.target === event.currentTarget && event.currentTarget.open) setShown(true); }}>
    {children}{shown && details}
  </details>;
}

/** A reasoning effort as people read it: "" is the model's default. */
export function effortLabel(effort: string): string {
  return effort === "" ? "Default effort" : effort === "xhigh" ? "Extra high" : effort[0].toUpperCase() + effort.slice(1);
}

export function EffortMenu({ model, catalog, effort, disabled, onBack, onSelect, checks = false }: {
  model: string; catalog: OrganizationPrices | null; effort: string; disabled: boolean;
  /** Returns to the model list; without it the menu stands alone. */
  onBack?: () => void; onSelect: (effort: string) => void;
  /** A plain menu beside the model picker: no model name, and a check mark on the current level instead of radio buttons. */
  checks?: boolean;
}) {
  const group = useRef<HTMLDivElement>(null), name = useId();
  const choices = ["", ...availableEfforts(catalog, model)];
  const selected = choices.includes(effort) ? effort : "";
  useEffect(() => { group.current?.querySelector<HTMLInputElement>("input:checked")?.focus(); }, [model]);
  return <section className={checks ? "org-effort-options org-effort-checks" : "org-effort-options"} aria-label="Choose reasoning effort">
    {!checks && <header>{onBack && <button type="button" aria-label="Back to models" onClick={onBack}>←</button>}
      <strong title={modelSelectionLabel(catalog, model)}>{modelSelectionLabel(catalog, model)}</strong></header>}
    <div ref={group} role="radiogroup" aria-label="Reasoning effort">{choices.map(choice => <label key={choice}>
      <input type="radio" name={name} value={choice} checked={choice === selected} disabled={disabled}
        aria-label={choice || "Default effort"} onChange={() => onSelect(choice)}
        onClick={() => { if (choice === selected) onSelect(choice); }} />
      {checks && <span className="org-effort-check" aria-hidden="true">{choice === selected ? "✓" : ""}</span>}
      <span>{checks ? effortLabel(choice).replace(" effort", "") : effortLabel(choice)}</span>
    </label>)}</div>
  </section>;
}

/** The reasoning effort under the message box, at its far right; a model without effort levels shows none. */
export function EffortPicker({ model, catalog, effort, disabled = false, onSelect }: {
  model: string; catalog: OrganizationPrices | null; effort: string; disabled?: boolean; onSelect: (effort: string) => void;
}) {
  const [open, setOpen] = useState(false), box = useRef<HTMLDivElement>(null), button = useRef<HTMLButtonElement>(null);
  const choices = availableEfforts(catalog, model);
  useEffect(() => {
    if (!open) return;
    const close = (event: PointerEvent) => { if (!box.current?.contains(event.target as Node)) setOpen(false); };
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); setOpen(false); button.current?.focus(); } };
    document.addEventListener("pointerdown", close); document.addEventListener("keydown", escape);
    return () => { document.removeEventListener("pointerdown", close); document.removeEventListener("keydown", escape); };
  }, [open]);
  useEffect(() => { if (disabled) setOpen(false); }, [disabled]);
  if (!choices.length) return null;
  const label = effortLabel(choices.includes(effort) ? effort : "");
  return <div className="org-effort-picker" ref={box}>
    <button type="button" ref={button} className="org-composer-pick org-composer-effort" aria-label={`Reasoning effort: ${label}`} title="Reasoning effort"
      aria-haspopup="true" aria-expanded={open} disabled={disabled} onClick={() => setOpen(value => !value)}>{label.replace(" effort", "")}</button>
    {open && <div className="org-model-options org-effort-popover"><EffortMenu model={model} catalog={catalog} effort={effort} disabled={disabled} checks
      onSelect={choice => { onSelect(choice); setOpen(false); button.current?.focus(); }} /></div>}
  </div>;
}

export function ModelPicker({ label, models, catalog, value, onChange, disabled = false, children, emptyLabel = "Choose model", effort = "", onEffortChange }: {
  label: string; models: string[]; catalog: OrganizationPrices | null; value: string; onChange: (id: string) => void;
  disabled?: boolean; children?: React.ReactNode; emptyLabel?: string;
  effort?: string; onEffortChange?: (effort: string) => void;
}) {
  const [open, setOpen] = useState(false), [query, setQuery] = useState(""), [showAll, setShowAll] = useState(false);
  const [effortModel, setEffortModel] = useState<string | null>(null);
  // Read favorites while closed so the first opening already groups them.
  useModelFavorites();
  // Options, and the catalog work behind them, exist only while the picker is open.
  const choices = useMemo(() => open ? models.filter(id => showAll || id === value || usefulChatModel(id, catalogModel(catalog, id))) : [],
    [open, models, showAll, value, catalog]);
  const [placement, setPlacement] = useState<React.CSSProperties>({});
  const menu = useRef<HTMLDetailsElement>(null), name = useId();
  const place = () => {
    const bounds = menu.current?.getBoundingClientRect(); if (!bounds) return;
    const width = Math.min(720, window.innerWidth - 24), below = window.innerHeight - bounds.bottom - 16;
    const above = bounds.top - 16;
    setPlacement({ position: "fixed", width, left: Math.max(12, Math.min(bounds.left, window.innerWidth - width - 12)), right: "auto",
      top: below >= Math.min(320, above) ? bounds.bottom + 8 : "auto", bottom: below >= Math.min(320, above) ? "auto" : window.innerHeight - bounds.top + 8,
      maxHeight: Math.max(100, Math.min(520, Math.max(below, above))) });
  };
  const effortPlacement = { ...placement, width: Math.min(320, Number(placement.width) || 320) };
  useEffect(() => {
    if (!open) return;
    const close = (event: PointerEvent) => { if (!menu.current?.contains(event.target as Node)) setOpen(false); };
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") {
      event.preventDefault(); event.stopPropagation(); setOpen(false); menu.current?.querySelector<HTMLElement>("summary")?.focus();
    } };
    document.addEventListener("pointerdown", close); document.addEventListener("keydown", escape);
    window.addEventListener("resize", place);
    return () => { document.removeEventListener("pointerdown", close); document.removeEventListener("keydown", escape); window.removeEventListener("resize", place); };
  }, [open]);
  useEffect(() => { if (disabled) setOpen(false); }, [disabled]);
  return <details className="org-model-menu org-shared-model-picker" ref={menu} open={open}>
    <summary role="button" aria-label={label} aria-expanded={open} aria-disabled={disabled}
      onClick={event => { event.preventDefault(); if (!disabled) { place(); setEffortModel(null); setOpen(!open); } }}><span>{value ? modelSelectionLabel(catalog, value, effort) : emptyLabel}</span><span aria-hidden="true">⌃</span></summary>
    {open && <div className="org-model-options" style={effortModel ? effortPlacement : placement}>{effortModel && onEffortChange ? <EffortMenu
      model={effortModel} catalog={catalog} effort={effort} disabled={disabled} onBack={() => setEffortModel(null)}
      onSelect={choice => { onEffortChange(choice); setOpen(false); menu.current?.querySelector<HTMLElement>("summary")?.focus(); }} />
      : <><input type="search" aria-label="Search models" placeholder="Search models and routers" value={query} onChange={event => setQuery(event.target.value)} />
      <label className="org-model-choice"><input type="checkbox" checked={showAll} onChange={event => setShowAll(event.target.checked)} />All chat models</label>
      <ModelRows ids={choices} catalog={catalog} allowedModels={models} selected={value} name={name} disabled={disabled} query={query}
        empty={<p>{models.length ? "No matching models." : "No models are enabled."}</p>}
        onSelect={id => { onChange(id); if (onEffortChange && availableEfforts(catalog, id).length) setEffortModel(id); else setOpen(false); }} />
      {children}
      </>}
    </div>}
  </details>;
}
