import { recordFailedRequest } from "./diagnostics";
import { observeRelease } from "./release";
export type Account = { id: string; name: string; email: string; email_verified_at?: number | null };
export type Role = "owner" | "admin" | "member" | "viewer";
export type Organization = { id: string; name: string; role: Role };
export type Project = { id: string; name: string; visibility?: "organization" | "private"; project_role?: "owner" | "member" | null };
export type ProjectMember = Account & { role: "owner" | "member" };
export type Channel = { id: string; name: string; project_id: string };
export type TopicKind = "alpha" | "beta" | "gamma";
export type KnowledgeTool = "ledger" | "bibliography" | "journal" | "workflows";
export type Topic = { id: string; name: string; channel_id: string; kind: TopicKind; default_model: string | null;
  default_effort?: string | null; knowledge_tools: KnowledgeTool[] };
export type Presence = {
  humans: (Account & { role: Role; online: boolean; last_seen_at: number | null })[];
  bots: { id: string; name: string; owner_user_id: string; online: boolean; last_seen_at: number | null; activity?: string | null;
    developer?: boolean; engine?: string | null; models?: string[];
    /** Only for your own bots: effort levels per model, and whether the bot may run local tools. */
    efforts?: Record<string, string[]>; local_tools?: boolean }[];
};
export type KnowledgeKind = "ledger" | "bibliography" | "journal";
export type KnowledgeEntry = {
  published_revision?: number | null;
  id: string; organization_id: string; kind: KnowledgeKind; scope_kind: "organization" | "project" | "topic";
  project_id: string | null; topic_id: string | null; created_by: string; created_by_kind: "human" | "bot";
  created_at: number; revision: number; head_revision: number; title: string; body: string; state: string;
  revision_by: string; revision_by_kind: "human" | "bot"; revised_at: number; author_name: string;
};
export type TopicState = {
  topic_id: string; subscribed: boolean; last_read_sequence: number; last_message_sequence: number;
  unread_count: number; unread_mention_count: number;
};
/** The signed-in member's view of one of their direct conversations. */
export type DirectConversation = {
  id: string; created_at: number; other_user: { id: string; name: string; kind?: "human" | "bot" }; other_member: boolean;
  muted: boolean; blocked: boolean; last_read_sequence: number; last_message_sequence: number;
  last_message_at: number | null; unread_count: number;
};
export type Hierarchy = { projects: Project[]; channels: Channel[]; topics: Topic[]; topic_state: TopicState[];
  direct_conversations?: DirectConversation[] };
export type Member = Account & { role: Role };
/** A later topic message added to a running model reply, and the step of that reply which read it. */
export type ReplyAddition = { message_id: string; run_id: string; added_by: string; moved: boolean; cancelled_runs: string[];
  created_at: number; read_at: number | null; read_step: number | null;
  /** The added message's text, sent with a reply's additions so its card can show them. */
  content?: string };
export type Message = { id: string; sequence: number; topic_id: string; author_id: string; content: string; client_id: string; created_at: number;
  reactions?: import("./ChatFeedback").Feedback;
  /** The message of the same topic that this one answers. */
  reply_to?: string | null;
  revision?: number; edited_at?: number | null;
  agent_role?: string;
  author_kind?: "human" | "assistant" | "bot"; bot_name?: string; assistant_model?: string | null; inference_run_id?: string | null;
  /** Project file revisions this reply's own run saved. */
  files?: import("./ReplyFiles").ReplyFile[];
  workflow_run_id?: string; usage?: MessageUsage | null;
  /** Set when this message was added to a running model reply. */
  addition?: ReplyAddition;
  /** On a model answer: the later messages its reply read and took into account. */
  additions?: ReplyAddition[];
  /** On a model answer: its reply's recorded steps, for the folded timeline under it. */
  steps?: ModelRun["activity"] };
export type ImpactProfile = { model: string; input_kwh_per_million: number; output_kwh_per_million: number;
  location: string; grid_grams_co2e_per_kwh: number | null; source: string };
export type MessageUsage = { prompt_tokens: number | null; completion_tokens: number | null; actual_microusd: number | null;
  estimated_kwh: number | null; estimated_kg_co2e: number | null; assumptions: ImpactProfile | null; estimate_kind?: "reference" | "configured" };
