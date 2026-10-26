import { PollBackoff, schedulePoll } from "./polling";
import React, { useEffect, useMemo, useRef, useState } from "react";
import { availableEfforts } from "./ReasoningEffort";
import { createPortal } from "react-dom";
import { ApiError, OrganizationPrices, aborted, decimalBudget, errorMessage, request, sharedRequest, topicPath, usd } from "./organization-api";
import { ModelPicker } from "./ModelBrowser";
import { catalogModel, modelOptionGroups } from "./modelRecommendations";
import { MathematicsBrowser, ResearchMarkdown } from "./MathematicsBrowser";
import { ResearchAccount } from "./ResearchAccount";
import { ResearchFiles } from "./ResearchFiles";
import { useOpenProjectFile } from "./ReplyFiles";
import "./research-controls.css";
import { useTopicCommand } from "./TopicCommands";
import { TopicItemDialog } from "./TopicActivity";

export const RESEARCH_TOOLS = ["history", "arxiv", "web", "knowledge", "workflows", "compute"] as const;
export type ResearchTool = typeof RESEARCH_TOOLS[number];
export const TOOL_LABELS: Record<ResearchTool, string> = {
  history: "Conversation history", arxiv: "Mathematical literature & databases", web: "Web", knowledge: "Research ledger & sources", workflows: "Gamma workflows", compute: "Compute",
};
type Goal = { id: string; objective: string; acceptance: string[]; owned_by: string; model: string; status: string;
  budget_microusd: number; used_or_held_microusd: number; max_turns: number; max_tool_calls: number;
  turns: number; tool_calls: number; deadline_at: number; reason: string | null;
  events: { sequence: number; created_at: number; event: { type: string; summary?: string; receipt_id?: string; name?: string } }[] };
type Context = { snapshot: { messages: { role: string; content: string }[]; memory_revision?: number; input_token_upper_bound?: number;
  through_sequence?: number; pinned_message_ids?: string[]; [key: string]: unknown } | null; working_memory: { body: unknown } | null;
  system_prompt: string; prompt_version: string; last_call_context?: unknown; memory_versions?: unknown[];
  pins?: { id: string; sequence: number; content: string }[] };
type Proposal = { id: string; status: string; reviewed_by: string | null; body: { action: string; title?: string; [key: string]: unknown } };
type ToolReceipt = { id: string; name: string; status: string; created_at: number; finished_at?: number | null };
/** The newest tool receipt summaries of a topic and the inclusive `created_at` cursor of its next receipt request. */
export type ToolReceiptState = { receipts: ToolReceipt[]; cursor: number };
export const RECENT_TOOL_LIMIT = 5;
const RECEIPT_TOPIC_LIMIT = 8;
const receiptStates = new Map<string, ToolReceiptState>();
/** Recently viewed topics keep their receipt summaries and cursor, so a revisit requests only newer receipts. */
export function readToolReceipts(key: string): ToolReceiptState | undefined { return receiptStates.get(key); }
export function rememberToolReceipts(key: string, state: ToolReceiptState | null): void {
  receiptStates.delete(key);
  if (!state) return;
  receiptStates.set(key, state);
  while (receiptStates.size > RECEIPT_TOPIC_LIMIT) receiptStates.delete(receiptStates.keys().next().value!);
}
/** The next request's cursor; it stays at the oldest displayed pending receipt so that receipt's status change is fetched. */
export function toolReceiptAfter(state: ToolReceiptState): number {
  return Math.min(state.cursor, ...state.receipts.filter(receipt => receipt.status === "requested").map(receipt => receipt.created_at));
}
/** Merge returned summaries by ID, keep the newest five, and advance the cursor. Unchanged input returns `state` itself. */
export function mergeToolReceipts(state: ToolReceiptState, incoming: ToolReceipt[]): ToolReceiptState {
  const byId = new Map(state.receipts.map(receipt => [receipt.id, receipt]));
  for (const receipt of incoming) byId.set(receipt.id, receipt);
  const receipts = [...byId.values()].sort((a, b) => b.created_at - a.created_at || (a.id < b.id ? 1 : a.id > b.id ? -1 : 0))
    .slice(0, RECENT_TOOL_LIMIT);
  const cursor = Math.max(state.cursor, ...incoming.map(receipt => receipt.created_at));
  return cursor === state.cursor && JSON.stringify(receipts) === JSON.stringify(state.receipts) ? state : { receipts, cursor };
}
export type GoalAttempt = { client_id: string; objective: string; acceptance: string[]; model: string; budget_microusd: number; effort?: string | null;
  max_turns: number; max_tool_calls: number; duration_seconds: number };

