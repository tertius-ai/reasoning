import { ModelPrice, OrganizationPrices } from "./organization-api";

/** Chat selectors omit transport-only aliases; admission still accepts live router IDs. */
export function chatModel(id: string): boolean {
  return !/(?:^|[/:_-])(?:batch|embedding|embeddings|rerank|reranker)(?:$|[/:_-])/i.test(id);
}

/** Default to agent-capable entries without turning the catalog into an admission whitelist. */
export function usefulChatModel(id: string, model?: ModelPrice): boolean {
  return chatModel(id) && (!model || model.execution_source === "bot" || id.startsWith("openrouter/")
    || model.supports_tools == null && model.supports_reasoning == null
    || model.supports_tools === true || model.supports_reasoning === true);
}

const catalogIndexes = new WeakMap<OrganizationPrices, Map<string, ModelPrice>>();

/** A catalog entry by ID, from an index built once per catalog object (the first entry wins, as with `find`). */
export function catalogModel(catalog: OrganizationPrices | null | undefined, id: string): ModelPrice | undefined {
  if (!catalog) return undefined;
  let index = catalogIndexes.get(catalog);
  if (!index) {
    index = new Map();
    for (const model of catalog.models) if (!index.has(model.id)) index.set(model.id, model);
    catalogIndexes.set(catalog, index);
  }
  return index.get(id);
}

export function recommendedAllowedModels(catalog: OrganizationPrices | null): ModelPrice[] {
  const allowed = new Set(catalog?.allowed_models ?? []);
  return (catalog?.recommendations?.model_ids ?? []).flatMap((id) => {
    const model = catalogModel(catalog, id); return model && allowed.has(id) ? [model] : [];
  });
}

export function modelOptionGroups(allowedModels: string[], catalog: OrganizationPrices | null, query = "") {
  const allowed = new Set(allowedModels);
  const matching = (id: string) => {
    return chatModel(id) && (id + " " + (catalogModel(catalog, id)?.name ?? "")).toLowerCase().includes(query.trim().toLowerCase());
  };
  const recommended = (catalog?.recommendations?.model_ids ?? []).filter((id) => allowed.has(id) && matching(id));
  const preferred = new Set(recommended);
  return { recommended, other: allowedModels.filter((id) => !preferred.has(id) && matching(id)) };
}

/**
 * The reply model to keep selected when the allowed models change, and the model that is no longer available, if any.
 * An unavailable choice is never silently replaced: it stays unavailable until someone chooses another model, or the
 * model is allowed again. With nothing chosen, the topic's default applies, then the first allowed model.
 */
export function keptReplyModel(current: string, lost: string | null, topicDefault: string | null | undefined, allowed: string[]):
  { model: string; lost: string | null } {
  const usable = (id: string) => id.startsWith("bot/") || allowed.includes(id);
  const keep = (id: string) => usable(id) ? { model: id, lost: null } : { model: "", lost: id };
  if (current) return keep(current);
  if (lost) return keep(lost);
  if (topicDefault) return keep(topicDefault);
  return { model: allowed[0] ?? "", lost: null };
}