export type UsageTotal = { runs: number; prompt_tokens: number; completion_tokens: number; actual_microusd: number;
  outstanding_microusd: number; input_known_runs: number; output_known_runs: number; cost_known_runs: number;
  energy_known_runs: number; reference_energy_runs?: number; carbon_known_runs: number; estimated_kwh: number | null; estimated_kg_co2e: number | null };
export type OrganizationUsage = { total: UsageTotal; projects: (UsageTotal & { project_id: string; project_name: string })[]; method: string; scope: string };
export type ModelPrice = { id: string; canonical_slug?: string | null; name: string; input_usd_per_million: number | null; output_usd_per_million: number | null;
  reasoning_efforts?: string[];
  execution_source?: "bot";
  bot_online?: boolean;
  provider: string; description: string; modality: string | null; input_modalities: string[]; output_modalities: string[];
  supported_parameters: string[]; supports_tools: boolean; supports_reasoning: boolean; request_usd: number | null;
  cache_read_usd_per_million: number | null; cache_write_usd_per_million: number | null;
  reasoning_usd_per_million: number | null; image_usd: number | null; web_search_usd: number | null;
  context_length: number; max_completion_tokens: number | null; weights_repository?: string | null;
  routing_availability?: { zdr: boolean | null; regions: string[]; source: string } };
export type ModelRecommendations = { version: string; reviewed_at: string; source: string; method: string; model_ids: string[];
  organization_override: boolean; evidence: { model_id: string; evidence_url: string; evaluated_setting: string; reason: string }[] };
export type BenchmarkScores = { intelligence_index: number | null; agentic_index: number | null; coding_index: number | null };
export type ModelBenchmarks = { models: Record<string, BenchmarkScores>; status: "live" | "cached" | "stale" | "unavailable";
  meta: { source: string; source_url: string; citation: string; as_of: string } | null };
export type OrganizationPrices = { models: ModelPrice[]; allowed_models: string[]; fetched_at: number; routing: string; source: string;
  recommendations?: ModelRecommendations; benchmarks?: ModelBenchmarks };
export type ProviderDiagnostics = { http_status?: number; category: string; message: string; action: string;
  phase?: "http" | "stream"; limit_source?: string; reason?: string; protocol_stage?: string };
export type InferenceCall = { id: string; sequence: number; run_id: string; request_id: string; requested_by: string; actor_name: string;
  usage?: MessageUsage;
  project_id: string; project_name: string; topic_id: string; topic_name: string; model: string; purpose: string; parent_call_id: string | null;
  harness_version: string; prompt_version: string; started_at: number; finished_at?: number; latency_ms?: number | null;
  time_to_first_token_ms?: number | null; time_to_response_headers_ms?: number | null; source: string;
  status: string; accounting_state: string; reserved_microusd: number; actual_microusd?: number | null;
  prompt_tokens?: number | null; completion_tokens?: number | null; cached_tokens?: number | null; reasoning_tokens?: number | null;
  total_tokens?: number | null; tool_tokens?: number | null; provider_generation_id?: string | null;
  error_code?: string | null; diagnostics?: ProviderDiagnostics | null; quote: Record<string, unknown>; route: Record<string, unknown>;
  context_sha256: string | null; workflow_run_id: string | null; workflow_node_id: string | null;
  receipts?: { sequence: number; kind: string; recorded_at: number; data: Record<string, unknown> }[] };
export type InferenceCallPage = { calls: InferenceCall[]; next_before: number | null };
/** Who may invoke a bot besides its owner: nobody, listed organization members, or everyone who can write in its topics. */
export type BotSharingPolicy = "owner" | "members" | "topic_writers";
export type BotMetadata = { id: string; name: string; organization_id: string; created_by: string; authorized_by: string;
  sharing?: { policy: BotSharingPolicy; members: string[] };
  developer?: { enabled: boolean; repositories: string[]; requesters: string[]; issue_self_approvers?: string[]; issue_daily_limit?: number };
  runtime?: { engine: string; models: string[]; local_tools: boolean; online: boolean } | null;
  public_access?: boolean; explicit_topic_ids?: string[];
  delegation_kind?: "administrator" | "personal";
  scopes: ("messages:read" | "messages:write" | "knowledge:read" | "knowledge:write")[]; topic_ids: string[]; created_at: number; expires_at: number;
  revoked_at: number | null; last_used_at: number | null; enabled: boolean };