export function ResearchInstructions({ instructions, loading, error = "", onInspect, bare = false }: {
  instructions: { system_prompt: string; prompt_version: string } | null; loading: boolean;
  error?: string; onInspect?: () => void;
  /** Shown without its own "System prompt" disclosure, as in the topic's System prompt dialog. */
  bare?: boolean;
}) {
  const body = <>{instructions ? <><small>{instructions.prompt_version}</small><ResearchMarkdown>{instructions.system_prompt}</ResearchMarkdown></>
    : <p>{loading ? "Loading system prompt…" : "System prompt unavailable. Try refreshing."}</p>}
    {error && <p role="alert" className="org-error">{error}</p>}</>;
  return <article className="org-message org-system-message" aria-label="Research instructions">
    {bare ? body : <details onToggle={(event) => { if (event.currentTarget.open) onInspect?.(); }}><summary>System prompt</summary>{body}</details>}
  </article>;
}

/** The short value a topic details row shows: "v9" for "topic-agents-v9". */
export function shortVersion(version: string): string { return version.match(/(v\d+)$/)?.[1] ?? version; }

/** The ⓘ beside the model under the message box: the selected model's capabilities, each research reader on or off. */
export function ModelCapabilities({ model, summary, date, readers }: {
  model: string; summary: string; date?: string | null; readers: [string, boolean][] | null;
}) {
  const [open, setOpen] = useState(false), box = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (event: PointerEvent | KeyboardEvent) => {
      if ((event instanceof KeyboardEvent && event.key === "Escape") || (event instanceof PointerEvent && !box.current?.contains(event.target as Node))) setOpen(false);
    };
    document.addEventListener("pointerdown", close); document.addEventListener("keydown", close);
    return () => { document.removeEventListener("pointerdown", close); document.removeEventListener("keydown", close); };
  }, [open]);
  return <span className="org-model-capabilities" ref={box}>
    <button type="button" className="org-composer-info" aria-label="Model details" title="Model details" aria-expanded={open}
      onClick={() => setOpen(value => !value)}>i</button>
    {open && <div className="org-capability-details" role="group" aria-label="Effective model capabilities">
      <strong>{model}</strong><p>{summary}</p>
      {(readers || date) && <dl>
        {readers?.map(([label, on]) => <React.Fragment key={label}><dt>{label}</dt><dd className={on ? "org-capability-on" : undefined}>{on ? "on" : "off"}</dd></React.Fragment>)}
        {date && <><dt>Date the model sees</dt><dd>{date} UTC</dd></>}
      </dl>}
    </div>}
  </span>;
}

/** The items of the topic details card; each opens in a dialog rather than a research pane. */
const TOPIC_ITEMS: readonly string[] = ["instructions", "account", "files"];

export function goalCommand(text: string): string | null {
  const match = text.trim().match(/^\/goal(?:\s+([\s\S]*))?$/); return match ? (match[1] ?? "").trim() : null;
}

export function captureGoal({ objective, acceptance, model, budget, turns, tools, hours, clientId }: {
  objective: string; acceptance: string; model: string; budget: string; turns: string; tools: string; hours: string; clientId: string;
}): GoalAttempt {
  const criteria = acceptance.split("\n").map((item) => item.trim()).filter(Boolean);
  const maximumTurns = Number(turns), maximumTools = Number(tools), duration = Number(hours) * 3600, cost = decimalBudget(budget);
  if (!objective.trim() || objective.trim().length > 8000 || !criteria.length || criteria.length > 20 || criteria.some((item) => item.length > 2000)
    || !model || cost < 1 || cost > 1e12 || !Number.isInteger(maximumTurns) || maximumTurns < 1 || maximumTurns > 100
    || !Number.isInteger(maximumTools) || maximumTools < 1 || maximumTools > 1000 || !Number.isInteger(duration) || duration < 60 || duration > 86400) {
    throw new Error("Choose an objective, completion criteria, model, and valid limits.");
  }
  return Object.freeze({ client_id: clientId, objective: objective.trim(), acceptance: Object.freeze(criteria) as unknown as string[],
    model, budget_microusd: cost, max_turns: maximumTurns, max_tool_calls: maximumTools, duration_seconds: duration });
}

export function ResearchGoalSummary({ goal, canControl, busy, onControl }: { goal: Goal; canControl: boolean; busy: boolean;
  onControl: (status: "active" | "paused" | "cancelled") => void }) {
  return <section className="org-research-goal" aria-label="Research goal"><header><strong>{goal.objective}</strong><span role="status">{goal.status.replaceAll("_", " ")}</span></header>
    <p>{goal.turns}/{goal.max_turns} turns · {goal.tool_calls}/{goal.max_tool_calls} tools · {goal.model.startsWith("bot/")
      ? "Owner's CLI account; charges tracked externally" : `${usd(goal.used_or_held_microusd)} used or held / ${usd(goal.budget_microusd)} limit`}</p>
    {goal.reason && <p>{goal.reason}</p>}
    <details><summary>Completion criteria and checkpoints</summary><ul>{goal.acceptance.map((item, index) => <li key={index}>{item}</li>)}</ul>
      {goal.events.filter((event) => event.event.summary).slice(0, 3).map((event) => <p key={event.sequence}>{event.event.summary}</p>)}</details>
  </section>;
}

