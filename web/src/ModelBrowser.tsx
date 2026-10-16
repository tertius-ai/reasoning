import React, { useEffect, useId, useRef, useState } from "react";
import { ModelPrice, ModelBenchmarks, OrganizationPrices, request, errorMessage } from "./organization-api";
import { formatModelPrice } from "./modelPriceFormat";
import { chatModel, usefulChatModel } from "./modelRecommendations";
import { availableEfforts } from "./ReasoningEffort";

export function modelSelectionLabel(catalog: OrganizationPrices | null, model: string, effort = "") {
  const metadata = catalog?.models.find(item => item.id === model);
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

export function modelSections(ids: string[], catalog: OrganizationPrices | null, query = "", favorites: string[] = []) {
  const models = new Map((catalog?.models ?? []).map(model => [model.id, model]));
  const math = new Set(catalog?.recommendations?.model_ids ?? []);
  const seen = new Set<string>();
  const candidates = [...new Set(ids)].filter(id => chatModel(id) && (id + " " + (models.get(id)?.name ?? "")).toLowerCase().includes(query.trim().toLowerCase())).sort();
  const section = (title: string, test: (id: string) => boolean) => {
    const choices = candidates.filter(id => !seen.has(id) && test(id)); choices.forEach(id => seen.add(id));
    return { title, ids: choices };
  };
  return [section("Favorites", id => favorites.includes(id)), section("Your connected bots", id => models.get(id)?.execution_source === "bot"), section("Best for math", id => math.has(id)), section("Best open-weights", id => !!models.get(id)?.weights_repository),
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

export function ModelRows({ ids, catalog, allowedModels, selected, onSelect, name, disabled = false, query = "", grouped = true }: {
  ids: string[]; catalog: OrganizationPrices | null; allowedModels: string[]; selected?: string; onSelect?: (id: string) => void;
  name?: string; disabled?: boolean; query?: string; grouped?: boolean;
}) {
  const [favorites, setFavorites] = useState<string[]>([]), [favoriteError, setFavoriteError] = useState("");
  const [savingFavorite, setSavingFavorite] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    const load = () => { void request<{ user_id: string; models: string[] }>("/auth/model-favorites", { signal: controller.signal })
      .then(value => { if (Array.isArray(value.models)) setFavorites(value.models); })
      .catch(() => { /* Selecting models does not require preferences. */ }); };
    load(); window.addEventListener("focus", load); window.addEventListener("reasoning-model-favorites", load);
    return () => { controller.abort(); window.removeEventListener("focus", load); window.removeEventListener("reasoning-model-favorites", load); };
  }, []);
  const favorite = async (id: string) => {
    setSavingFavorite(true); setFavoriteError("");
    try { const value = await request<{ user_id: string; models: string[] }>("/auth/model-favorites", {
      method: "PUT", body: { model_id: id, favorite: !favorites.includes(id) } });
      setFavorites(value.models); window.dispatchEvent(new Event("reasoning-model-favorites"));
    } catch (cause) { setFavoriteError(errorMessage(cause)); } finally { setSavingFavorite(false); }
  };
  const models = new Map((catalog?.models ?? []).map(model => [model.id, model]));
  const math = new Set(grouped ? catalog?.recommendations?.model_ids ?? [] : []);
  const groups = grouped ? modelSections(ids, catalog, query, favorites) : [{ title: "Models", ids: [...new Set(ids)].filter(id => chatModel(id)
    && (id + " " + (models.get(id)?.name ?? "")).toLowerCase().includes(query.trim().toLowerCase())).sort() }];
  return <div className="org-model-rows">{groups.map(group => <section key={group.title}>
    <h5>{group.title}</h5>{group.ids.map(id => <ModelCard key={id} details={<ModelDetails id={id} model={models.get(id)} catalog={catalog} />}>
      <summary>{onSelect && <button type="button" className="org-model-favorite" aria-label={`Favorite ${id}`} aria-pressed={favorites.includes(id)}
        disabled={savingFavorite} onClick={event => { event.preventDefault(); event.stopPropagation(); void favorite(id); }}>{favorites.includes(id) ? "★" : "☆"}</button>}{onSelect ? <label onClick={event => event.stopPropagation()}><input type="radio" name={name} aria-label={id} checked={selected === id}
        disabled={disabled || !allowedModels.includes(id)} onChange={() => { /* Selection also opens effort for the current model. */ }}
        onClick={() => onSelect(id)} />
        <ModelLine id={id} model={models.get(id)} allowed={allowedModels.includes(id)} math={math.has(id)} scores={catalog?.benchmarks} /></label>
        : <ModelLine id={id} model={models.get(id)} allowed={allowedModels.includes(id)} math={math.has(id)} scores={catalog?.benchmarks} />}
        <span className="org-model-chevron" aria-hidden="true">⌄</span></summary>
    </ModelCard>)}</section>)}{favoriteError && <p role="alert" className="org-error">{favoriteError}</p>}</div>;
}

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

export function EffortMenu({ model, catalog, effort, disabled, onBack, onSelect }: {
  model: string; catalog: OrganizationPrices | null; effort: string; disabled: boolean;
  onBack: () => void; onSelect: (effort: string) => void;
}) {
  const group = useRef<HTMLDivElement>(null), name = useId();
  const choices = ["", ...availableEfforts(catalog, model)];
  const selected = choices.includes(effort) ? effort : "";
  useEffect(() => { group.current?.querySelector<HTMLInputElement>("input:checked")?.focus(); }, [model]);
  return <section className="org-effort-options" aria-label="Choose reasoning effort">
    <header><button type="button" aria-label="Back to models" onClick={onBack}>←</button>
      <strong title={modelSelectionLabel(catalog, model)}>{modelSelectionLabel(catalog, model)}</strong></header>
    <div ref={group} role="radiogroup" aria-label="Reasoning effort">{choices.map(choice => <label key={choice}>
      <input type="radio" name={name} value={choice} checked={choice === selected} disabled={disabled}
        aria-label={choice || "Default effort"} onChange={() => onSelect(choice)}
        onClick={() => { if (choice === selected) onSelect(choice); }} />
      <span>{choice === "" ? "Default effort" : choice === "xhigh" ? "Extra high" : choice[0].toUpperCase() + choice.slice(1)}</span>
    </label>)}</div>
  </section>;
}

export function ModelPicker({ label, models, catalog, value, onChange, disabled = false, children, emptyLabel = "Choose model", effort = "", onEffortChange }: {
  label: string; models: string[]; catalog: OrganizationPrices | null; value: string; onChange: (id: string) => void;
  disabled?: boolean; children?: React.ReactNode; emptyLabel?: string;
  effort?: string; onEffortChange?: (effort: string) => void;
}) {
  const [open, setOpen] = useState(false), [query, setQuery] = useState(""), [showAll, setShowAll] = useState(false);
  const [effortModel, setEffortModel] = useState<string | null>(null);
  const choices = models.filter(id => showAll || id === value || usefulChatModel(id, catalog?.models.find(model => model.id === id)));
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
    <div className="org-model-options" style={effortModel ? effortPlacement : placement}>{effortModel && onEffortChange ? <EffortMenu
      model={effortModel} catalog={catalog} effort={effort} disabled={disabled} onBack={() => setEffortModel(null)}
      onSelect={choice => { onEffortChange(choice); setOpen(false); menu.current?.querySelector<HTMLElement>("summary")?.focus(); }} />
      : <><input type="search" aria-label="Search models" placeholder="Search models and routers" value={query} onChange={event => setQuery(event.target.value)} />
      <label className="org-model-choice"><input type="checkbox" checked={showAll} onChange={event => setShowAll(event.target.checked)} />All chat models</label>
      <ModelRows ids={choices} catalog={catalog} allowedModels={models} selected={value} name={name} disabled={disabled} query={query}
        onSelect={id => { onChange(id); if (onEffortChange && availableEfforts(catalog, id).length) setEffortModel(id); else setOpen(false); }} />
      {!models.length && <p>No models are enabled.</p>}{models.length > 0 && !modelSections(choices, catalog, query).length && <p>No matching models.</p>}
      {children}
      </>}
    </div>
  </details>;
}