export type BotModel = { id: string; name: string; bot_id: string; engine: string; owner_user_id: string;
  online?: boolean; local_tools?: boolean; efforts?: string[] };
export type BotRun = { id: string; bot_id: string; bot_name?: string | null; model: string; topic_id: string; status: string; phase: string;
  activity?: { kind: string; label: string; at: number }[];
  context_through_sequence?: number;
  requested_by?: string; owner_user_id?: string | null; approval_by?: "owner" | "requester" | null;
  created_at: number; updated_at: number; output_text: string; error_code: string | null; timing?: BotRunTiming | null };
/** Server stamps in epoch milliseconds; runner stages in milliseconds after the runner received the claim. Null is unknown. */
export type BotRunTiming = { enqueued_at_ms: number | null; claimed_at_ms: number | null; committed_at_ms: number | null;
  queue_wait_ms: number | null; claim_to_commit_ms: number | null; resolved_model: string | null;
  runner: { cli_started_ms: number | null; cli_ready_ms: number | null; first_text_ms: number | null;
    final_text_ms: number | null; cli_exited_ms: number | null } };
export type SearchResult = Message & { project_name: string; channel_name: string; topic_name: string; topic_kind: TopicKind };
export type Session = { user_id: string; organization_id?: string; csrf_token?: string };
export type KeyMetadata = { id: string; provider: string; label: string; fingerprint: string; disabled_at: number | null };
export type Invitation = { id: string; email: string; role: Role; expires_at: number; accepted_at: number | null; revoked_at: number | null;
  email_state?: "queued" | "sending" | "accepted" | "rejected" | "unknown" | "cancelled" | null; email_error?: string | null };
export type InvitationMail = { enabled: boolean; daily_limit: number | null; worker_running: boolean };
export function invitationEmailLabel(invitation: Invitation): string {
  const labels = { queued: "Email queued", sending: "Email submission in progress", accepted: "Email accepted by provider (inbox delivery unconfirmed)",
    rejected: "Email rejected; share the invitation link", unknown: "Email submission uncertain; no automatic resend", cancelled: "Email cancelled" };
  return invitation.email_state ? labels[invitation.email_state] : "Link sharing only";
}
export type Policy = { allowed_models: string[] };
export type InferenceConfiguration = {
  routing?: RoutingPolicy;
  allowed_models: string[]; has_organization_key: boolean; enabled: boolean; worker_running: boolean;
  spent_microusd: number; reserved_microusd: number; uncertain_microusd: number;
};
export type RoutingPolicy = { strategy: "recommended" | "cheapest" | "latency" | "throughput"; zdr: boolean; region: "global" | "us" | "eu" };
export type ModelAttempt = {
  client_id: string; models: string[]; max_completion_tokens?: number | null; context_message_limit: number; through_sequence: number;
  effort?: string | null;
};
/**
 * Browser-only state of a reply request. Without `unconfirmed` the submission
 * is in flight; with it, the submission failed without a definitive answer and
 * the server may or may not have accepted it.
 */
export type PendingModelAttempt = ModelAttempt & { unconfirmed?: { reason: string } };
export type ModelRun = {
  timing?: { wait_phase: "local_queue" | "preparing_context" | "provider_wait" | "provider_stream" | "tool_execution" | null;
    queue_wait_ms: number | null; preparation_ms: number | null; provider_elapsed_ms: number | null;
    time_to_response_headers_ms: number | null; time_to_first_token_ms: number | null;
    tool_execution_ms: number | null; provider_calls: number } | null;
  agent?: { role: string; task_id?: string | null; parent_run?: string | null; root_run: string; thread_status?: string | null } | null;
  cost_progress?: { confirmed_microusd: number; next_call_estimate_microusd: number; internal_hold_microusd: number } | null;
  created_at?: number; started_at?: number | null;
  activity?: { sequence: number; phase: string; tool?: string; round?: number; summary?: string;
    /** For `additions_read`: the added messages read before provider call `step`. */
    message_ids?: string[]; step?: number | null;
    /** For tool events: what the call did, in a short phrase ("searched arXiv for “x” · 4 results"). */
    detail?: string }[];
  answer_state?: { status: "working" | "waiting" | "partial" | "response_ready"; reason?: string | null;
    checkpoint?: { summary: string; remaining?: string; sources?: string[] } | null } | null;
  diagnostics?: ProviderDiagnostics | null;
  usage?: MessageUsage;
  id: string; request_id: string; requested_by: string; model: string;
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled" | "interrupted"; cancel_requested: number;
  output_text: string; error_code: string | null; message_id: string | null; context_through_sequence: number;
  accounting_state: "reserved" | "settled" | "uncertain" | "released"; actual_microusd: number | null;
  reserved_microusd: number; prompt_tokens: number | null; completion_tokens: number | null;
  provider_generation_id: string | null;
  /** Messages added to this reply while it runs; only a thread's root reply carries them. */
  additions?: ReplyAddition[];
};