export function ResearchControls({ organizationId, topicId, userId, selectedModel, catalog, allowedModels, canWrite, canManage, online,
  goalDraft, onGoalDraftDone, onGoalStarted, onAuthLost, toolbarTarget, menuTarget = null, capabilitiesTarget = null, instructions, onSettingsChanged, onPrepareReview, selectedEffort = null }: {
  organizationId: string; topicId: string; userId: string; selectedModel: string; canWrite: boolean; canManage: boolean; online: boolean;
  catalog: OrganizationPrices | null; allowedModels: string[];
  goalDraft: string | null; onGoalDraftDone: () => void; onGoalStarted: (objective: string) => void; onAuthLost: () => void;
  toolbarTarget: HTMLElement | null;
  /** The topic details card in the top bar: System prompt, Research account and Project files appear there. */
  menuTarget?: HTMLElement | null;
  /** Beside the model picker under the message box: the ⓘ with the selected model's capabilities appears there. */
  capabilitiesTarget?: HTMLElement | null;
  /** The topic's system prompt, shown in the System prompt dialog. */
  instructions?: React.ReactNode;
  selectedEffort?: string | null;
  onSettingsChanged?: () => void;
  onPrepareReview?: (text: string) => void;
}) {
  const [goal, setGoal] = useState<Goal | null>(null); const [enabled, setEnabled] = useState<ResearchTool[]>([...RESEARCH_TOOLS]);
  const [context, setContext] = useState<Context | null>(null); const [proposals, setProposals] = useState<Proposal[]>([]);
  const [pane, setPane] = useState<"goal" | "context" | "tools" | "proposals" | "ledger" | "files" | "reviews" | "account" | "instructions" | null>(() => typeof window !== "undefined" && new URLSearchParams(window.location.search).get("topic") === topicId && new URLSearchParams(window.location.search).has("account_revision") ? "account" : null);
  useEffect(() => {
    const linked = () => { const query = new URLSearchParams(window.location.search);
      if (query.get("topic") === topicId && query.has("account_revision")) setPane("account"); };
    window.addEventListener("popstate", linked); return () => window.removeEventListener("popstate", linked);
  }, [topicId]);
  const [reviewer, setReviewer] = useState(""), [reviewScope, setReviewScope] = useState("");
  const [reviewEnabled, setReviewEnabled] = useState(true), [profile, setProfile] = useState("workbench");
  const [additionalInstructions, setAdditionalInstructions] = useState("");
  const [ledgerLimit, setLedgerLimit] = useState(8), [knowledgeScope, setKnowledgeScope] = useState("working_set");
  const [sourceConfig, setSourceConfig] = useState<{ web_search_configured: boolean; current_date: string } | null>(null);
  const [profiles, setProfiles] = useState<{ id: string; name: string; description: string }[]>([]);
  const [computeAllowance, setComputeAllowance] = useState(0);
  const [reviews, setReviews] = useState<{ id: string; status: string; requested_by: string; reviewer_model: string;
    subject?: { account_revision?: number; account_sha256?: string }; stale?: boolean;
    result: { recurring_objection?: boolean; changed_fields?: string[]; report?: string; error?: string; distinct_model?: boolean; verdict?: { verdict: string } } | null }[]>([]);
  const [createOpen, setCreateOpen] = useState(false); const [objective, setObjective] = useState("");
  const [goalModel, setGoalModel] = useState("");
  const [goalEffort, setGoalEffort] = useState(selectedEffort ?? "");
  const [acceptance, setAcceptance] = useState(""); const [budget, setBudget] = useState("");
  const [turns, setTurns] = useState("10"); const [toolLimit, setToolLimit] = useState("100"); const [hours, setHours] = useState("1");
  const [attempt, setAttempt] = useState<GoalAttempt | null>(null); const [clientId, setClientId] = useState(() => crypto.randomUUID());
  const [busy, setBusy] = useState(false); const [error, setError] = useState(""); const [refresh, setRefresh] = useState(0);
  const [pollError, setPollError] = useState("");
  // What the topic details card shows beside each item: the prompt version, the account version and the number of files.
  const [counts, setCounts] = useState<{ instructions?: string; account?: string; files?: string }>({});
  const [countsRevision, setCountsRevision] = useState(0);
  const [receipt, setReceipt] = useState<unknown>(null);
  const receiptKey = userId + " " + topicPath(organizationId, topicId);
  const toolState = useRef<ToolReceiptState & { key: string }>({ key: receiptKey, ...readToolReceipts(receiptKey) ?? { receipts: [], cursor: 0 } });
  const [recentTools, setRecentTools] = useState<ToolReceipt[]>(toolState.current.receipts);
  const dialog = useRef<HTMLDialogElement>(null);
  const objectiveField = useRef<HTMLTextAreaElement>(null);
  const acceptanceField = useRef<HTMLTextAreaElement>(null);
  const root = topicPath(organizationId, topicId);
  // Computed once per catalog and model list, not on every keystroke in the composer.
  const toolModels = useMemo(() => allowedModels.filter((id) => catalogModel(catalog, id)?.supports_tools), [allowedModels, catalog]);
  const goalModelGroups = useMemo(() => modelOptionGroups(toolModels, catalog), [toolModels, catalog]);
  useEffect(() => {
    if (goalDraft !== null) { if (!attempt) setObjective(goalDraft); setCreateOpen(true); setError(""); }
  }, [goalDraft]);
  useEffect(() => {
    if (createOpen) { dialog.current?.showModal(); (objective.trim() ? acceptanceField.current : objectiveField.current)?.focus(); }
  }, [createOpen]);
  useEffect(() => {
    if (createOpen && !attempt) setGoalModel((current) => toolModels.includes(current) ? current : toolModels.includes(selectedModel) ? selectedModel
      : goalModelGroups.recommended[0] ?? toolModels[0] ?? "");
  }, [createOpen, selectedModel, catalog, allowedModels]);
  const fail = (cause: unknown) => { if (cause instanceof ApiError && cause.status === 401) onAuthLost(); else setError(errorMessage(cause)); };
  useEffect(() => {
    const controller = new AbortController(), backoff = new PollBackoff(2500, 10000); let timer: ReturnType<typeof setTimeout>;
    if (toolState.current.key !== receiptKey) {
      toolState.current = { key: receiptKey, ...readToolReceipts(receiptKey) ?? { receipts: [], cursor: 0 } };
      setRecentTools(toolState.current.receipts);
    }
    const poll = async () => {
      let active = true;
      try {
        const [current, pending, toolReceipts, checked] = await Promise.all([
          // The agent activity view polls the same goal more often; reuse its recent result.
          sharedRequest<Goal | null>(root + "/goal", { maxAge: 2500, signal: controller.signal }),
          request<Proposal[]>(root + "/research-proposals", { signal: controller.signal }),
          // Summaries only: a receipt's arguments and result are read when it is opened.
          request<ToolReceipt[]>(root + "/research-tools?summary=1&after=" + toolReceiptAfter(toolState.current), { signal: controller.signal }),
          request<typeof reviews>(root + "/mathematical-reviews" + (reviewScope ? "?scope=" + reviewScope : ""), { signal: controller.signal }),
        ]);
        if (controller.signal.aborted) return; setGoal(current); setProposals(pending); setReviews(checked); setPollError("");
        const merged = mergeToolReceipts(toolState.current, toolReceipts);
        if (merged !== toolState.current) {
          toolState.current = { key: receiptKey, ...merged };
          rememberToolReceipts(receiptKey, merged); setRecentTools(merged.receipts);
        }
        active = current?.status === "active" || pending.some(proposal => ["pending", "reviewing"].includes(proposal.status))
          || merged.receipts.some(tool => tool.status === "requested");
      } catch (cause) {
        if (!aborted(cause)) {
          if (cause instanceof ApiError && cause.status === 401) onAuthLost();
          else {
            if (cause instanceof ApiError && (cause.status === 403 || cause.status === 404)) {
              rememberToolReceipts(receiptKey, null); toolState.current = { key: receiptKey, receipts: [], cursor: 0 }; setRecentTools([]);
            }
            setPollError("Unable to refresh research status; reconnecting. " + errorMessage(cause));
          }
        }
      }
      if (!controller.signal.aborted) timer = schedulePoll(poll, backoff.next(active), controller.signal);
    };
    void poll();
    void request<{ tools: ResearchTool[]; reviewer_model?: string; compute_allowance_cpu_seconds?: number;
      review_enabled?: boolean; prompt_profile?: string; additional_instructions?: string;
      ledger_context_limit?: number; knowledge_context_limit?: number; knowledge_scope?: string; web_search_configured: boolean; current_date: string;
      profiles?: { id: string; name: string; description: string }[] }>(root + "/research-settings", { signal: controller.signal }).then((value) => {
      if (!controller.signal.aborted) { setEnabled(value.tools ?? [...RESEARCH_TOOLS]); setReviewer(value.reviewer_model ?? ""); setComputeAllowance(value.compute_allowance_cpu_seconds ?? 0);
        setReviewEnabled(value.review_enabled ?? true); setProfile(value.prompt_profile ?? "workbench");
        setAdditionalInstructions(value.additional_instructions ?? ""); setProfiles(value.profiles ?? []);
        setLedgerLimit(value.knowledge_context_limit ?? value.ledger_context_limit ?? 8); setKnowledgeScope(value.knowledge_scope ?? "working_set"); setSourceConfig(value); }
    }).catch((cause) => { if (!aborted(cause)) fail(cause); });
    return () => { controller.abort(); clearTimeout(timer); };
  }, [root, receiptKey, refresh, reviewScope, onAuthLost]);
  useEffect(() => {
    if (!menuTarget) return;
    const controller = new AbortController(), signal = controller.signal;
    const quietly = (cause: unknown) => { if (!aborted(cause) && cause instanceof ApiError && cause.status === 401) onAuthLost(); };
    void request<{ prompt_version: string }>(root + "/research-instructions", { signal })
      .then(value => setCounts(current => ({ ...current, instructions: shortVersion(value.prompt_version) }))).catch(quietly);
    void request<{ revision: number } | null>(root + "/research-account?summary=true", { signal })
      .then(value => setCounts(current => ({ ...current, account: value ? `v${value.revision}` : "none" }))).catch(quietly);
    void Promise.all([request<unknown[]>(root + "/research-files", { signal }), request<unknown[]>(root + "/media", { signal })])
      .then(([files, media]) => setCounts(current => ({ ...current, files: String(files.length + media.length) }))).catch(quietly);
    return () => controller.abort();
  }, [root, !!menuTarget, countsRevision, onAuthLost]);
  const closeItem = () => { setPane(null); setCountsRevision(value => value + 1); };
  // A prepared critique goes to the message box, so the account dialog closes to show it.
  const prepareReview = onPrepareReview && ((text: string) => { closeItem(); onPrepareReview(text); });
  const loadContext = async () => {
    setPane(pane === "context" ? null : "context"); setError("");
    try { setContext(await request<Context>(root + "/research-context")); } catch (cause) { fail(cause); }
  };
  const control = async (status: "active" | "paused" | "cancelled") => {
    if (!goal) return; setBusy(true); setError("");
    try { await request(root + "/goal/" + goal.id, { method: "PATCH", body: { status } }); setRefresh((value) => value + 1); }
    catch (cause) { fail(cause); } finally { setBusy(false); }
  };
  const closeGoal = () => { setCreateOpen(false); onGoalDraftDone(); };
  const pending = proposals.filter((proposal) => ["pending", "reviewing"].includes(proposal.status));
  const activeTool = recentTools.find((tool) => tool.status === "requested");
  // The selected model's research capabilities, shown by the ⓘ beside the model picker.
  const model = catalogModel(catalog, selectedModel);
  const web = enabled.includes("web"), literature = enabled.includes("arxiv"), search = web && !!sourceConfig?.web_search_configured;
  const readers: [string, boolean][] = [["Public source reader", web], ["Literature readers (arXiv)", literature], ["General web search", search]];
  const detailed = !selectedModel.startsWith("bot/") && !!model?.supports_tools && !!sourceConfig;
  const summary = selectedModel.startsWith("bot/") ? "CLI bot · tools depend on its live grants and runner"
    : !model ? "Checking selected model capabilities…"
      : !model.supports_tools ? "Plain response · no research tools"
        : !sourceConfig ? "Checking research capabilities…"
          : `Research · ${readers.filter(([, on]) => on).length} of ${readers.length} readers`;
  const [openFile, setOpenFile] = useState<{ path: string; revision: number } | null>(null);
  useOpenProjectFile(topicId, file => { setOpenFile(file); setPane("files"); });
  useTopicCommand(topicId, command => {
    if (command === "context") { void loadContext(); return; }
    if (["ledger", "files", "goal", "tools", "proposals", "reviews", "account"].includes(command)) {
      if (command === "goal" && !goal && canWrite) { setCreateOpen(true); return; }
      setPane(current => current === command ? null : command as typeof pane);
    }
  });
  return <div className="org-research-controls">
    {menuTarget && createPortal(<>
      {([["instructions", "≡", "System prompt"], ["account", "◇", "Research account"], ["files", "▤", "Project files"]] as const).map(([value, mark, label]) =>
        <button key={value} type="button" className="org-topic-card-row" onClick={() => {
          // Choosing an item closes the details card, as choosing a menu item does, and opens the item's dialog.
          menuTarget.closest("details")?.removeAttribute("open");
          setPane(value);
        }}><span className="org-account-mark" aria-hidden="true">{mark}</span><span>{label}</span>
          <small className="org-topic-card-count">{counts[value]}<span aria-hidden="true"> ›</span></small></button>)}
    </>, menuTarget)}
    {capabilitiesTarget && createPortal(<ModelCapabilities model={selectedModel} summary={summary} date={sourceConfig?.current_date}
      readers={detailed ? readers : null} />, capabilitiesTarget)}
    {toolbarTarget && createPortal(<div className="org-research-toolbar" aria-label="Research actions">
    {(goal?.status === "active" || activeTool) && <span role="status" className="org-goal-live"><i />{activeTool ? `Using ${activeTool.name.replaceAll("_", " ")}` : "Working toward your goal"}</span>}
  </div>, toolbarTarget)}
    {pane && !TOPIC_ITEMS.includes(pane) && <button type="button" aria-label="Close research panel" onClick={() => setPane(null)}>×</button>}
    {pane === "instructions" && <TopicItemDialog title="System prompt" onClose={closeItem}>{instructions}</TopicItemDialog>}
    {pane === "account" && <TopicItemDialog title="Research account" onClose={closeItem}><ResearchAccount key={root} root={root} canWrite={canWrite} online={online}
      reviewAvailable={!!onPrepareReview && !selectedModel.startsWith("bot/") && !!catalogModel(catalog, selectedModel)?.supports_tools && enabled.includes("knowledge") && reviewEnabled}
      onPrepareReview={prepareReview} onAuthLost={onAuthLost} /></TopicItemDialog>}
    {pane === "ledger" && <div className="org-research-pane"><MathematicsBrowser organizationId={organizationId} topicId={topicId} canWrite={canWrite} onAuthLost={onAuthLost} /></div>}
    {pane === "files" && <TopicItemDialog title="Project files" onClose={() => { setOpenFile(null); closeItem(); }}>
      <ResearchFiles key={openFile ? openFile.path + "@" + openFile.revision : "list"} organizationId={organizationId} topicId={topicId}
        canWrite={canWrite} onAuthLost={onAuthLost} initialFile={openFile} /></TopicItemDialog>}
    {pane === "reviews" && <div className="org-research-pane"><label>Review scope<select value={reviewScope} onChange={event => setReviewScope(event.target.value)}><option value="">Topic default</option><option value="working_set">Account working set</option><option value="topic">This topic</option><option value="project">Authorized project</option></select></label>{reviews.map((review) => <details key={review.id}>
      <summary>{review.subject?.account_revision ? `Account v${review.subject.account_revision}${review.stale ? " · earlier candidate" : ""} · ` : ""}{review.result?.verdict?.verdict ?? review.status} · {review.reviewer_model}</summary>
      <p className="org-help">{review.subject?.account_revision ? "Exact-version account critique; ledger status is unchanged. " : ""}Fresh-context {review.result?.distinct_model === false ? "same-model " : ""}LLM review, not formal certification.</p>
      {review.result?.changed_fields && <p>Changed since previous critique: {review.result.changed_fields.join(", ") || "no account fields"}.</p>}
      {review.result?.recurring_objection && <p>The same strongest objection recurs. Change approach or preserve the unresolved gap.</p>}
      <ResearchMarkdown>{review.result?.report ?? review.result?.error ?? "Review has not returned a report."}</ResearchMarkdown>
      {canWrite && (canManage || review.requested_by === userId) && ["pending", "running"].includes(review.status) && <button disabled={busy || !online} onClick={() => {
        setBusy(true); void request(root + "/mathematical-reviews/" + review.id + "/cancel", { method: "POST", body: {} })
          .then(() => setRefresh((value) => value + 1)).catch(fail).finally(() => setBusy(false));
      }}>Cancel review</button>}
    </details>)}</div>}
    {error && <p role="alert" className="org-error">{error}</p>}
    {pollError && <p role="alert" className="org-error">{pollError}</p>}
    {pane === "goal" && <div className="org-research-pane">
      {goal && <ResearchGoalSummary goal={goal} canControl={canWrite && (canManage || goal.owned_by === userId)} busy={busy || !online} onControl={(status) => void control(status)} />}
      {canWrite && (!goal || !["active", "paused", "blocked"].includes(goal.status)) && <button disabled={!online || !selectedModel} onClick={() => { if (!attempt) setObjective(""); setCreateOpen(true); }}>{attempt ? "Retry pending goal" : "Start a goal"}</button>}
      {!goal && <p className="org-help">Set a goal to keep the agent working across turns. You choose the stopping criteria and limits.</p>}
      {goal?.events.filter((event) => event.event.receipt_id).slice(0, 5).map((event) => <button key={event.sequence} onClick={() => void request(root + "/research-tools/" + event.event.receipt_id).then(setReceipt).catch(fail)}>{event.event.name} receipt</button>)}
      {receipt !== null && <details open><summary>Tool receipt</summary><pre>{JSON.stringify(receipt, null, 2)}</pre></details>}
    </div>}
    {pane === "context" && <div className="org-research-pane"><p className="org-help">Context uses cited working memory, pins, a token-budgeted recent tail, and retrieval. The complete conversation remains available.</p>
      {!context && <p role="status">Loading context…</p>}
      {context && <><small>{context.prompt_version} · {context.snapshot?.input_token_upper_bound?.toLocaleString() ?? "—"} input token upper bound
        · memory revision {context.snapshot?.memory_revision ?? "none"} · {context.pins?.length ?? context.snapshot?.pinned_message_ids?.length ?? 0} pins</small>
        <details><summary>System instructions</summary><pre>{context.system_prompt}</pre></details>
        <details><summary>Working memory</summary><pre>{JSON.stringify(context.working_memory?.body ?? {}, null, 2)}</pre></details>
        <details><summary>Pinned messages ({context.pins?.length ?? 0})</summary>{context.pins?.map((pin) => <p key={pin.id}>Message {pin.sequence}: {pin.content}</p>)}</details>
        <details><summary>Memory revisions</summary><pre>{JSON.stringify(context.memory_versions ?? [], null, 2)}</pre></details>
        <details><summary>Exact captured context</summary>{context.snapshot ? <pre>{JSON.stringify(context.snapshot.messages, null, 2)}</pre> : <p>Context is captured when the first model run starts.</p>}</details>
        {context.last_call_context && <details><summary>Latest provider-call context</summary><pre>{JSON.stringify(context.last_call_context, null, 2)}</pre></details>}</>}
    </div>}
    {pane === "tools" && <div className="org-research-pane"><form onSubmit={(event) => {
      event.preventDefault(); setBusy(true); setError(""); void request(root + "/research-settings", { method: "PUT", body: { tools: enabled, reviewer_model: reviewer || null,
        review_enabled: reviewEnabled, prompt_profile: profile, additional_instructions: additionalInstructions,
        knowledge_context_limit: ledgerLimit, knowledge_scope: knowledgeScope, compute_allowance_cpu_seconds: computeAllowance } })
        .then(() => { setRefresh((value) => value + 1); onSettingsChanged?.(); }).catch(fail).finally(() => setBusy(false));
    }}><label>Research strategy<select aria-label="Research strategy" value={profile} disabled={!canWrite || busy} onChange={event => setProfile(event.target.value)}>
      {profiles.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
      <p className="org-help">{profiles.find(item => item.id === profile)?.description} All strategies use the same granted tools and ledger.</p>
      <label>Additional research instructions<textarea value={additionalInstructions} maxLength={8000} rows={2} disabled={!canWrite || busy}
        onChange={event => setAdditionalInstructions(event.target.value)} /></label>
      <label>Default knowledge scope<select value={knowledgeScope} disabled={!canWrite || busy} onChange={event => setKnowledgeScope(event.target.value)}>
        <option value="working_set">Account working set (topic until an account exists)</option><option value="topic">This topic</option><option value="project">Authorized project knowledge</option>
      </select></label>
      <label>Automatic knowledge records (0 disables)<input type="number" min={0} max={20} value={ledgerLimit}
        disabled={!canWrite || busy} onChange={event => setLedgerLimit(Number(event.target.value))} /></label>
      <p className="org-help">The combined allowance covers relevant ledger records, critique summaries and shared knowledge references. Account links include supporting dependencies; wider authorized knowledge is available through explicit project searches and exact reads.</p>
      <div className="org-research-tool-grid">{RESEARCH_TOOLS.map((tool) => <label key={tool}><input type="checkbox" checked={enabled.includes(tool)} disabled={!canWrite || busy}
      onChange={(event) => setEnabled((current) => event.target.checked ? [...current, tool] : current.filter((item) => item !== tool))} />{TOOL_LABELS[tool]}</label>)}</div>
      <label><input type="checkbox" checked={reviewEnabled} disabled={!canWrite || busy} onChange={event => setReviewEnabled(event.target.checked)} />Fresh-context review</label>
      <ModelPicker label="Reviewer override" models={allowedModels.filter(id => !id.startsWith("bot/"))} catalog={catalog} value={reviewer}
        emptyLabel="Automatic reviewer" disabled={!canWrite || busy || !reviewEnabled} onChange={setReviewer} />
      <button type="button" disabled={!canWrite || busy || !reviewer} onClick={() => setReviewer("")}>Use automatic reviewer</button>
      <p className="org-help">{reviewer ? "Explicit reviewer override." : "Automatic: prefer another allowed model family; otherwise use a fresh context."} LLM review is not formal certification.</p>
      <label>Monthly autonomous compute allowance (CPU seconds)<input type="number" min={0} max={1000000} value={computeAllowance} disabled={!canWrite || busy}
        onChange={(event) => setComputeAllowance(Number(event.target.value))} /></label>
      <p className="org-help">The conversation carries the answer. Save reusable knowledge and request review when useful. The selected reviewer uses the organization’s inference budget. Compute beyond this allowance and workflow execution require approval. Alpha topics are excluded.</p>
      <button type="button" onClick={() => void loadContext()}>Inspect context</button>
      {canWrite && <button disabled={busy || !online}>Save tools</button>}</form>
      {recentTools.map((tool) => <button key={tool.id} onClick={() => void request(root + "/research-tools/" + tool.id).then(setReceipt).catch(fail)}>{tool.name.replaceAll("_", " ")} · {tool.status}</button>)}
      {receipt !== null && <details open><summary>Tool receipt</summary><pre>{JSON.stringify(receipt, null, 2)}</pre></details>}
    </div>}
    {pane === "proposals" && <div className="org-research-pane">{pending.map((proposal) => <article key={proposal.id}><strong>{proposal.body.title ?? `Proposed ${proposal.body.action}`}</strong>
      <details><summary>Review action</summary><pre>{JSON.stringify(proposal.body, null, 2)}</pre></details>
      {canWrite && (proposal.status === "pending" || proposal.reviewed_by === userId) && <div className="org-research-goal-actions">
        <button disabled={busy || !online} onClick={() => { setBusy(true); void request(root + "/research-proposals/" + proposal.id, { method: "POST", body: { approve: true } })
          .then(() => setRefresh((value) => value + 1)).catch(fail).finally(() => setBusy(false)); }}>{proposal.status === "reviewing" ? "Resume approval" : "Approve"}</button>
        {proposal.status === "pending" && <button disabled={busy || !online} onClick={() => { setBusy(true); void request(root + "/research-proposals/" + proposal.id, { method: "POST", body: { approve: false } })
          .then(() => setRefresh((value) => value + 1)).catch(fail).finally(() => setBusy(false)); }}>Decline</button>}</div>}
    </article>)}</div>}
    {createOpen && <dialog className="org-dialog org-goal-dialog" ref={dialog} aria-label="Start a research goal" onCancel={closeGoal} onClose={closeGoal}>
      <header><h2>Keep working toward a goal</h2><button type="button" aria-label="Close goal" onClick={closeGoal}>×</button></header>
      <form onSubmit={(event) => {
        event.preventDefault(); setBusy(true); setError("");
        void (async () => { try {
          const captured = attempt ?? { ...captureGoal({ objective, acceptance, model: goalModel,
            budget: goalModel.startsWith("bot/") ? "0.000001" : budget, turns, tools: toolLimit, hours, clientId }),
            effort: availableEfforts(catalog, goalModel).includes(goalEffort) ? goalEffort : null };
          setAttempt(captured); const created = await request<Goal>(root + "/goal", { method: "POST", body: captured });
          setGoal(created); setAttempt(null); setClientId(crypto.randomUUID()); closeGoal(); setPane("goal"); onGoalStarted(created.objective);
        } catch (cause) {
          if (cause instanceof ApiError && cause.status >= 400 && cause.status < 500 && cause.status !== 408) { setAttempt(null); setClientId(crypto.randomUUID()); }
          fail(cause);
        } finally { setBusy(false); } })();
      }}>
        <label>Goal<textarea required ref={objectiveField} maxLength={8000} rows={3} value={objective} disabled={busy || !!attempt} onChange={(event) => setObjective(event.target.value)} /></label>
        <label>Done when…<textarea required ref={acceptanceField} maxLength={20000} rows={2} placeholder="One completion criterion per line" value={acceptance} disabled={busy || !!attempt} onChange={(event) => setAcceptance(event.target.value)} /></label>
        <ModelPicker label="Goal model" models={toolModels} catalog={catalog} value={attempt?.model ?? goalModel}
          onChange={model => { setGoalModel(model); setGoalEffort(""); }} effort={goalEffort} onEffortChange={setGoalEffort} disabled={busy || !!attempt} />
        <p className="org-help">Goals need tool use to report checkpoints.{!toolModels.includes(selectedModel) && " Choose a supported goal model here."}</p>
        <div className="org-goal-limits">{!goalModel.startsWith("bot/") && <label>Maximum USD<input required inputMode="decimal" value={budget} disabled={busy || !!attempt} onChange={(event) => setBudget(event.target.value)} /></label>}
          <label>Hours<input required type="number" min={1 / 60} max={24} step="any" value={hours} disabled={busy || !!attempt} onChange={(event) => setHours(event.target.value)} /></label>
          <label>Turns<input required type="number" min={1} max={100} value={turns} disabled={busy || !!attempt} onChange={(event) => setTurns(event.target.value)} /></label>
          <label>Tool calls<input required type="number" min={1} max={1000} value={toolLimit} disabled={busy || !!attempt} onChange={(event) => setToolLimit(event.target.value)} /></label></div>
        {error && <p role="alert" className="org-error">{error}</p>}
        {attempt && <p className="org-help">Retry preserves the exact goal and limits until its receipt is known.</p>}
        <button className="org-primary" disabled={busy || !online || (!attempt && !toolModels.includes(goalModel))}>{busy ? "Starting…" : attempt ? "Retry exact goal" : "Start goal"}</button>
      </form>
    </dialog>}
  </div>;
}