export const usd = (microusd: number) => "$" + (microusd / 1_000_000).toFixed(6);

export const EMPTY_HIERARCHY: Hierarchy = { projects: [], channels: [], topics: [], topic_state: [] };
export const KIND_SYMBOL: Record<TopicKind, string> = { alpha: "α", beta: "β", gamma: "γ" };
export const KIND_LABEL: Record<TopicKind, string> = { alpha: "Discussion", beta: "AI chat", gamma: "Workflow" };

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

export function csrfCookie(cookie: string): string | null {
  for (const name of ["__Host-reasoning_csrf", "reasoning_csrf"]) {
    const entry = cookie.split(";").map((part) => part.trim()).find((part) => part.startsWith(name + "="));
    if (entry) {
      try { return decodeURIComponent(entry.slice(name.length + 1)); } catch { return null; }
    }
  }
  return null;
}

/** The ApiError message when a failed response carries no string `detail`. */
export const UNDESCRIBED_ERROR = "Some fields are invalid. Check the form and try again.";

type SharedRead = { started: number; durable: boolean; promise: Promise<unknown> };
const sharedReads = new Map<string, SharedRead>();

/** Recently viewed topics' loaded messages and cursors, so a revisit shows content at once and fetches only the delta. */
export type TopicSnapshot = { messages: Message[]; cursor: number; editCursor: number; older: boolean };
const topicSnapshots = new Map<string, TopicSnapshot>();
export const TOPIC_SNAPSHOT_LIMIT = 8;
export function readTopicSnapshot(key: string): TopicSnapshot | undefined { return topicSnapshots.get(key); }
export function rememberTopicSnapshot(key: string, snapshot: TopicSnapshot | null): void {
  topicSnapshots.delete(key);
  if (!snapshot) return;
  topicSnapshots.set(key, snapshot);
  while (topicSnapshots.size > TOPIC_SNAPSHOT_LIMIT) topicSnapshots.delete(topicSnapshots.keys().next().value!);
}

/** Forget every shared read and topic snapshot, for example when the signed-in account changes. */
export function clearSharedReads(): void { sharedReads.clear(); topicSnapshots.clear(); }

/** Drop one shared read so the next request fetches it again, for example after an explicit refresh. */
export function forgetSharedRead(path: string): void { sharedReads.delete(path); }

/** The public model catalog is large and changes slowly; mounted views share one copy for five minutes. */
export const CATALOG_MAX_AGE = 300_000;
export const modelCatalog = (organizationId: string, signal?: AbortSignal) =>
  sharedRequest<OrganizationPrices>(organizationPath(organizationId) + "/model-prices", { maxAge: CATALOG_MAX_AGE, durable: true, signal });

function mutationCompleted(path: string): void {
  // Topic-scoped and direct-conversation writes (messages, read cursors, runs, goals, media) never change organization catalogs.
  const topicScoped = path.includes("/topics/") || path.includes("/direct-conversations/");
  for (const [key, entry] of sharedReads) if (!(topicScoped && entry.durable)) sharedReads.delete(key);
}

function abortable<T>(promise: Promise<T>, signal?: AbortSignal): Promise<T> {
  if (!signal) return promise;
  if (signal.aborted) return Promise.reject(new DOMException("The request was aborted.", "AbortError"));
  return new Promise<T>((resolve, reject) => {
    const abort = () => reject(new DOMException("The request was aborted.", "AbortError"));
    signal.addEventListener("abort", abort, { once: true });
    promise.then(resolve, reject).finally(() => signal.removeEventListener("abort", abort));
  });
}

/**
 * A GET shared by every mounted view: concurrent callers share one request, and a result started less than
 * `maxAge` milliseconds ago is reused. Any write through `request` invalidates shared reads (durable catalogs
 * survive topic-scoped writes; read-only mathematics operations are not writes); failures are never reused.
 * Private responses stay in memory only.
 */
export function sharedRequest<T>(path: string, options: { maxAge: number; signal?: AbortSignal; durable?: boolean }): Promise<T> {
  const now = Date.now(), existing = sharedReads.get(path);
  if (existing && now - existing.started <= options.maxAge)
    return abortable(existing.promise as Promise<T>, options.signal);
  const entry: SharedRead = { started: now, durable: Boolean(options.durable), promise: request<T>(path) };
  sharedReads.set(path, entry);
  entry.promise.catch(() => { if (sharedReads.get(path) === entry) sharedReads.delete(path); });
  return abortable(entry.promise as Promise<T>, options.signal);
}

/** `POST /mathematics` operations the server runs without writing (`READS` in server/workbench_tools.py). */
export const MATHEMATICS_READS: ReadonlySet<string> = new Set(["ledger_status", "ledger_search", "ledger_graph", "ledger_blockers",
  "ledger_blast_radius", "ledger_references", "ledger_search_references", "ledger_read"]);
const readOnlyOperation = (path: string, body: unknown) => /^\/organizations\/[^/]+\/mathematics$/.test(path)
  && MATHEMATICS_READS.has(String((body as { name?: unknown } | null)?.name));

export async function request<T>(path: string, options: { method?: string; body?: unknown; signal?: AbortSignal } = {}): Promise<T> {
  if (options.method && options.method !== "GET" && !(options.method === "POST" && readOnlyOperation(path, options.body))) {
    try { return await send<T>(path, options); } finally { mutationCompleted(path); }
  }
  return send<T>(path, options);
}

async function send<T>(path: string, options: { method?: string; body?: unknown; signal?: AbortSignal }): Promise<T> {
  const headers: Record<string, string> = {};
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (options.method && options.method !== "GET") {
    const csrf = csrfCookie(document.cookie);
    if (csrf) headers["X-CSRF-Token"] = csrf;
  }
  const method = options.method ?? "GET";
  let response: Response;
  try {
    response = await fetch("/api" + path, {
      method, headers, credentials: "same-origin", cache: "no-store", signal: options.signal,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
    });
  } catch (cause) {
    // A cancelled request is not a failure; anything else never reached the server.
    if (!options.signal?.aborted) recordFailedRequest(method, path, 0, "network error: the request did not reach the server");
    throw cause;
  }
  observeRelease(response);
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const detail = typeof body?.detail === "string" ? body.detail : UNDESCRIBED_ERROR;
    recordFailedRequest(method, path, response.status, detail);
    throw new ApiError(response.status, detail);
  }
  return response.status === 204 ? undefined as T : response.json();
}

export const organizationPath = (organizationId: string) => "/organizations/" + encodeURIComponent(organizationId);
export const topicPath = (organizationId: string, topicId: string) => organizationPath(organizationId) + "/topics/" + encodeURIComponent(topicId);
export const errorMessage = (error: unknown) => error instanceof Error ? error.message : "Request failed. Try again.";
export function topicGrantLabel(hierarchy: Hierarchy | null, topicId: string): string {
  const topic = hierarchy?.topics.find((item) => item.id === topicId);
  if (!topic) return topicId;
  const channel = hierarchy?.channels.find((item) => item.id === topic.channel_id);
  const project = hierarchy?.projects.find((item) => item.id === channel?.project_id);
  return `${project?.name ?? "Project"} / #${channel?.name ?? "Channel"} / ${KIND_SYMBOL[topic.kind]}-${topic.name}`;
}
export function authorLabel(message: Message, members: Member[]): string {
  if (message.author_kind === "assistant") return (message.agent_role ? message.agent_role.replaceAll("_", " ") : "LLM") + " · " + message.assistant_model;
  if (message.author_kind === "bot") return "Bot · " + (message.bot_name ?? "Bot");
  return members.find((member) => member.id === message.author_id)?.name ?? "Departed member";
}
/** Whether you wrote a message yourself: a model or bot reply you asked for is not yours. */
export const isOwnMessage = (message: Pick<Message, "author_id" | "author_kind">, userId: string) =>
  (message.author_kind ?? "human") === "human" && message.author_id === userId;
export const aborted = (error: unknown) => error instanceof Error && error.name === "AbortError";

/**
 * Reconcile messages by ID. A newer revision replaces a message; an equal revision replaces it only when its
 * content changed. Unchanged messages keep their object identity, and an empty or unchanged delta returns
 * `existing` itself, so memoized rendering skips them.
 */
export function mergeMessages(existing: Message[], incoming: Message[]): Message[] {
  const byId = new Map(existing.map((message) => [message.id, message]));
  let changed = false;
  for (const message of incoming) {
    const current = byId.get(message.id);
    const revision = message.revision ?? 0, currentRevision = current?.revision ?? 0;
    if (!current || revision > currentRevision || revision === currentRevision && JSON.stringify(current) !== JSON.stringify(message)) {
      byId.set(message.id, message); changed = true;
    }
  }
  return changed ? [...byId.values()].sort((a, b) => a.sequence - b.sequence) : existing;
}

export function mentionText(content: string, members: Pick<Member, "id" | "name">[]): string {
  const names = new Map(members.map((member) => [member.id, member.name]));
  return content.replace(/@\[([a-f0-9]{32})(?::([A-Za-z0-9][A-Za-z0-9._/-]{0,99}(?:\[(?:1m|200k)\])?)(?:!(low|medium|high|xhigh|max|ultra))?)?\]/g,
    (_match, id: string, model?: string, effort?: string) => "@" + (names.get(id) ?? "departed member")
      + (model ? "-" + model : "") + (effort ? "!" + effort : ""));
}

export type MentionQuery = { start: number; end: number; query: string };
export function mentionQuery(content: string, cursor: number, botNames: string[] = []): MentionQuery | null {
  if (!Number.isInteger(cursor) || cursor < 0 || cursor > content.length) return null;
  const prefix = content.slice(0, cursor), start = prefix.lastIndexOf("@");
  if (start >= 0 && (start === 0 || /[\s([{]/.test(prefix[start - 1]))) {
    const query = prefix.slice(start + 1);
    if (botNames.some(name => query.toLocaleLowerCase().startsWith(name.toLocaleLowerCase() + "-")
      && !/\s/.test(query.slice(name.length + 1)))) return { start, end: cursor, query };
  }
  const match = /(?:^|[\s([{])@([^\s@\[\]]*)$/u.exec(content.slice(0, cursor));
  return match ? { start: cursor - match[1].length - 1, end: cursor, query: match[1] } : null;
}

export function mentionMarkdown(content: string, members: Pick<Member, "id" | "name">[]): string {
  const names = new Map(members.map((member) => [member.id, member.name]));
  return content.replace(/@\[([a-f0-9]{32})(?::([A-Za-z0-9][A-Za-z0-9._/-]{0,99}(?:\[(?:1m|200k)\])?)(?:!(low|medium|high|xhigh|max|ultra))?)?\]/g,
    (_match, id: string, model?: string, effort?: string) => {
    const name = (names.get(id) ?? "departed member").replace(/[\r\n]/g, " ").replace(/[\\`*_{}\[\]()#+.!<>~$]/g, "\\$&");
    // A link to `#mention/ID`, rendered as a tinted mention; the label stays escaped text.
    return "[@" + name + (model ? "-" + model.replace(/[._/-]/g, "\\$&") : "") + (effort ? "!" + effort : "") + "](#mention/" + id + ")";
  });
}

export function decimalBudget(value: string): number {
  const match = /^(\d{1,10})(?:\.(\d{1,6}))?$/.exec(value.trim());
  if (!match) throw new Error("Use a nonnegative dollar amount with at most six decimal places.");
  const result = Number(match[1]) * 1_000_000 + Number((match[2] ?? "").padEnd(6, "0"));
  if (result > 1_000_000_000_000_000) throw new Error("Budget exceeds the supported maximum.");
  return result;
}
