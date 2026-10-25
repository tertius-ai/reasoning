import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { PlusIcon } from "./ComposerIcons";
import {
  ChatExecutionOptions,
  ChatInfo,
  ChatEntry,
  ExecutionPreflight,
  ModelPriceInfo,
  PortableCheckpoint,
  ReviewInfo,
  SelectedRunConfiguration,
  GoalInfo,
  TaskInfo,
  WriterInfo,
  answerApproval,
  fetchChatExecutionOptions,
  fetchHistory,
  fetchModelPrices,
  fetchReviewDetail,
  fetchReviews,
  fetchTasks,
  fetchWriterDetail,
  exportPortableCheckpoint,
  handoffPortableCheckpoint,
  interruptChat,
  openEvents,
  postReadMarker,
  preflightChatExecution,
  promotePortableCheckpoint,
  sendMessage,
  uploadArtifact,
} from "./api";
import { modelProfile } from "./AgentModelPicker";
import { estimateOpenRouterTokenCost, formatEstimatedCost } from "./costEstimate";
import { RunConfigurationControls } from "./RunConfigurationControls";
import { AutoRoutingControl } from "./AutoRoutingControl";
import { RevealArtifactButton } from "./ArtifactActions";
import {
  Markdown,
  TeX,
  artifactNameFromPath,
  openArtifact,
  openLedgerEntry,
} from "./Markdown";

// `k` is the item's absolute transcript index, used as its stable React
// key: the quick two-turn paint and the full backfill assign the SAME
// key to the same entry, so the swap reconciles in place instead of
// remounting (and flashing) everything on screen. Items appended by
// live events have no k and key by array position, which is stable
// within a session.
type Item = ItemVariant & { k?: number };

type ItemVariant =
  | {
      kind: "user";
      text: string;
      steered?: boolean;
      at?: string;
      importedCheckpointId?: string;
      sourceChatId?: string;
    }
  | {
      kind: "assistant";
      text: string;
      at?: string;
      error?: boolean;
      messageId?: string;
      // the entry's transcript index — the coordinate the chat's shared
      // read marker is kept in
      tidx?: number;
      // set once a KaTeX repair lands: `text` holds the repaired copy and
      // this the author's original, shown behind the toggle.
      latexOriginal?: string;
      executorId?: string;
      model?: string;
      operationId?: string;
      importedCheckpointId?: string;
      sourceChatId?: string;
    }
  | { kind: "reasoning"; text: string }
  | {
      kind: "activity";
      label: string;
      detail: string;
      tex?: boolean;
      body?: string;
      error?: boolean;
    }
  | { kind: "subagent"; threadId: string }
  | {
      kind: "review";
      reviewId: string;
      status: string;
      verdict: string | null;
      subjectId: string;
      /** writeup reviews: the reviewed document's artifacts path */
      documentName?: string;
      text: string;
      // set once the report's LaTeX is autorepaired for display
      latexOriginal?: string;
    }
  | {
      kind: "writer";
      writerId: string;
      writerKey: string;
      /** the document's artifacts path */
      filename: string;
      status: string;
      activation: number;
      text: string;
      at?: string;
      latexOriginal?: string;
    }
  | {
      kind: "approval";
      requestId: string;
      approvalKind: string;
      command: string | null;
      cwd: string | null;
      reason: string | null;
      decision?: string;
    };

interface ApprovalItem {
  requestId: string;
  approvalKind: string;
  command: string | null;
  cwd: string | null;
  reason: string | null;
  createdAt?: number;
  expiresAt?: number;
  decision?: string;
  submitting?: string;
  error?: string;
}

const VERDICT_CHIP: Record<string, string> = {
  verified: "established",
  established: "established",
  flawed: "refuted",
  refuted: "refuted",
  inconclusive: "pending",
  failed: "withdrawn",
};

interface AgentLine {
  detail: string;
  tex?: boolean;
  error?: boolean;
}

export interface AgentInfo {
  name: string;
  status: "running" | "done";
  lines: AgentLine[];
  text: string | null;
}

interface LiveText {
  id: string;
  text: string;
}

interface TokenUsage {
  input: number;
  cached: number;
  output: number;
  contextPct: number | null;
}

function prettyExecutor(executorId: string): string {
  if (executorId === "codex") return "Codex CLI";
  if (executorId === "reasoning-native") return "Reasoning Native";
  if (executorId === "claude-code") return "Claude Code";
  if (executorId === "hermes") return "Hermes Agent";
  if (executorId === "opencode") return "OpenCode";
  return executorId;
}

/** 3-significant-figure compact token counts: 1.23M, 45.6k, 999. */
const compactFormat = new Intl.NumberFormat("en", {
  notation: "compact",
  maximumSignificantDigits: 3,
});
const fmtTokens = (value: number): string =>
  compactFormat.format(value).replace("K", "k");

/** Human summary of a ledger tool call from its arguments (and, for the
 *  main agent, its parsed result). Returns null for unknown tools. */
function describeLedgerCall(
  tool: string,
  args: Record<string, unknown>,
  result: Record<string, unknown> | null,
): string | null {
  const s = (v: unknown) => (typeof v === "string" ? v : "");
  const id = (v: unknown) => (typeof v === "string" ? v.slice(0, 12) : "");
  switch (tool) {
    case "ledger_claim": {
      const kind = s(args.kind) === "definition" ? "definition" : "claim";
      const made = id(result?.claim_id);
      const loc = s(args.locator);
      const cited = args.reference_id ? `  [cites ${id(args.reference_id)}${loc ? ` ${loc}` : ""}]` : "";
      return `new ${kind}${made ? ` ${made}` : ""}: ${s(args.statement)}${cited}`;
    }
    case "ledger_refine":
      return `refine ${id(args.claim_id)}: ${s(args.statement)}`;
    case "ledger_reference": {
      const made = id(result?.reference_id);
      return `reference${made ? ` ${made}` : ""}: ${s(args.author)}, ${s(args.title)}${args.year ? ` (${args.year})` : ""}`;
    }
    case "ledger_refine_reference":
      return `update reference ${id(args.reference_id)}`;
    case "ledger_references":
      return "list references";
    case "ledger_search_references":
      return `search references “${s(args.text)}”`;
    case "ledger_prove": {
      const pinned = Array.isArray(args.premises) ? args.premises.length : 0;
      const fresh = Array.isArray(args.new_premises) ? args.new_premises.length : 0;
      const n = pinned + fresh;
      const from = n ? ` from ${n} premise${n === 1 ? "" : "s"}` : "";
      return `prove ${id(args.claim_id)}${from}: ${s(args.argument)}`;
    }
    case "ledger_revise_proof":
      return `revise proof ${id(args.proof_id)}${
        args.argument ? `: ${s(args.argument)}` : " (premises only)"
      }`;
    case "ledger_verify":
      return `${s(args.verdict) || "verdict"} · ${id(args.proof_id ?? args.claim_id)} (authority: ${s(args.authority)})`;
    case "ledger_withdraw":
      return `${args.restore ? "restore" : "withdraw"} ${id(args.claim_id)}`;
    case "ledger_abandon":
      return `${args.resume ? "resume" : "abandon"} proof ${id(args.proof_id)}`;
    case "ledger_status": {
      if (args.claim_id) return `status of ${id(args.claim_id)}`;
      const counts = result?.counts as Record<string, number> | undefined;
      const summary =
        counts && Object.keys(counts).length
          ? Object.entries(counts)
              .map(([status, count]) => `${count} ${status}`)
              .join(", ")
          : "";
      return `status overview${summary ? `: ${summary}` : ""}`;
    }
    case "ledger_search": {
      const matches = result?.matches;
      const hits = Array.isArray(matches)
        ? ` → ${matches.length} match${matches.length === 1 ? "" : "es"}`
        : "";
      return `search “${s(args.text)}”${hits}`;
    }
    case "ledger_blast_radius":
      return `blast radius of ${id(args.claim_id)}`;
    case "ledger_fsck":
      return "fsck";
    default:
      return null;
  }
}

/** Parse the JSON payload out of an MCP tool-call result item. */
function parseToolResult(item: Record<string, unknown>): Record<string, unknown> | null {
  try {
    const result = item.result as Record<string, unknown> | null;
    const structured = result?.structuredContent;
    if (structured && typeof structured === "object" && !Array.isArray(structured)) {
      return structured as Record<string, unknown>;
    }
    const content = result?.content as { text?: string }[] | undefined;
    const text = content?.[0]?.text;
    return typeof text === "string" ? JSON.parse(text) : null;
  } catch {
    return null;
  }
}

/** Plain-text content from a tool result, for the expanded step log. */
function toolResultText(item: Record<string, unknown>): string {
  const result = item.result as Record<string, unknown> | null;
  const content = result?.content;
  if (!Array.isArray(content)) return "";
  return content
    .map((block) => {
      if (!block || typeof block !== "object") return "";
      const text = (block as Record<string, unknown>).text;
      return typeof text === "string" ? text : "";
    })
    .filter(Boolean)
    .join("\n\n");
}

function prettyJson(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value ?? "");
  }
}

/** "gpt-5.6-sol" -> "GPT 5.6-Sol" */
function prettyModel(model: string): string {
  const [head, ...rest] = model.split("-");
  if (head !== "gpt" || rest.length === 0) return model;
  const tail = rest
    .map((part) => (/[a-z]/.test(part[0] ?? "") ? part[0].toUpperCase() + part.slice(1) : part))
    .join("-");
  return `GPT ${tail}`;
}

/** codex wraps every shell call as `/bin/bash -lc '<script>'`; show just
 *  the script. Handles both shell quoting styles; anything that doesn't
 *  match the wrapper shape is shown as-is. */
function displayCommand(raw: string): string {
  const m = raw.match(/^\s*(?:\S*\/)?(?:ba|da|z)?sh\s+-l?c\s+([\s\S]+?)\s*$/);
  if (!m) return raw;
  const script = m[1];
  const quote = script[0];
  if ((quote === '"' || quote === "'") && script.endsWith(quote) && script.length > 1) {
    const inner = script.slice(1, -1);
    if (quote === "'") return inner.replaceAll("'\\''", "'");
    return inner.replace(/\\([\\"$`])/g, "$1");
  }
  return script;
}

function activityFromEvent(event: Record<string, unknown>): Item | null {
  const item = (event.item ?? {}) as Record<string, unknown>;
  const type = item.type as string | undefined;
  if (type === "command_execution") {
    const cmd = (item.command as string) ?? "";
    const cwd = typeof item.cwd === "string" ? item.cwd : "";
    const output =
      typeof item.output === "string"
        ? item.output
        : typeof item.aggregatedOutput === "string"
          ? item.aggregatedOutput
          : "";
    const exitCode = item.exit_code ?? item.exitCode;
    const body = [
      cwd ? `Working directory: ${cwd}` : "",
      output.trimEnd(),
      exitCode !== undefined && exitCode !== null ? `Exit code: ${String(exitCode)}` : "",
    ]
      .filter(Boolean)
      .join("\n\n");
    return {
      kind: "activity",
      label: "$",
      detail: displayCommand(cmd),
      body: body || undefined,
      error: typeof exitCode === "number" && exitCode !== 0,
    };
  }
  if (type === "mcp_tool_call") {
    const server = (item.server as string) ?? "";
    const tool = (item.tool as string) ?? "";
    const args = item.arguments ?? {};
    const result = toolResultText(item);
    const error = item.error;
    const body = [
      args && typeof args === "object" && Object.keys(args as object).length
        ? `Arguments\n${prettyJson(args)}`
        : "",
      result && result !== "{}" ? `Result\n${result}` : "",
      error ? `Error\n${prettyJson(error)}` : "",
    ]
      .filter(Boolean)
      .join("\n\n");
    if (server === "ledger") {
      const detail = describeLedgerCall(
        tool,
        args as Record<string, unknown>,
        parseToolResult(item),
      );
      if (detail)
        return {
          kind: "activity",
          label: "ledger",
          detail,
          tex: true,
          body: body || undefined,
          error: Boolean(error),
        };
    }
    return {
      kind: "activity",
      label: "tool",
      detail: `${server}.${tool}`,
      body: body || undefined,
      error: Boolean(error),
    };
  }
  if (type === "web_search") {
    const query = (item.query as string) ?? "";
    return { kind: "activity", label: "search", detail: query };
  }
  if (type === "file_change") {
    const changes = (item.changes ?? []) as { path?: string }[];
    const count = Array.isArray(changes) ? changes.length : 0;
    const paths = changes
      .map((change) => change.path)
      .filter(Boolean)
      .join(", ");
    return {
      kind: "activity",
      label: "edit",
      detail: paths || (count ? `${count} file change${count === 1 ? "" : "s"}` : "file change"),
      body: prettyJson(changes),
    };
  }
  if (type === "harness_progress") {
    const category = (item.category as string) || "progress";
    return {
      kind: "activity",
      label: "harness",
      detail: (item.detail as string) || category,
    };
  }
  return null;
}

function agentLineFromEvent(event: Record<string, unknown>): AgentLine {
  const detail = (event.detail as string) ?? "";
  const error = event.error === true;
  const dot = detail.indexOf(".");
  const server = dot > 0 ? detail.slice(0, dot) : "";
  if (server === "ledger") {
    const summary = describeLedgerCall(
      detail.slice(dot + 1),
      (event.arguments ?? {}) as Record<string, unknown>,
      null,
    );
    if (summary) return { detail: summary, tex: true, error };
  }
  return { detail, error };
}

// ---- intermediate-step grouping -------------------------------------------
// Runs of reasoning and tool calls between printed outputs collapse to
// a single line (with a spinner while the run is live). Clicking
// expands a scrollable log capped at the viewport, pinned to its bottom
// until the reader scrolls up inside it. Subagent boxes are separators
// like printed outputs: a running agent lives in the dock above the
// input, and its completed box breaks the run where it landed.

interface GroupEntry {
  item: Item;
  index: number;
}

/** Breathing room between an expanded group and the chat view's edges. */
const GROUP_PAD = 8;


type RenderNode =
  | { kind: "plain"; item: Item; index: number }
  | { kind: "group"; entries: GroupEntry[]; start: number };

const groupable = (item: Item) =>
  item.kind === "reasoning" || item.kind === "activity";

/** Turn-ending messages: assistant messages followed only by review or
 *  subagent boxes (or nothing) until the next user message. These are
 *  the answers a returning reader catches up on; the server's read
 *  marker records the newest one whose top the reader has scrolled to. */
function turnEnders(items: Item[]): { tidx: number; index: number }[] {
  const out: { tidx: number; index: number }[] = [];
  let candidate: { tidx: number; index: number } | null = null;
  items.forEach((item, index) => {
    if (item.kind === "user") {
      if (candidate) out.push(candidate);
      candidate = null;
    } else if (item.kind === "assistant") {
      if (item.error) return; // client-only error line, not a message
      candidate = item.tidx === undefined ? null : { tidx: item.tidx, index };
    } else if (
      item.kind !== "review" &&
      item.kind !== "subagent" &&
      item.kind !== "writer"
    ) {
      // reasoning/activity/approval: the turn continued past the message
      candidate = null;
    }
  });
  if (candidate) out.push(candidate);
  return out;
}

function buildNodes(items: Item[]): RenderNode[] {
  const nodes: RenderNode[] = [];
  items.forEach((item, index) => {
    if (groupable(item)) {
      const last = nodes[nodes.length - 1];
      if (last?.kind === "group") last.entries.push({ item, index });
      else nodes.push({ kind: "group", entries: [{ item, index }], start: index });
    } else {
      nodes.push({ kind: "plain", item, index });
    }
  });
  return nodes;
}

/** The one disclosure glyph: it points right when closed and the
 *  container rotates it a quarter turn when open. */
function Chevron() {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 12 12"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="m4.5 2.5 3.5 3.5-3.5 3.5" />
    </svg>
  );
}

function ActivityGroup({
  entries,
  active,
  liveTail,
  open,
  onToggle,
  describe,
  renderStep,
}: {
  entries: GroupEntry[];
  active: boolean;
  liveTail: string | null;
  open: boolean;
  onToggle: () => void;
  describe: (item: Item) => string;
  renderStep: (entry: GroupEntry) => React.ReactNode;
}) {
  const logRef = useRef<HTMLDivElement | null>(null);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const stickRef = useRef(true);
  const [logMax, setLogMax] = useState<number | null>(null);
  const frozenRef = useRef(false);

  // Pinned to the bottom while the reader stays there; scrolling up
  // inside the log stops the pin until they return.
  useEffect(() => {
    if (!open) {
      stickRef.current = true;
      return;
    }
    const node = logRef.current;
    if (node && stickRef.current) node.scrollTop = node.scrollHeight;
    // logMax dep: opening first renders the log uncapped (no overflow, so
    // the pin above is a no-op); once the cap lands and the scrollbar
    // appears, pin again so an opened box starts at its newest step.
  }, [open, entries.length, liveTail, logMax]);

  // An opened log is capped so the whole group fits the visible chat
  // area (viewport minus input row, dock, and padding), and it keeps
  // tracking that area through resizes (window, dock appearing). Once
  // the reader scrolls the group away from its flush position it keeps
  // its size until reopened. Only wheel/touch scrolls can freeze it:
  // programmatic scrolls (our own flush, resize clamping, autoscroll)
  // are not the reader leaving.
  useEffect(() => {
    if (!open) {
      setLogMax(null);
      frozenRef.current = false;
      return;
    }
    const node = rootRef.current;
    const scroller = node?.closest(".chat-scroll") as HTMLElement | null;
    if (!node || !scroller) return;
    const fit = () => {
      const log = logRef.current;
      if (!log) return;
      const chrome = node.offsetHeight - log.offsetHeight;
      setLogMax(Math.max(96, scroller.clientHeight - 2 * GROUP_PAD - chrome));
    };
    const isFlush = () => {
      const box = node.getBoundingClientRect();
      const view = scroller.getBoundingClientRect();
      return (
        box.top <= view.top + GROUP_PAD + 4 &&
        Math.abs(box.bottom - (view.bottom - GROUP_PAD)) < 6
      );
    };
    fit();
    const observer = new ResizeObserver(() => {
      if (!frozenRef.current) fit();
    });
    observer.observe(scroller);
    const userScroll = { pending: false, timer: 0 };
    const markUser = () => {
      userScroll.pending = true;
      // Wheeling inside the inner log never scrolls the chat: expire the
      // mark so it cannot mislabel a later programmatic scroll.
      window.clearTimeout(userScroll.timer);
      userScroll.timer = window.setTimeout(() => {
        userScroll.pending = false;
      }, 150);
    };
    const onScroll = () => {
      if (!userScroll.pending) return;
      userScroll.pending = false;
      if (!isFlush()) frozenRef.current = true;
    };
    scroller.addEventListener("wheel", markUser, { passive: true });
    scroller.addEventListener("touchmove", markUser, { passive: true });
    scroller.addEventListener("scroll", onScroll);
    return () => {
      observer.disconnect();
      scroller.removeEventListener("wheel", markUser);
      scroller.removeEventListener("touchmove", markUser);
      scroller.removeEventListener("scroll", onScroll);
    };
  }, [open]);

  // Once the cap is applied, land the group's bottom flush with the
  // view's bottom edge (a full-height group is then flush on both).
  useLayoutEffect(() => {
    if (!open || logMax === null) return;
    const node = rootRef.current;
    const scroller = node?.closest(".chat-scroll") as HTMLElement | null;
    if (!node || !scroller) return;
    const delta =
      node.getBoundingClientRect().bottom -
      (scroller.getBoundingClientRect().bottom - GROUP_PAD);
    if (delta > 0) scroller.scrollTop += delta;
  }, [open, logMax]);

  const last = entries[entries.length - 1];
  const snippet = liveTail
    ? lastReasoningLine(liveTail)
    : last
      ? describe(last.item)
      : "";
  // The still-streaming step counts: the open log shows it as a line,
  // and a turn's very first step renders as a "1 step" group rather
  // than a differently-shaped bare line.
  const count = entries.length + (liveTail ? 1 : 0);
  return (
    <div ref={rootRef} className={`activity-group ${open ? "open" : ""}`}>
      <div className="group-row" onClick={onToggle}>
        {active ? (
          <span className="dot" />
        ) : (
          <span className="group-caret" aria-hidden="true">
            <Chevron />
          </span>
        )}
        <span className="group-count">
          {count} step{count === 1 ? "" : "s"}
        </span>
        {!open && (
          <span className="group-snippet step-line">
            {last?.item.kind === "reasoning" ? <TeX text={snippet} /> : snippet}
          </span>
        )}
      </div>
      {open && (
        <div
          className="group-log"
          ref={logRef}
          style={logMax !== null ? { maxHeight: logMax } : undefined}
          onScroll={() => {
            const node = logRef.current;
            if (!node) return;
            stickRef.current =
              node.scrollHeight - node.scrollTop - node.clientHeight < 40;
          }}
        >
          {entries.map(renderStep)}
          {liveTail && (
            <div className="group-reasoning live-step step-line">
              {lastReasoningLine(liveTail)}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** Whether an expanded step/subagent/review box fills the visible chat
 *  area: the reader is inside it, so the chat must not autoscroll
 *  underneath them however "at the bottom" the scroller technically
 *  is. Collapsing the box or scrolling away from it clears this. */
function openBoxFillsView(scroller: HTMLElement): boolean {
  const view = scroller.getBoundingClientRect();
  for (const box of scroller.querySelectorAll(
    ".activity-group.open, .dock-agent.inline.open",
  )) {
    const rect = box.getBoundingClientRect();
    const overlap =
      Math.min(rect.bottom, view.bottom) - Math.max(rect.top, view.top);
    if (overlap >= 0.9 * view.height) return true;
  }
  return false;
}

/** The last non-empty line with bold markers stripped — how a reasoning
 *  step reads once it lands, applied to the live stream so a step never
 *  flashes raw markdown while it waits for the next one. */
function lastReasoningLine(text: string): string {
  const lines = text
    .split("\n")
    .map((line) => line.replace(/\*\*/g, "").trim())
    .filter(Boolean);
  return lines[lines.length - 1] ?? "";
}

function ActivityLine({
  label,
  detail,
  tex,
  body,
  error,
}: {
  label: string;
  detail: string;
  tex?: boolean;
  body?: string;
  error?: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div
      className={`activity-line ${tex ? "activity-ledger" : ""} ${error ? "activity-error" : ""} ${expanded ? "expanded" : "step-line"}`}
      onClick={(event) => {
        if ((event.target as HTMLElement).closest("a, .ledger-link")) return;
        setExpanded((current) => !current);
      }}
    >
      <div className="activity-summary">
        <span className="k">{label}</span> {tex ? <TeX text={detail} /> : detail}
      </div>
      {expanded && body && <pre className="activity-detail">{body}</pre>}
    </div>
  );
}

/** A reasoning step starts as one line and expands to the full summary. */
function ReasoningStep({ text }: { text: string }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div
      className={`group-reasoning ${expanded ? "expanded" : "step-line"}`}
      onClick={(event) => {
        if ((event.target as HTMLElement).closest("a, .ledger-link")) return;
        setExpanded((current) => !current);
      }}
    >
      {expanded ? <Markdown text={text} /> : <TeX text={lastReasoningLine(text)} />}
    </div>
  );
}

function ApprovalCard({
  item,
  onDecide,
}: {
  item: ApprovalItem;
  onDecide: (requestId: string, decision: string) => void;
}) {
  const submitting = item.submitting !== undefined;
  return (
    <div className="approval-card">
      <div className="approval-title">
        <span className="status-chip status-pending">approval</span>
        <span>
          codex asks to {item.approvalKind === "file_change" ? "edit files outside the sandbox" : "run a command outside the sandbox"}
        </span>
      </div>
      {item.command && <pre className="approval-command">{item.command}</pre>}
      {item.cwd && (
        <div className="approval-scope">
          working directory <code>{item.cwd}</code>
        </div>
      )}
      {item.reason && <div className="approval-reason">{item.reason}</div>}
      {item.error && (
        <div className="approval-error" role="alert">
          {item.error}
        </div>
      )}
      {item.decision ? (
        <div className="approval-decision">
          {item.decision === "decline" ? "denied" : item.decision === "acceptForSession" ? "approved for session" : "approved"}
        </div>
      ) : (
        <div className="approval-buttons">
          <button
            disabled={submitting}
            onClick={() => onDecide(item.requestId, "accept")}
          >
            {item.submitting === "accept" ? "Approving…" : "Approve"}
          </button>
          <button
            disabled={submitting}
            onClick={() => onDecide(item.requestId, "acceptForSession")}
          >
            {item.submitting === "acceptForSession"
              ? "Approving for session…"
              : "Approve for session"}
          </button>
          <button
            className="deny"
            disabled={submitting}
            onClick={() => onDecide(item.requestId, "decline")}
          >
            {item.submitting === "decline" ? "Denying…" : "Deny"}
          </button>
        </div>
      )}
    </div>
  );
}

/** When a collapsible box opens, scroll the chat view down so the whole
 *  (now-taller) box sits in the viewport — capped so its top never goes
 *  above the view top when it is taller than the viewport. No-op for
 *  boxes outside the scrollable transcript (e.g. the fixed dock). */
function useScrollIntoViewOnOpen(open: boolean) {
  const ref = useRef<HTMLDivElement | null>(null);
  useLayoutEffect(() => {
    if (!open) return;
    const node = ref.current;
    const scroller = node?.closest(".chat-scroll") as HTMLElement | null;
    if (!node || !scroller) return;
    const box = node.getBoundingClientRect();
    const view = scroller.getBoundingClientRect();
    const pad = 8;
    const hidden = box.bottom - (view.bottom - pad);
    if (hidden <= 0) return; // already fully visible
    const keepTop = Math.max(0, box.top - (view.top + pad));
    scroller.scrollTop += Math.min(hidden, keepTop);
  }, [open]);
  return ref;
}

/** Cap an opened box's log so the whole box fills at most the visible
 *  chat area — the same behavior as reasoning step groups. Works for
 *  boxes inside the transcript (the log may grow to the scroll area
 *  minus the box's own chrome) and for dock rows below it (the log may
 *  consume the chat area, which shrinks as it grows — the cap counts
 *  the log's own height back in, so the measure has a fixed point). */
function useLogCap(
  open: boolean,
  rootRef: React.MutableRefObject<HTMLDivElement | null>,
  external?: React.MutableRefObject<HTMLDivElement | null>,
): [React.MutableRefObject<HTMLDivElement | null>, number | null] {
  const own = useRef<HTMLDivElement | null>(null);
  const logRef = external ?? own;
  const [logMax, setLogMax] = useState<number | null>(null);
  useEffect(() => {
    if (!open) {
      setLogMax(null);
      return;
    }
    const node = rootRef.current;
    const scroller = (node?.closest(".chat-scroll") ??
      document.querySelector(".chat-scroll")) as HTMLElement | null;
    if (!node || !scroller) return;
    const inScroller = node.closest(".chat-scroll") !== null;
    const fit = () => {
      const log = logRef.current;
      if (!log) return;
      const cap = inScroller
        ? scroller.clientHeight -
          2 * GROUP_PAD -
          (node.offsetHeight - log.offsetHeight)
        : scroller.clientHeight + log.offsetHeight - 2 * GROUP_PAD;
      setLogMax(Math.max(96, cap));
    };
    fit();
    const observer = new ResizeObserver(fit);
    observer.observe(scroller);
    return () => observer.disconnect();
  }, [open, rootRef]);
  return [logRef, logMax];
}

function AgentBox({
  agent,
  open,
  onToggle,
  inline,
}: {
  agent: AgentInfo;
  open: boolean;
  onToggle: () => void;
  inline?: boolean;
}) {
  const boxRef = useScrollIntoViewOnOpen(open);
  const [logRef, logMax] = useLogCap(open, boxRef);
  const last = agent.lines[agent.lines.length - 1];
  const snippet = last
    ? last.detail
    : agent.text
      ? agent.text.split("\n")[0]
      : "";
  return (
    <div
      ref={boxRef}
      className={`dock-agent ${open ? "open" : ""} ${inline ? "inline" : ""}`}
    >
      <div className="dock-agent-row" onClick={onToggle}>
        <span className="agent-glyph">⑂</span>
        <span className="agent-name">{agent.name || "subagent"}</span>
        {agent.status === "running" ? (
          <span className="thinking">
            <span className="dot" /> running
          </span>
        ) : (
          <span className="status-chip status-established">done</span>
        )}
        {!open && (
          <span className="dock-agent-snippet step-line">
            <TeX text={snippet} />
          </span>
        )}
      </div>
      {open && (
        <div
          className="dock-agent-log"
          ref={logRef}
          style={logMax !== null ? { maxHeight: logMax } : undefined}
        >
          {agent.lines.map((line, i) => (
            <ActivityLine
              key={i}
              label={line.tex ? "ledger" : "tool"}
              detail={line.detail}
              tex={line.tex}
              error={line.error}
            />
          ))}
          {agent.text && (
            <div className="agent-card-body">
              <Markdown text={agent.text} />
            </div>
          )}
          {agent.lines.length === 0 && !agent.text && (
            <div className="empty">no activity yet</div>
          )}
        </div>
      )}
    </div>
  );
}

/** A reviewed document's filename, at the right of a review row where
 *  claim reviews carry their ledger slug; tapping opens the artifact. */
function ArtifactChip({ name }: { name: string }) {
  const artifactName = artifactNameFromPath(name) ?? name;
  return (
    <span className="artifact-inline-reference">
      <span
        className="claim-id ledger-link"
        title={name}
        onClick={(e) => {
          e.stopPropagation();
          openArtifact(artifactName);
        }}
      >
        {name.split("/").pop()}
      </span>
      <RevealArtifactButton name={artifactName} compact />
    </span>
  );
}

/** Inline record of a finished independent review (transcript block). */
function ReviewBox({
  item,
  open,
  onToggle,
}: {
  item: Extract<Item, { kind: "review" }>;
  open: boolean;
  onToggle: () => void;
}) {
  const [showOriginal, setShowOriginal] = useState(false);
  const chip = item.verdict
    ? (VERDICT_CHIP[item.verdict] ?? "pending")
    : item.status === "failed"
      ? "refuted"
      : "pending";
  const repaired = item.latexOriginal !== undefined;
  const shown = showOriginal ? (item.latexOriginal ?? item.text) : item.text;
  const boxRef = useScrollIntoViewOnOpen(open);
  const [logRef, logMax] = useLogCap(open, boxRef);
  return (
    <div
      ref={boxRef}
      className={`dock-agent inline review-box ${open ? "open" : ""}`}
    >
      <div className="dock-agent-row" onClick={onToggle}>
        <span className="agent-glyph">⚖</span>
        <span className="agent-name">review</span>
        <span className={`status-chip status-${chip}`}>
          {item.verdict ?? item.status}
        </span>
        {item.subjectId ? (
          <span
            className="claim-id ledger-link"
            onClick={(e) => {
              e.stopPropagation();
              openLedgerEntry(item.subjectId);
            }}
          >
            {item.subjectId.slice(0, 12)}
          </span>
        ) : item.documentName ? (
          <ArtifactChip name={item.documentName} />
        ) : null}
      </div>
      {open && (
        <div
          className="dock-agent-log"
          ref={logRef}
          style={logMax !== null ? { maxHeight: logMax } : undefined}
        >
          {shown ? (
            <div className="agent-card-body">
              <Markdown text={shown} />
              {repaired && (
                <button
                  className="latex-repair-note"
                  onClick={() => setShowOriginal((v) => !v)}
                >
                  {showOriginal
                    ? "view LaTeX autorepair"
                    : "LaTeX autorepaired — view original"}
                </button>
              )}
            </div>
          ) : (
            <div className="empty">no report</div>
          )}
        </div>
      )}
    </div>
  );
}

/** A running review's row in the dock. Expands to the reviewer's live
 *  reasoning so far — the only window into a writeup review, which has no
 *  ledger slug to open. The step trace is heavy, so the poll never
 *  carries it: an open row fetches (and re-fetches) the review detail
 *  on its own. */
function ReviewDockRow({
  projectId,
  review,
  open,
  onToggle,
}: {
  projectId: string;
  review: ReviewInfo;
  open: boolean;
  onToggle: () => void;
}) {
  const [steps, setSteps] = useState<string[] | null>(null);
  useEffect(() => {
    if (!open) {
      setSteps(null);
      return;
    }
    let cancelled = false;
    const tick = () =>
      fetchReviewDetail(projectId, review.review_id)
        .then((detail) => {
          if (!cancelled) setSteps(detail.steps ?? null);
        })
        .catch(() => {});
    tick();
    const timer = setInterval(tick, 3000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [open, projectId, review.review_id]);

  // An opened step list starts at (and follows) its newest step, until
  // the reader scrolls up inside it.
  const stepsRef = useRef<HTMLDivElement | null>(null);
  const rowRef = useRef<HTMLDivElement | null>(null);
  const [, logMax] = useLogCap(open, rowRef, stepsRef);
  const stickRef = useRef(true);
  useEffect(() => {
    if (!open) {
      stickRef.current = true;
      return;
    }
    const node = stepsRef.current;
    if (node && stickRef.current) node.scrollTop = node.scrollHeight;
  }, [open, steps, logMax]);
  const subject = review.subject.proof_id ?? review.subject.claim_id;
  const live = review.live ? lastReasoningLine(review.live) : "";
  return (
    <div ref={rowRef} className={`dock-agent ${open ? "open" : ""}`}>
      <div className="dock-agent-row" onClick={onToggle}>
        <span className="agent-glyph">⚖</span>
        <span className="agent-name">review</span>
        <span className="thinking">
          <span className="dot" /> {review.model ?? "running"}
        </span>
        {/* the reviewer's latest reasoning step, ellipsized against the
            slug so the row never wraps */}
        {!open && <span className="dock-agent-snippet step-line">{live}</span>}
        {subject ? (
          <span
            className="claim-id ledger-link"
            onClick={(e) => {
              e.stopPropagation();
              openLedgerEntry(subject);
            }}
          >
            {subject.slice(0, 12)}
          </span>
        ) : review.subject.name ? (
          <ArtifactChip name={review.subject.name} />
        ) : null}
      </div>
      {open && (
        <div
          className="review-steps"
          ref={stepsRef}
          style={logMax !== null ? { maxHeight: logMax } : undefined}
          onScroll={() => {
            const node = stepsRef.current;
            if (!node) return;
            stickRef.current =
              node.scrollHeight - node.scrollTop - node.clientHeight < 40;
          }}
        >
          {steps && steps.length ? (
            steps.map((step, i) => <ReasoningStep key={i} text={step} />)
          ) : review.live ? (
            <ReasoningStep text={review.live} />
          ) : (
            <div className="empty">no output yet</div>
          )}
        </div>
      )}
    </div>
  );
}

const WRITER_CHIP: Record<string, string> = {
  running: "pending",
  pending: "pending",
  paused: "definition",
  cancelling: "pending",
  failed: "refuted",
};

/** A writer's row in the dock. Unlike other subagents it does not
 *  leave when it returns: it sits "paused", holding its session, until
 *  the model terminates it. Expands to its reasoning steps so far. */
function WriterDockRow({
  projectId,
  writer,
  open,
  onToggle,
}: {
  projectId: string;
  writer: WriterInfo;
  open: boolean;
  onToggle: () => void;
}) {
  const [steps, setSteps] = useState<string[] | null>(null);
  useEffect(() => {
    if (!open) {
      setSteps(null);
      return;
    }
    let cancelled = false;
    const tick = () =>
      fetchWriterDetail(projectId, writer.writer_id)
        .then((detail) => {
          if (!cancelled) setSteps(detail.steps ?? null);
        })
        .catch(() => {});
    tick();
    const timer = setInterval(tick, 3000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [open, projectId, writer.writer_id]);

  const stepsRef = useRef<HTMLDivElement | null>(null);
  const rowRef = useRef<HTMLDivElement | null>(null);
  const [, logMax] = useLogCap(open, rowRef, stepsRef);
  const stickRef = useRef(true);
  useEffect(() => {
    if (!open) {
      stickRef.current = true;
      return;
    }
    const node = stepsRef.current;
    if (node && stickRef.current) node.scrollTop = node.scrollHeight;
  }, [open, steps, logMax]);
  const running = writer.status === "running" || writer.status === "pending";
  const live = running && writer.live ? lastReasoningLine(writer.live) : "";
  return (
    <div ref={rowRef} className={`dock-agent ${open ? "open" : ""}`}>
      <div className="dock-agent-row" onClick={onToggle}>
        <span className="agent-glyph">✍</span>
        <span className="agent-name">writer</span>
        {running ? (
          <span className="thinking">
            <span className="dot" /> {writer.model ?? "running"}
          </span>
        ) : (
          <span
            className={`status-chip status-${WRITER_CHIP[writer.status] ?? "pending"}`}
          >
            {writer.status}
          </span>
        )}
        {!open && <span className="dock-agent-snippet step-line">{live}</span>}
        <ArtifactChip name={writer.filename} />
      </div>
      {open && (
        <div
          className="review-steps"
          ref={stepsRef}
          style={logMax !== null ? { maxHeight: logMax } : undefined}
          onScroll={() => {
            const node = stepsRef.current;
            if (!node) return;
            stickRef.current =
              node.scrollHeight - node.scrollTop - node.clientHeight < 40;
          }}
        >
          {steps && steps.length ? (
            steps.map((step, i) => <ReasoningStep key={i} text={step} />)
          ) : writer.live ? (
            <ReasoningStep text={writer.live} />
          ) : (
            <div className="empty">no output yet</div>
          )}
        </div>
      )}
    </div>
  );
}

/** Inline record of a writer's return (transcript block): its report
 *  to the main agent, with the document linked at the right. */
function WriterBox({
  item,
  open,
  onToggle,
}: {
  item: Extract<Item, { kind: "writer" }>;
  open: boolean;
  onToggle: () => void;
}) {
  const [showOriginal, setShowOriginal] = useState(false);
  const repaired = item.latexOriginal !== undefined;
  const shown = showOriginal ? (item.latexOriginal ?? item.text) : item.text;
  const boxRef = useScrollIntoViewOnOpen(open);
  const [logRef, logMax] = useLogCap(open, boxRef);
  return (
    <div
      ref={boxRef}
      className={`dock-agent inline review-box ${open ? "open" : ""}`}
    >
      <div className="dock-agent-row" onClick={onToggle}>
        <span className="agent-glyph">✍</span>
        <span className="agent-name">writer</span>
        <span
          className={`status-chip status-${WRITER_CHIP[item.status] ?? "pending"}`}
        >
          {item.status === "paused" ? "returned" : item.status}
        </span>
        <ArtifactChip name={item.filename} />
      </div>
      {open && (
        <div
          className="dock-agent-log"
          ref={logRef}
          style={logMax !== null ? { maxHeight: logMax } : undefined}
        >
          {shown ? (
            <div className="agent-card-body">
              <Markdown text={shown} />
              {repaired && (
                <button
                  className="latex-repair-note"
                  onClick={() => setShowOriginal((v) => !v)}
                >
                  {showOriginal
                    ? "view LaTeX autorepair"
                    : "LaTeX autorepaired — view original"}
                </button>
              )}
            </div>
          ) : (
            <div className="empty">no report</div>
          )}
        </div>
      )}
    </div>
  );
}

/** The entry bar. Owns the draft text locally so typing re-renders only
 *  this small component, never the (KaTeX-heavy) transcript above it.
 *  Attached files upload to the project's artifacts folder as they are
 *  picked; sending cites their artifact paths in the message. */
function ChatInput({
  draftKey,
  busy,
  reviewsRunning,
  executionOptions,
  selection,
  preflight,
  usage,
  estimatedCostUsd,
  projectId,
  chatId,
  onSelection,
  onSend,
  onStop,
  onOpenConnections,
}: {
  draftKey: string;
  busy: boolean;
  /** In-flight reviewers count as running work: Stop shows (and cancels
   *  them) even when the main model is idle. */
  reviewsRunning: boolean;
  executionOptions: ChatExecutionOptions;
  selection: SelectedRunConfiguration;
  preflight: ExecutionPreflight | null;
  usage: TokenUsage | null;
  estimatedCostUsd: number | null;
  projectId: string;
  chatId: string;
  onSelection: (selection: SelectedRunConfiguration) => void;
  onSend: (message: string) => void;
  onStop: () => void;
  onOpenConnections: () => void;
}) {
  const readDraft = () => {
    try {
      return localStorage.getItem(draftKey) ?? "";
    } catch {
      return "";
    }
  };
  const saveDraft = (value: string) => {
    try {
      if (value) localStorage.setItem(draftKey, value);
      else localStorage.removeItem(draftKey);
    } catch {
      // A restricted webview may disable storage; the in-memory draft
      // still behaves normally for the lifetime of this chat view.
    }
  };
  const [input, setInput] = useState(readDraft);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [configurationOpen, setConfigurationOpen] = useState(false);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const harness = executionOptions.harnesses.find(
    (candidate) => candidate.harness_id === selection.harness_id,
  );
  const route = executionOptions.inference_routes.find(
    (candidate) => candidate.route_id === selection.inference_route_id,
  );
  const target = executionOptions.execution_targets.find(
    (candidate) => candidate.target_id === selection.target_id,
  );
  const profile = modelProfile(selection.harness_id, selection.model);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [attachments, setAttachments] = useState<
    { path: string; name: string }[]
  >([]);
  const [uploading, setUploading] = useState(0);

  // Prompt Studio can hand a drafting request to the normal Reasoning chat
  // without sending it automatically; the operator sees and edits the exact
  // request before choosing to spend a turn on it.
  useEffect(() => {
    const receiveDraft = (event: Event) => {
      const text = (event as CustomEvent<string>).detail;
      if (!text) return;
      setInput(text);
      saveDraft(text);
      window.setTimeout(() => inputRef.current?.focus(), 0);
    };
    window.addEventListener("chat-draft", receiveDraft);
    return () => window.removeEventListener("chat-draft", receiveDraft);
  }, []);

  // Grow the input to fit its content; past the CSS max-height cap the
  // clamp kicks in and the text scrolls inside instead.
  useEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
  }, [input]);

  const attach = (files: FileList | null) => {
    for (const file of Array.from(files ?? [])) {
      setUploading((n) => n + 1);
      uploadArtifact(projectId, file)
        .then((stored) =>
          setAttachments((prev) =>
            prev.some((a) => a.path === stored.path) ? prev : [...prev, stored],
          ),
        )
        .catch(() => {})
        .finally(() => setUploading((n) => n - 1));
    }
  };

  const send = () => {
    const message = input.trim();
    if ((!message && attachments.length === 0) || uploading > 0) return;
    const cited = attachments.map((a) => `attached: ${a.path}`).join("\n");
    setInput("");
    saveDraft("");
    setAttachments([]);
    onSend(message && cited ? `${message}\n\n${cited}` : message || cited);
  };

  // On phones Enter makes a newline (the send button sends): there is no
  // Shift key, and accidental sends are costly mid-turn.
  const mobile = window.matchMedia("(max-width: 768px)").matches;
  const codexBacked = selection.harness_id === "codex";
  const externalBusy = busy && !codexBacked;
  const connectionReady = route?.ready ?? false;
  const configurationReady = preflight?.compatible ?? false;

  return (
    <div className="chat-input-box">
      <textarea
        ref={inputRef}
        rows={1}
        placeholder={
          !connectionReady
            ? "Set up the selected connection to continue…"
            : !configurationReady
              ? "Resolve the selected run configuration to continue…"
            : mobile
            ? busy
              ? externalBusy
                ? "The selected agent is still running…"
                : "Steer the running turn…"
              : "Ask anything about this project…"
            : busy
              ? externalBusy
                ? "The selected agent is still running…"
                : "Steer the running turn…  (Enter to send)"
              : "Ask anything about this project…"
        }
        value={input}
        title={
          mobile ? undefined : "Enter to send, Shift+Enter for a new line"
        }
        onChange={(e) => {
          setInput(e.target.value);
          saveDraft(e.target.value);
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey && !mobile) {
            e.preventDefault();
            if (configurationReady) send();
            else if (!connectionReady) onOpenConnections();
          }
        }}
      />
      {(attachments.length > 0 || uploading > 0) && (
        <div className="attach-row">
          {attachments.map((a) => (
            <span key={a.path} className="attach-chip" title={a.path}>
              {a.name}
              <button
                className="attach-remove"
                type="button"
                aria-label={`Remove ${a.name}`}
                title="remove attachment"
                onClick={() =>
                  setAttachments((prev) =>
                    prev.filter((x) => x.path !== a.path),
                  )
                }
              >
                ×
              </button>
            </span>
          ))}
          {uploading > 0 && (
            <span className="attach-chip attach-uploading">uploading…</span>
          )}
        </div>
      )}
      {configurationOpen && (
        <div className="chat-configuration-panel" id="chat-run-configuration">
          <RunConfigurationControls
            options={executionOptions}
            selection={selection}
            preflight={preflight}
            disabled={busy}
            onChange={onSelection}
          />
          <AutoRoutingControl
            source="chat"
            projectId={projectId}
            resourceId={chatId}
            prompt={input}
            reasoningEffort={selection.reasoning_effort}
            disabled={busy}
            onSelect={({ provider: _provider, ...selected }) => onSelection(selected)}
          />
        </div>
      )}
      <div className="chat-input-controls">
        <button
          className="attach-btn"
          type="button"
          aria-label="Attach files"
          title="attach a file (stored in the project's artifacts)"
          onClick={() => fileRef.current?.click()}
        >
          <PlusIcon />
        </button>
        <input
          ref={fileRef}
          type="file"
          multiple
          style={{ display: "none" }}
          onChange={(e) => {
            attach(e.target.files);
            e.target.value = "";
          }}
        />
        <button
          className={`chat-configuration-summary ${configurationOpen ? "active" : ""}`}
          type="button"
          aria-expanded={configurationOpen}
          aria-controls="chat-run-configuration"
          onClick={() => setConfigurationOpen((open) => !open)}
        >
          <span className={`configuration-status ${configurationReady ? "ready" : "blocked"}`} aria-hidden="true" />
          <span>{harness?.display_name ?? "Choose agent"}</span>
          <i aria-hidden="true">·</i>
          <span className="mono">{selection.model || "Choose model"}</span>
          {selection.reasoning_effort && <><i aria-hidden="true">·</i><span>{selection.reasoning_effort.replace("xhigh", "extra-high")}</span></>}
          <svg aria-hidden="true" width="12" height="12" viewBox="0 0 12 12"><path d="m3 4.5 3 3 3-3" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" /></svg>
        </button>
        <button
          className={`chat-details-toggle ${detailsOpen ? "active" : ""}`}
          type="button"
          aria-label={detailsOpen ? "Hide agent and session details" : "Show agent and session details"}
          aria-expanded={detailsOpen}
          aria-controls="chat-input-details"
          title={detailsOpen ? "Hide agent and session details" : "Show agent and session details"}
          onClick={() => setDetailsOpen((open) => !open)}
        >
          <svg
            aria-hidden="true"
            width="15"
            height="15"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.8"
            strokeLinecap="round"
          >
            <circle cx="12" cy="12" r="8.5" />
            <path d="M12 11v5.5M12 7.8v.6" />
          </svg>
        </button>
        {!configurationReady && (
          <button
            type="button"
            className="composer-setup-link"
            onClick={onOpenConnections}
          >
            {connectionReady ? "Fix setup" : "Set up"}
          </button>
        )}
        <span className="ctrl-spacer" />
        {((busy && harness?.supports_interrupt) || reviewsRunning) && (
          <button
            type="button"
            className="stop-btn"
            aria-label="Stop"
            title="Stop"
            onClick={onStop}
          >
            <svg aria-hidden="true" width="11" height="11" viewBox="0 0 12 12">
              <rect x="1.5" y="1.5" width="9" height="9" rx="1.6" fill="currentColor" />
            </svg>
          </button>
        )}
        <button
          type="button"
          className="composer-send"
          aria-label="Send"
          title="Send"
          disabled={
            !configurationReady ||
            !input.trim() && attachments.length === 0 ||
            uploading > 0 ||
            !selection.model.trim() ||
            externalBusy
          }
          onClick={send}
        >
          <svg
            aria-hidden="true"
            width="16"
            height="16"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2.3"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <path d="M12 19.5V5M5.5 11.5 12 5l6.5 6.5" />
          </svg>
        </button>
      </div>
      {detailsOpen && (
        <div className="chat-input-details" id="chat-input-details">
          <div className="chat-input-detail">
            <span>provider route</span>
            <strong className={route?.ready ? "ready" : "needs-setup"}>
              {route?.ready ? "ready" : "setup required"}
            </strong>
            <small>{route?.readiness ?? "Choose an inference route"}</small>
          </div>
          {selection.model && (
            <div className="chat-input-detail">
              <span>model</span>
              <strong>{profile.cost}</strong>
              <small>{profile.context}</small>
            </div>
          )}
          {harness && (
            <div className="chat-input-detail">
              <span>harness</span>
              <strong>{harness.display_name}</strong>
              <small>{harness.prompt_fidelity.replaceAll("_", " ")} prompt</small>
            </div>
          )}
          {target && (
            <div className="chat-input-detail">
              <span>target</span>
              <strong>{target.display_name}</strong>
              <small>{target.isolation.replaceAll("_", " ")} isolation</small>
            </div>
          )}
          {preflight && !preflight.compatible && (
            <div className="chat-input-detail">
              <span>preflight</span>
              <strong className="needs-setup">blocked</strong>
              <small>{preflight.issues.map((issue) => issue.detail).join(" · ")}</small>
            </div>
          )}
          {usage && (
            <div className="chat-input-detail">
              <span>session</span>
              <strong>
                {fmtTokens(usage.input)} in · {fmtTokens(usage.output)} out
              </strong>
              <small>
                {usage.cached ? `${fmtTokens(usage.cached)} cached` : "no cached tokens"}
                {usage.contextPct !== null ? ` · ${usage.contextPct}% context` : ""}
                {estimatedCostUsd !== null
                  ? ` · ${formatEstimatedCost(estimatedCostUsd)} estimated`
                  : ""}
              </small>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** Trailing "attached: artifacts/<name>" lines (the composer appends
 *  them for message attachments) split off the message body, so they
 *  can render as artifact links rather than plain text. */
function splitAttachments(text: string): { body: string; attached: string[] } {
  const lines = text.split("\n");
  const attached: string[] = [];
  while (lines.length > 0) {
    const last = lines[lines.length - 1].trim();
    if (last === "") {
      lines.pop();
      continue;
    }
    const match = last.match(/^attached: artifacts\/(\S+)$/);
    if (!match) break;
    attached.unshift(match[1]);
    lines.pop();
  }
  return { body: lines.join("\n").trimEnd(), attached };
}

/** When a message was recorded, shown lightly in its bottom-right
 *  corner on mouseover — or on tap, which toggles it (mobile has no
 *  hover). */
function MessageStamp({
  at,
  shown,
}: {
  at: string | undefined;
  shown: boolean;
}) {
  if (!at) return null;
  const date = new Date(at);
  if (isNaN(date.getTime())) return null;
  const time = date.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
  });
  const label =
    date.toDateString() === new Date().toDateString()
      ? time
      : `${date.toLocaleDateString([], { month: "short", day: "numeric" })} ${time}`;
  return (
    <span className={`msg-stamp ${shown ? "shown" : ""}`} title={at}>
      {label}
    </span>
  );
}

/** Tapping a message shows its stamp; it stays until the user taps
 *  anywhere outside the message. Clicks on links and buttons pass. */
function useStampTap(): [
  React.MutableRefObject<HTMLDivElement | null>,
  boolean,
  (e: React.MouseEvent) => void,
] {
  const ref = useRef<HTMLDivElement | null>(null);
  const [shown, setShown] = useState(false);
  const onClick = (e: React.MouseEvent) => {
    if ((e.target as HTMLElement).closest("a, button, .ledger-link")) return;
    setShown(true);
  };
  useEffect(() => {
    if (!shown) return;
    const dismiss = (e: MouseEvent | TouchEvent) => {
      const node = ref.current;
      if (node && !node.contains(e.target as Node)) setShown(false);
    };
    document.addEventListener("click", dismiss);
    document.addEventListener("touchstart", dismiss);
    return () => {
      document.removeEventListener("click", dismiss);
      document.removeEventListener("touchstart", dismiss);
    };
  }, [shown]);
  return [ref, shown, onClick];
}

function UserMessage({ item }: { item: Extract<Item, { kind: "user" }> }) {
  const { body, attached } = splitAttachments(item.text);
  const [stampRef, stampShown, onStampTap] = useStampTap();
  return (
    <div
      ref={stampRef}
      className={`msg-user ${item.steered ? "steered" : ""}`}
      onClick={onStampTap}
    >
      {item.steered && <span className="steer-tag">mid-turn</span>}
      {item.importedCheckpointId && (
        <ImportedHandoffSource
          checkpointId={item.importedCheckpointId}
          sourceChatId={item.sourceChatId}
        />
      )}
      {body}
      {attached.map((name) => (
        <div key={name} className="msg-attachment">
          📎{" "}
          <span className="artifact-inline-reference">
            <span className="ledger-link" onClick={() => openArtifact(name)}>
              {name}
            </span>
            <RevealArtifactButton name={name} compact />
          </span>
        </div>
      ))}
      <MessageStamp at={item.at} shown={stampShown} />
    </div>
  );
}

function ImportedHandoffSource({
  checkpointId,
  sourceChatId,
}: {
  checkpointId: string;
  sourceChatId?: string;
}) {
  const source = sourceChatId ? ` from ${sourceChatId}` : "";
  return (
    <div
      className="handoff-source"
      title={`Imported checkpoint ${checkpointId}${source}`}
      aria-label={`Imported checkpoint ${checkpointId}${source}`}
    >
      imported handoff{sourceChatId ? ` · ${sourceChatId}` : ""}
    </div>
  );
}

type PortableHandoffPhase = "idle" | "exporting" | "review" | "handing-off";

export function PortableHandoffControl({
  projectId,
  chatId,
  operationId,
  workspaceId,
  onCreated,
}: {
  projectId: string;
  chatId: string;
  operationId: string;
  workspaceId?: string | null;
  onCreated: (chatId: string) => void;
}) {
  const [phase, setPhase] = useState<PortableHandoffPhase>("idle");
  const [checkpoint, setCheckpoint] = useState<PortableCheckpoint | null>(null);
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);

  const createDestination = async (value: PortableCheckpoint) => {
    setPhase("handing-off");
    const handoff = await handoffPortableCheckpoint(
      projectId,
      chatId,
      value.checkpoint_id,
      { workspace_id: workspaceId ?? null },
    );
    onCreated(handoff.chat.id);
  };

  const begin = async () => {
    setError(null);
    setPhase("exporting");
    try {
      const value = await exportPortableCheckpoint(projectId, chatId, operationId);
      setCheckpoint(value);
      if (value.automatic_failover.allowed) await createDestination(value);
      else setPhase("review");
    } catch (cause) {
      setPhase("idle");
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  };

  const promoteAndContinue = async () => {
    if (!checkpoint || !reason.trim()) return;
    setError(null);
    setPhase("handing-off");
    try {
      const promoted = await promotePortableCheckpoint(
        projectId,
        chatId,
        checkpoint.checkpoint_id,
        reason.trim(),
      );
      await createDestination(promoted);
    } catch (cause) {
      setPhase("review");
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  };

  return (
    <div className="portable-handoff" onClick={(event) => event.stopPropagation()}>
      {phase === "idle" && (
        <button type="button" className="portable-handoff-trigger" onClick={() => void begin()}>
          continue in a new chat
        </button>
      )}
      {phase === "exporting" && <span className="portable-handoff-status">preparing handoff…</span>}
      {phase === "handing-off" && <span className="portable-handoff-status">creating new chat…</span>}
      {phase === "review" && checkpoint && (
        <div className="portable-handoff-review">
          <div className="portable-handoff-warning">
            Review required: {checkpoint.automatic_failover.reason}
          </div>
          <label>
            Why is it safe to continue after these effects?
            <textarea
              value={reason}
              rows={2}
              onChange={(event) => setReason(event.target.value)}
            />
          </label>
          <div className="portable-handoff-actions">
            <button
              type="button"
              onClick={() => {
                setPhase("idle");
                setCheckpoint(null);
              }}
            >
              cancel
            </button>
            <button
              type="button"
              className="confirm"
              disabled={!reason.trim()}
              onClick={() => void promoteAndContinue()}
            >
              attest and continue
            </button>
          </div>
        </div>
      )}
      {error && <div className="portable-handoff-error" role="alert">{error}</div>}
    </div>
  );
}

/** An assistant message; when its LaTeX was autorepaired for display, a
 *  caption toggles between the repaired copy and the author's original. */
/** A failed turn's text arrives as an "**error:** …" line written for a
 *  markdown reader. It renders beside a chip that already says "error", so
 *  drop that opening marker and render the rest as a plain line, keeping
 *  backticked spans as code rather than printing their backticks. */
function errorLine(text: string): React.ReactNode[] {
  const body = text.replace(/^\s*(?:\*\*)?error:(?:\*\*)?\s*/i, "").trim();
  return body
    .split(/(`[^`]+`)/)
    .filter((part) => part !== "")
    .map((part, index) =>
      part.length > 1 && part.startsWith("`") && part.endsWith("`") ? (
        <code key={index}>{part.slice(1, -1)}</code>
      ) : (
        part
      ),
    );
}

function AssistantMessage({
  item,
  handoff,
}: {
  item: Extract<Item, { kind: "assistant" }>;
  handoff?: {
    projectId: string;
    chatId: string;
    workspaceId?: string | null;
    onCreated: (chatId: string) => void;
  };
}) {
  const [showOriginal, setShowOriginal] = useState(false);
  const [stampRef, stampShown, onStampTap] = useStampTap();
  const repaired = item.latexOriginal !== undefined;
  const shown = showOriginal ? (item.latexOriginal ?? item.text) : item.text;
  return (
    <div
      ref={stampRef}
      className="msg-assistant"
      onClick={onStampTap}
    >
      {item.executorId && (
        <div className="assistant-source">
          {prettyExecutor(item.executorId)}{item.model ? ` · ${prettyModel(item.model)}` : ""}
        </div>
      )}
      {item.importedCheckpointId && (
        <ImportedHandoffSource
          checkpointId={item.importedCheckpointId}
          sourceChatId={item.sourceChatId}
        />
      )}
      {item.error ? (
        <div className="msg-error-line">
          <span className="status-chip status-refuted">error</span>
          <span>{errorLine(shown)}</span>
        </div>
      ) : (
        <Markdown text={shown} />
      )}
      {repaired && (
        <button
          className="latex-repair-note"
          onClick={() => setShowOriginal((v) => !v)}
        >
          {showOriginal ? "view LaTeX autorepair" : "LaTeX autorepaired — view original"}
        </button>
      )}
      {handoff && item.operationId && !item.error && (
        <PortableHandoffControl
          {...handoff}
          operationId={item.operationId}
        />
      )}
      <MessageStamp at={item.at} shown={stampShown} />
    </div>
  );
}

const TASK_STATUS_GLYPH: Record<string, string> = {
  pending: "○",
  in_progress: "◐",
  completed: "✓",
};

/** The agent's task list, shown above the input. Two states: collapsed
 *  to a single row (the active task, or the top one when none is active)
 *  and open to the full list (which may grow past the shared budget and
 *  scroll). The collapse state is owned by the parent so it can persist
 *  per chat and shrink the subagent dock; it takes priority over that
 *  dock for the shared vertical budget. */
/** A goal's one-line rendering: "Goal: <text>" plus its mark. */
function GoalLine({ goal }: { goal: GoalInfo }) {
  return (
    <span className={`goal-line goal-${goal.status}`}>
      <span className="goal-prefix">Goal:</span> {goal.text}
      {goal.status !== "active" && (
        <span className="goal-mark">
          {" — "}
          {goal.status}
          {goal.reason ? `: ${goal.reason}` : ""}
        </span>
      )}
    </span>
  );
}

function TaskDock({
  tasks,
  goal,
  collapsed,
  onToggle,
}: {
  tasks: TaskInfo[];
  goal: GoalInfo | null;
  collapsed: boolean;
  onToggle: () => void;
}) {
  if (tasks.length === 0 && !goal) return null;
  const done = tasks.filter((t) => t.status === "completed").length;
  const active = tasks.find((t) => t.status === "in_progress");
  const headline = active ?? tasks[0];
  return (
    <div className={`task-dock ${collapsed ? "collapsed" : "expanded"}`}>
      <div className="task-dock-head" onClick={onToggle}>
        <span className="task-caret" aria-hidden="true">
          <Chevron />
        </span>
        <span className="task-dock-title">
          {tasks.length > 0 ? "tasks" : "goal"}
        </span>
        {tasks.length > 0 && (
          <span className="task-dock-count">
            {done}/{tasks.length}
          </span>
        )}
        {collapsed &&
          // the active task headlines; with none running, the goal does
          (active || !goal ? (
            headline && (
              <span className={`task-head-active task-${headline.status}`}>
                <span className="task-glyph">
                  {TASK_STATUS_GLYPH[headline.status] ?? "○"}
                </span>
                <span className="task-title">{headline.title}</span>
              </span>
            )
          ) : (
            <span className="task-head-active">
              <GoalLine goal={goal} />
            </span>
          ))}
      </div>
      {!collapsed && (
        <div className="task-list">
          {tasks.map((task, i) => (
            <div key={i} className={`task-row task-${task.status}`}>
              <span className="task-glyph">
                {TASK_STATUS_GLYPH[task.status] ?? "○"}
              </span>
              <span className="task-title">{task.title}</span>
            </div>
          ))}
          {goal && (
            <div
              className={`task-row goal-row ${tasks.length === 0 ? "goal-only" : ""}`}
            >
              <GoalLine goal={goal} />
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** The dock above the input box holds RUNNING work only — subagents and
 *  in-flight reviews; completed ones move inline into the chat. Up to
 *  `cap` rows show individually; beyond that the whole dock collapses to
 *  a single "N running" row (expanding to all, scrollable) so it can
 *  never eat the screen. The task dock shrinks `cap` as it needs room. */
function SubagentDock({
  projectId,
  agents,
  order,
  reviews,
  writers,
  expanded,
  onToggle,
  cap,
}: {
  projectId: string;
  agents: Record<string, AgentInfo>;
  order: string[];
  reviews: ReviewInfo[];
  writers: WriterInfo[];
  expanded: string | null;
  onToggle: (id: string) => void;
  /** Max rows shown before collapsing the rest into a "+N more" row. The
   *  task dock shrinks this so the two share one vertical budget. */
  cap: number;
}) {
  const [showAll, setShowAll] = useState(false);
  const rows: { key: string; node: React.ReactNode }[] = [];
  for (const id of order) {
    if (!agents[id]) continue;
    rows.push({
      key: id,
      node: (
        <AgentBox
          key={id}
          agent={agents[id]}
          open={expanded === id}
          onToggle={() => onToggle(id)}
        />
      ),
    });
  }
  for (const review of reviews) {
    rows.push({
      key: review.review_id,
      node: (
        <ReviewDockRow
          key={review.review_id}
          projectId={projectId}
          review={review}
          open={expanded === review.review_id}
          onToggle={() => onToggle(review.review_id)}
        />
      ),
    });
  }
  for (const writer of writers) {
    rows.push({
      key: writer.writer_id,
      node: (
        <WriterDockRow
          key={writer.writer_id}
          projectId={projectId}
          writer={writer}
          open={expanded === writer.writer_id}
          onToggle={() => onToggle(writer.writer_id)}
        />
      ),
    });
  }
  if (rows.length === 0) return null;
  const overflow = rows.length > cap;
  // Collapsed, the dock is exactly one row that stands for every running
  // item; expanded, it lists them all (and scrolls).
  const visible = overflow && !showAll ? [] : rows;
  return (
    <div className="agent-dock">
      {visible.map((row) => row.node)}
      {overflow && (
        <div className="dock-agent dock-agent-more">
          <div className="dock-agent-row" onClick={() => setShowAll((s) => !s)}>
            <span className="agent-glyph">⋯</span>
            <span className="agent-name">
              {showAll ? "show less" : `${rows.length} running`}
            </span>
          </div>
        </div>
      )}
    </div>
  );
}

/** Transcript entries -> render items (+ inline completed subagents).
 *  `startIndex` is the absolute transcript index of messages[0], so
 *  tidx-based read markers line up on partial (turns=) fetches. */
function buildHistoryItems(
  messages: ChatEntry[],
  startIndex: number,
): { items: Item[]; agents: Record<string, AgentInfo> } {
  const agents: Record<string, AgentInfo> = {};
  const items: Item[] = [];
  messages.forEach((entry, offset) => {
    const i = startIndex + offset;
    // every item carries its transcript index as its stable render key
    const push = (item: ItemVariant) => items.push({ ...item, k: i });
    if (entry.role === "subagent") {
      // Completed agents render inline at their transcript position.
      const key = `history-${i}`;
      agents[key] = {
        name: entry.name ?? "subagent",
        status: "done",
        lines: [],
        text: entry.text || null,
      };
      push({ kind: "subagent", threadId: key });
    } else if (entry.role === "reasoning") {
      push({ kind: "reasoning", text: entry.text });
    } else if (entry.role === "activity") {
      const activity = activityFromEvent({ item: entry.item ?? {} });
      if (activity) push(activity);
    } else if (entry.role === "review") {
      push({
        kind: "review",
        reviewId: entry.review_id ?? `history-review-${i}`,
        status: entry.status ?? "completed",
        verdict: entry.verdict ?? null,
        subjectId: entry.subject?.proof_id ?? entry.subject?.claim_id ?? "",
        documentName: entry.subject?.name,
        text: entry.latex_repaired ?? entry.text,
        latexOriginal: entry.latex_repaired ? entry.text : undefined,
      });
    } else if (entry.role === "writer") {
      push({
        kind: "writer",
        writerId: entry.writer_id ?? "",
        writerKey: entry.writer_key ?? `history-writer-${i}`,
        filename: entry.filename ?? "",
        status: entry.status ?? "paused",
        activation: entry.activation ?? 1,
        text: entry.latex_repaired ?? entry.text,
        latexOriginal: entry.latex_repaired ? entry.text : undefined,
        at: entry.at,
      });
    } else if (entry.role === "error") {
      push({
        kind: "assistant",
        text: `**error:** ${entry.text}`,
        at: entry.at,
        error: true,
      });
    } else if (entry.role === "user") {
      push({
        kind: "user",
        text: entry.text,
        steered: entry.steered,
        at: entry.at,
        importedCheckpointId: entry.imported_checkpoint_id,
        sourceChatId: entry.source_chat_id,
      });
    } else {
      // a repaired message shows the repaired text with the original
      // one toggle away
      push({
        kind: "assistant",
        text: entry.latex_repaired ?? entry.text,
        at: entry.at,
        messageId: entry.message_id,
        tidx: i,
        latexOriginal: entry.latex_repaired ? entry.text : undefined,
        executorId: entry.executor_id,
        model: entry.model,
        operationId: entry.operation_id,
        importedCheckpointId: entry.imported_checkpoint_id,
        sourceChatId: entry.source_chat_id,
      });
    }
  });
  return { items, agents };
}

export function Chat({
  projectId,
  chatId,
  chat,
  onLedgerActivity,
  onChatStatusChange,
  onTurnCompleted,
  onOpenConnections,
  onHandoffCreated,
}: {
  projectId: string;
  chatId: string;
  chat: ChatInfo;
  onLedgerActivity: () => void;
  onChatStatusChange: (phase: "turn.started" | "turn.completed" | "activity") => void;
  onTurnCompleted: (workflows: { workflow_id: string; revision_id: string }[]) => void;
  onOpenConnections: () => void;
  onHandoffCreated: (chatId: string) => void;
}) {
  const isDevelopment = chat.mode === "development";
  const [items, setItems] = useState<Item[]>([]);
  const [approvals, setApprovals] = useState<ApprovalItem[]>([]);
  const itemsRef = useRef<Item[]>([]);
  itemsRef.current = items;
  const [openGroups, setOpenGroups] = useState<Set<number>>(new Set());
  // a group's stable identity: its first entry's transcript index when
  // known (survives the quick-paint -> full-backfill swap), else its
  // array position
  const groupKey = (node: Extract<RenderNode, { kind: "group" }>): number =>
    node.entries[0]?.item.k ?? node.start;
  const [reviews, setReviews] = useState<ReviewInfo[]>([]);
  const [writers, setWriters] = useState<WriterInfo[]>([]);
  const [tasks, setTasks] = useState<TaskInfo[]>([]);
  const [goal, setGoal] = useState<GoalInfo | null>(null);
  const [agents, setAgents] = useState<Record<string, AgentInfo>>({});
  const [agentOrder, setAgentOrder] = useState<string[]>([]);
  const [expandedAgent, setExpandedAgent] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [liveMessage, setLiveMessage] = useState<LiveText | null>(null);
  const [liveReasoning, setLiveReasoning] = useState<LiveText | null>(null);
  const [usage, setUsage] = useState<TokenUsage | null>(null);
  const [reviewUsage, setReviewUsage] = useState<{
    input: number;
    cached: number;
    output: number;
  } | null>(null);
  const [model, setModel] = useState("");
  const [executionOptions, setExecutionOptions] =
    useState<ChatExecutionOptions | null>(null);
  const [runSelection, setRunSelectionState] =
    useState<SelectedRunConfiguration | null>(null);
  const [executionPreflight, setExecutionPreflight] =
    useState<ExecutionPreflight | null>(null);
  const setRunSelection = (selection: SelectedRunConfiguration) => {
    setRunSelectionState(selection);
    localStorage.setItem("workbench-chat-harness", selection.harness_id);
    localStorage.setItem("workbench-chat-executor", selection.harness_id);
    localStorage.setItem("workbench-chat-route", selection.inference_route_id);
    localStorage.setItem("workbench-chat-model", selection.model);
    if (selection.reasoning_effort) {
      localStorage.setItem("workbench-effort", selection.reasoning_effort);
    }
    if (isDevelopment) {
      localStorage.setItem("workbench-codex-model", selection.model);
    }
  };
  const [threadId, setThreadId] = useState<string | null>(null);
  // Task list open/closed, remembered per chat and browser — including
  // across spells where the list is empty. Starts open.
  const tasksCollapsedKey = `tasks-collapsed-${projectId}/${chatId}`;
  const [tasksCollapsed, setTasksCollapsedState] = useState(false);
  useEffect(() => {
    setTasksCollapsedState(localStorage.getItem(tasksCollapsedKey) === "1");
  }, [tasksCollapsedKey]);
  const toggleTasksCollapsed = () =>
    setTasksCollapsedState((current) => {
      const next = !current;
      localStorage.setItem(tasksCollapsedKey, next ? "1" : "0");
      return next;
    });
  // `loading` covers the initial history fetch (a "loading…" line).
  // Only the last turns load eagerly; older history pages in on demand
  // when the reader scrolls up (or taps the load-earlier row).
  const [loading, setLoading] = useState(true);
  const [hasMore, setHasMore] = useState(false);
  const hasMoreRef = useRef(false);
  hasMoreRef.current = hasMore;
  const [paging, setPaging] = useState(false);
  const pagingRef = useRef(false);
  // absolute transcript index of the oldest loaded history item
  const historyStartRef = useRef(0);
  // scroll compensation for a prepended page (see the layout effect)
  const pendingPrependRef = useRef<{ height: number; top: number } | null>(
    null,
  );
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const completedAgentsRef = useRef<Set<string>>(new Set());
  // The event handler closes over fresh state each render; the stream
  // callback goes through this ref so one EventSource serves the whole
  // chat lifetime.
  const handleEventRef = useRef<(event: Record<string, unknown>) => void>(
    () => {},
  );
  const authoredWorkflowsRef = useRef<Map<string, string>>(new Map());
  const sourceRef = useRef<EventSource | null>(null);
  const loadSeqRef = useRef(0);
  const lastEventAtRef = useRef(Date.now());
  // Reading-position state: whether the view sits at the bottom, and the
  // key of the newest block the anchor logic has already handled.
  const atBottomRef = useRef(true);
  const anchorKeyRef = useRef("");
  const suppressAnchorRef = useRef(false);
  // The chat's shared read marker (transcript index of the newest
  // turn-ending message reached; null = never tracked → all read), and a
  // one-shot load target: the first unread turn end to land the view on.
  const readIndexRef = useRef<number | null>(null);
  const pendingReadRef = useRef<number | null>(null);
  const [showJump, setShowJump] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const refreshExecutionOptions = () => {
      void fetchChatExecutionOptions(projectId, chatId)
      .then((options) => {
        if (cancelled) return;
        setExecutionOptions(options);
        const usePinnedDefault = options.session_binding !== null;
        const savedHarness = isDevelopment || usePinnedDefault
          ? options.default.harness_id
          : localStorage.getItem("workbench-chat-harness")
            || localStorage.getItem("workbench-chat-executor")
            || options.default.harness_id;
        const harness = options.harnesses.find(
          (candidate) => candidate.harness_id === savedHarness,
        );
        const savedRoute = usePinnedDefault
          ? options.default.inference_route_id
          : localStorage.getItem("workbench-chat-route");
        const route = options.inference_routes.find(
          (candidate) => candidate.route_id === savedRoute
            && candidate.harness_ids.includes(savedHarness),
        ) ?? options.inference_routes.find(
          (candidate) => candidate.harness_ids.includes(savedHarness),
        );
        const savedEffort = localStorage.getItem("workbench-effort");
        const reasoningEffort = savedEffort
          && harness?.reasoning_efforts.includes(savedEffort)
          ? savedEffort
          : options.default.reasoning_effort;
        const selectedModel = usePinnedDefault
          ? options.default.model
          : isDevelopment
            ? localStorage.getItem("workbench-codex-model")
              || options.default.model
            : localStorage.getItem("workbench-chat-model")
              || options.default.model;
        setRunSelectionState({
          harness_id: harness?.harness_id ?? options.default.harness_id,
          inference_route_id: route?.route_id
            ?? options.default.inference_route_id,
          target_id: options.default.target_id,
          model: selectedModel,
          reasoning_effort: reasoningEffort,
        });
      })
      .catch(() => {
        if (!cancelled) {
          setExecutionOptions(null);
          setRunSelectionState(null);
          setExecutionPreflight(null);
        }
      });
    };
    refreshExecutionOptions();
    window.addEventListener(
      "agent-connections-changed",
      refreshExecutionOptions,
    );
    return () => {
      cancelled = true;
      window.removeEventListener(
        "agent-connections-changed",
        refreshExecutionOptions,
      );
    };
  }, [projectId, chatId, isDevelopment]);

  useEffect(() => {
    if (!runSelection) {
      setExecutionPreflight(null);
      return;
    }
    let cancelled = false;
    setExecutionPreflight(null);
    const timer = window.setTimeout(() => {
      void preflightChatExecution(projectId, chatId, runSelection)
        .then((result) => {
          if (!cancelled) setExecutionPreflight(result);
        })
        .catch(() => {
          if (!cancelled) setExecutionPreflight({
            compatible: false,
            issues: [{
              code: "preflight_unavailable",
              scope: "route",
              detail: "Run preflight is unavailable",
            }],
          });
        });
    }, 200);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [projectId, chatId, runSelection]);

  /** One mapping for token usage from live events and persisted
   *  history alike: totals measure spend, the last request's tokens
   *  measure the context. */
  const applyUsagePayload = (u: Record<string, unknown>) => {
    const last = (u.last ?? {}) as Record<string, number>;
    const total = (u.total ?? {}) as Record<string, number>;
    const window = u.modelContextWindow as number | null;
    setUsage({
      input: total.inputTokens ?? 0,
      cached: total.cachedInputTokens ?? 0,
      output: total.outputTokens ?? 0,
      contextPct:
        window && last.totalTokens
          ? Math.min(100, Math.round((100 * last.totalTokens) / window))
          : null,
    });
  };

  /** Fetch history and (re)subscribe to the event stream. `preserve`
   *  keeps the reading position (background-return resync); without it
   *  the view jumps to the bottom (opening/switching chats). */
  const loadChat = useCallback(
    (preserve: boolean) => {
      sourceRef.current?.close();
      // Opening/switching a chat blanks to a "loading…" line while the
      // history renders; a background resync leaves the view in place.
      if (!preserve) {
        setLoading(true);
        setItems([]);
        setApprovals([]);
        setOpenGroups(new Set());
        setExpandedAgent(null);
      }
      // Mobile return-to-foreground fires several resync signals at once;
      // only the newest load may open a stream, or the older one's
      // EventSource leaks and every event renders twice.
      const seq = ++loadSeqRef.current;
      const applyMeta = (history: Awaited<ReturnType<typeof fetchHistory>>) => {
        setModel(history.model);
        setThreadId(history.thread_id);
        setBusy(history.turn_active);
        setApprovals(
          (history.pending_approvals ?? []).map((approval) => ({
            requestId: approval.request_id,
            approvalKind: approval.kind,
            command: approval.command,
            cwd: approval.cwd,
            reason: approval.reason,
            createdAt: approval.created_at,
            expiresAt: approval.expires_at,
          })),
        );
        // idle reload: the persisted counts, not zeros (live events
        // overwrite as soon as a turn runs)
        if (history.usage) applyUsagePayload(history.usage);
      };
      // Only the recent window loads eagerly (the last two turns /
      // 300 entries); older history pages in on scroll-up. On a
      // background resync (preserve) the fresh window is merged onto
      // already-loaded older pages when it connects to them; a hole (a
      // long absence) falls back to replacing the view.
      fetchHistory(projectId, chatId, undefined, 2)
        .then((quick) => {
          if (seq !== loadSeqRef.current) return;
          applyMeta(quick);
          const start = quick.start_index ?? 0;
          const built = buildHistoryItems(quick.messages, start);
          const prev = itemsRef.current;
          const prevHistory = prev.filter((it) => it.k !== undefined);
          const prevMaxK = prevHistory.length
            ? (prevHistory[prevHistory.length - 1].k ?? -1)
            : -1;
          const merge = preserve && prevHistory.length > 0 && start <= prevMaxK + 1;
          let current: Item[];
          if (merge) {
            const kept = prev.filter(
              (it) => it.k !== undefined && (it.k ?? 0) < start,
            );
            current = [...kept, ...built.items];
            setItems(current);
            setAgents((old) => ({ ...old, ...built.agents }));
            for (const key of Object.keys(built.agents))
              completedAgentsRef.current.add(key);
            // historyStartRef / hasMore describe the OLDEST loaded page;
            // a merged resync leaves them untouched
          } else {
            current = built.items;
            setItems(current);
            setAgents(built.agents);
            setAgentOrder([]);
            completedAgentsRef.current = new Set(Object.keys(built.agents));
            historyStartRef.current = start;
            setHasMore(quick.has_more);
          }
          setLoading(false);
          readIndexRef.current = quick.read_index ?? null;
          const enders = turnEnders(current);
          if (
            readIndexRef.current === null &&
            historyStartRef.current === 0 &&
            enders.length === 0
          ) {
            // A chat with no answers yet: start tracking now, so its
            // first answer counts as unread even if it lands while the
            // page is away. (Chats predating the marker keep null — all
            // read — until scrolling establishes it.)
            readIndexRef.current = -1;
            void postReadMarker(projectId, chatId, -1).catch(() => {});
          }
          const marker = readIndexRef.current;
          const unread =
            marker === null ? undefined : enders.find((e) => e.tidx > marker);
          if (unread && (!preserve || atBottomRef.current)) {
            // Land at the top of the first unread turn end — the reader
            // catches up from there — instead of the bottom.
            pendingReadRef.current = unread.tidx;
          } else if (preserve && !atBottomRef.current) {
            // mid-scroll background resync: keep the reading position
            suppressAnchorRef.current = true;
          }
          if (!preserve) anchorKeyRef.current = "";
          lastEventAtRef.current = Date.now();
          // Subscribe from the window's seq cursor: everything after
          // the snapshot streams in, nothing duplicates, and every
          // window (this one included) renders the same broadcast.
          sourceRef.current = openEvents(projectId, chatId, quick.seq, (event) => {
            lastEventAtRef.current = Date.now();
            handleEventRef.current(event);
          });
        })
        .catch(() => {
          if (seq === loadSeqRef.current) setLoading(false);
        });
    },
    [projectId, chatId],
  );

  /** Page one more block of older history in above the loaded window,
   *  keeping the viewport glued to what it was showing. */
  const loadOlder = useCallback(() => {
    if (pagingRef.current || !hasMoreRef.current) return;
    const before = historyStartRef.current;
    if (before <= 0) return;
    pagingRef.current = true;
    setPaging(true);
    const seq = loadSeqRef.current;
    fetchHistory(projectId, chatId, undefined, undefined, before)
      .then((page) => {
        if (seq !== loadSeqRef.current) return;
        const start = page.start_index ?? 0;
        const built = buildHistoryItems(page.messages, start);
        const node = scrollRef.current;
        if (node)
          pendingPrependRef.current = {
            height: node.scrollHeight,
            top: node.scrollTop,
          };
        setItems((prev) => [...built.items, ...prev]);
        setAgents((prev) => ({ ...built.agents, ...prev }));
        for (const key of Object.keys(built.agents))
          completedAgentsRef.current.add(key);
        historyStartRef.current = start;
        setHasMore(page.has_more);
      })
      .catch(() => {})
      .finally(() => {
        if (seq === loadSeqRef.current) {
          pagingRef.current = false;
          setPaging(false);
        }
      });
  }, [projectId, chatId]);

  // A prepended page grows the scroll height above the viewport; keep
  // the reader glued to the content they were looking at. (Native
  // scroll anchoring is disabled on the scroller so the two never
  // double-compensate.)
  useLayoutEffect(() => {
    const pending = pendingPrependRef.current;
    if (!pending) return;
    pendingPrependRef.current = null;
    const node = scrollRef.current;
    if (!node) return;
    const delta = node.scrollHeight - pending.height;
    if (delta > 0) {
      skipScrollRef.current++;
      node.scrollTop = pending.top + delta;
    }
  }, [items]);

  useEffect(() => {
    loadChat(false);
    return () => {
      loadSeqRef.current++; // abort any in-flight load
      sourceRef.current?.close();
    };
  }, [loadChat]);

  // In-flight reviews dock in the chat that requested them, like
  // subagents; other chats (and CLI sessions) follow them through the
  // ledger instead. The server narrows the poll to this session's
  // active rows and pre-summed reviewer token usage — the full review
  // history (reports, step traces) never rides along.
  useEffect(() => {
    let cancelled = false;
    const here = `${projectId}/${chatId}`;
    const tick = () =>
      fetchReviews(projectId, here)
        .then((payload) => {
          if (cancelled) return;
          setReviews(payload.reviews);
          setWriters(payload.writers ?? []);
          const u = payload.usage;
          setReviewUsage(u && (u.input || u.output) ? u : null);
        })
        .catch(() => {});
    tick();
    const timer = setInterval(tick, 3000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [projectId, chatId]);

  // The agent's task list (written out-of-process by the ledger MCP
  // tool); poll it for the task dock.
  useEffect(() => {
    let cancelled = false;
    const tick = () =>
      fetchTasks(projectId, chatId)
        .then((payload) => {
          if (cancelled) return;
          setTasks(payload.tasks);
          setGoal(payload.goal ?? null);
        })
        .catch(() => {});
    tick();
    const timer = setInterval(tick, 2000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [projectId, chatId]);

  // Mobile browsers freeze pages and kill connections in the background;
  // an EventSource can come back CLOSED (no auto-retry) or as a silent
  // zombie. On any return-to-foreground signal, resync when the stream is
  // closed or has not even delivered a keepalive ping recently.
  useEffect(() => {
    const maybeResync = () => {
      if (document.visibilityState === "hidden") return;
      const source = sourceRef.current;
      const stale = Date.now() - lastEventAtRef.current > 45_000;
      if (!source || source.readyState === EventSource.CLOSED || stale) {
        loadChat(true);
      }
    };
    document.addEventListener("visibilitychange", maybeResync);
    window.addEventListener("pageshow", maybeResync);
    window.addEventListener("focus", maybeResync);
    window.addEventListener("online", maybeResync);
    return () => {
      document.removeEventListener("visibilitychange", maybeResync);
      window.removeEventListener("pageshow", maybeResync);
      window.removeEventListener("focus", maybeResync);
      window.removeEventListener("online", maybeResync);
    };
  }, [loadChat]);

  // Consecutive intermediate steps render as one collapsible node; the
  // anchor logic below works in nodes, not raw items.
  const nodes = buildNodes(items);

  // atBottomRef ("is the reader following the live edge?") must change
  // only on genuine user scrolls. Our own programmatic scrolls fire a
  // native scroll event too — and a pin's event can arrive after newer
  // content grew scrollHeight, which would wrongly read as "left the
  // bottom." So a programmatic scroll bumps skipScrollRef and sets the
  // intended state directly; onScroll consumes that mark and leaves
  // atBottomRef alone.
  const skipScrollRef = useRef(0);

  const scrollTo = (node: HTMLElement, top: number, atBottom: boolean) => {
    const target = Math.max(0, Math.min(top, node.scrollHeight - node.clientHeight));
    if (Math.abs(node.scrollTop - target) > 1) {
      skipScrollRef.current++;
      node.scrollTop = target;
    }
    atBottomRef.current = atBottom;
    setShowJump(!atBottom);
  };

  /** The rendered element for items[index] (plain nodes only). */
  const elementForItem = (index: number): HTMLElement | null => {
    const nodeIdx = nodes.findIndex(
      (n) => n.kind === "plain" && n.index === index,
    );
    if (nodeIdx < 0) return null;
    const inner = scrollRef.current?.querySelector(".chat-inner");
    return (inner?.children[nodeIdx] as HTMLElement) ?? null;
  };

  // A turn-ending message counts as read once the view has scrolled
  // below its top — its first line has entered the viewport. Checked on
  // every scroll (programmatic ones included) and content change; each
  // advance moves the server's shared marker forward.
  const markRead = () => {
    const node = scrollRef.current;
    if (!node || loading) return;
    const viewBottom = node.scrollTop + node.clientHeight;
    let best: number | null = null;
    for (const ender of turnEnders(items)) {
      const marker = readIndexRef.current;
      if (marker !== null && ender.tidx <= marker) continue;
      const el = elementForItem(ender.index);
      if (!el) continue;
      const top =
        el.getBoundingClientRect().top -
        node.getBoundingClientRect().top +
        node.scrollTop;
      if (top < viewBottom - 8 && (best === null || ender.tidx > best))
        best = ender.tidx;
    }
    if (best !== null) {
      readIndexRef.current = best;
      void postReadMarker(projectId, chatId, best).catch(() => {});
    }
  };

  const onScroll = () => {
    const node = scrollRef.current;
    if (!node) return;
    markRead();
    // nearing the top of the loaded window: page older history in
    if (node.scrollTop < 600) loadOlder();
    const atBottom = node.scrollHeight - node.scrollTop - node.clientHeight < 48;
    setShowJump(!atBottom);
    if (skipScrollRef.current > 0) {
      skipScrollRef.current--;
      return;
    }
    atBottomRef.current = atBottom;
    // A reader who repositions during the turn (e.g. scrolls down to
    // follow a long typing animation past its parked top) has chosen
    // their spot: the turn's end must not yank them back to the top.
    if (busy) userScrolledWhileBusyRef.current = true;
  };

  const jumpToBottom = () => {
    const node = scrollRef.current;
    if (node) node.scrollTo({ top: node.scrollHeight, behavior: "smooth" });
  };

  // Autoscroll: while the view sits at the bottom, it follows the live
  // edge — every streamed reasoning step, tool line, and character of the
  // message's typing animation keeps the newest content pinned at the
  // bottom. Anywhere above the bottom the view never moves; the ↓ button
  // returns to the live edge. (The final message then snaps to its top
  // when the turn ends — see below.)
  useEffect(() => {
    const node = scrollRef.current;
    if (!node) return;
    const key = liveMessage
      ? `live-m-${liveMessage.id}`
      : liveReasoning
        ? `live-r-${liveReasoning.id}`
        : `item-${items.length - 1}`;
    const previous = anchorKeyRef.current;
    anchorKeyRef.current = key;
    if (pendingReadRef.current !== null) {
      // A load found an unread turn-ending message: land the viewport at
      // its top (which marks it read — the next unread is the next stop).
      const tidx = pendingReadRef.current;
      pendingReadRef.current = null;
      const el = elementForItem(
        items.findIndex((it) => it.kind === "assistant" && it.tidx === tidx),
      );
      if (el) {
        const top =
          el.getBoundingClientRect().top -
          node.getBoundingClientRect().top +
          node.scrollTop -
          12;
        scrollTo(node, top, false);
        return;
      }
    }
    if (suppressAnchorRef.current) {
      // background resync: keep the reading position untouched
      if (key !== previous) suppressAnchorRef.current = false;
      return;
    }
    if (previous !== "" && !atBottomRef.current) return;
    // a reader inside a viewport-filling open box is not "at the
    // bottom" in any sense that justifies moving them
    if (previous !== "" && openBoxFillsView(node)) return;
    // The typing animation follows the bottom only until the streaming
    // message's top reaches the top of the view; a longer message then
    // parks there and streams below the fold, so the reader starts at
    // the beginning and the turn's end brings no jump back up.
    if (liveMessage) {
      const el = node.querySelector(".msg-assistant.live");
      if (el) {
        const cap =
          el.getBoundingClientRect().top -
          node.getBoundingClientRect().top +
          node.scrollTop -
          12;
        if (cap < node.scrollHeight - node.clientHeight) {
          scrollTo(node, cap, false);
          return;
        }
      }
    }
    // fresh load and every following render alike: stay at the bottom
    scrollTo(node, node.scrollHeight, true);
  }, [items, liveMessage, liveReasoning, nodes.length]);

  // Content can change with no scroll event at all (a short chat that
  // fits the viewport, a turn end appending its final message while
  // pinned); sweep for newly readable turn ends after the anchor effect
  // above has settled the position.
  useEffect(() => {
    markRead();
  }, [items, loading, busy]);

  // The docks (tasks, subagents/reviews) live below the scroll area, so
  // when one appears or grows the scroll viewport shrinks — its clientHeight
  // drops while scrollTop stays, dropping the newest content below the fold.
  // Follow the shrink: while pinned to the bottom, stay pinned. (Content
  // growth changes scrollHeight, not the element's box, so this observer
  // fires only on viewport resizes — dock appearing, textarea growing, the
  // window resizing — never on new transcript content.)
  useEffect(() => {
    const node = scrollRef.current;
    if (!node) return;
    const observer = new ResizeObserver(() => {
      if (!atBottomRef.current || openBoxFillsView(node)) return;
      const target = node.scrollHeight - node.clientHeight;
      if (Math.abs(node.scrollTop - target) > 1) {
        skipScrollRef.current++;
        node.scrollTop = target;
      }
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  // When the turn ends on an assistant message, snap that final message's
  // top into view so the reader starts at the beginning of the answer —
  // but only if they were following the stream (still at the bottom).
  // A turn end the server flagged will_continue (reviewers still out,
  // an unresolved goal about to rewake the chat) is exempt: its message
  // is an interim note, not the answer, so the view stays pinned like
  // for any mid-turn message.
  const wasBusyRef = useRef(false);
  const willContinueRef = useRef(false);
  const userScrolledWhileBusyRef = useRef(false);
  useLayoutEffect(() => {
    const justEnded = wasBusyRef.current && !busy;
    wasBusyRef.current = busy;
    const userMoved = userScrolledWhileBusyRef.current;
    if (!busy) userScrolledWhileBusyRef.current = false;
    if (!justEnded || !atBottomRef.current || userMoved) return;
    if (scrollRef.current && openBoxFillsView(scrollRef.current)) return;
    if (willContinueRef.current) return;
    const node = scrollRef.current;
    const lastItem = items[items.length - 1];
    if (!node || lastItem?.kind !== "assistant") return;
    const el = node.querySelector(".chat-inner")?.children[nodes.length - 1];
    if (!el) return;
    const top =
      el.getBoundingClientRect().top -
      node.getBoundingClientRect().top +
      node.scrollTop -
      12;
    // leaving the bottom for the top: the reader is no longer following
    scrollTo(node, top, false);
  }, [busy]);

  const handleEvent = (event: Record<string, unknown>) => {
    const type = event.type as string;
    if (type === "thread.started" && event.thread_id) {
      setThreadId(event.thread_id as string);
    } else if (type === "user_message") {
      setItems((prev) => [
        ...prev,
        {
          kind: "user",
          text: (event.text as string) ?? "",
          steered: event.steered === true,
          at: event.at as string | undefined,
        },
      ]);
      setBusy(true);
    } else if (type === "turn.started") {
      if (!willContinueRef.current) authoredWorkflowsRef.current.clear();
      willContinueRef.current = false;
      setBusy(true);
      onChatStatusChange("turn.started");
    } else if (type === "turn.completed") {
      // The server says outright whether this end is real; a
      // will_continue end is semantically mid-turn and must not snap
      // the view to the message's top.
      willContinueRef.current = event.will_continue === true;
      setBusy(false);
      setLiveMessage(null);
      setLiveReasoning(null);
      onLedgerActivity();
      onChatStatusChange("turn.completed");
      if (!willContinueRef.current) {
        onTurnCompleted(
          [...authoredWorkflowsRef.current].map(([workflow_id, revision_id]) => ({
            workflow_id,
            revision_id,
          })),
        );
        authoredWorkflowsRef.current.clear();
      }
    } else if (type === "delta") {
      const id = (event.id as string) ?? "";
      const text = (event.text as string) ?? "";
      const update = (prev: LiveText | null): LiveText =>
        prev && prev.id === id ? { id, text: prev.text + text } : { id, text };
      if (event.channel === "message") setLiveMessage(update);
      else if (event.channel === "reasoning") setLiveReasoning(update);
    } else if (type === "token_usage") {
      applyUsagePayload((event.usage ?? {}) as Record<string, unknown>);
    } else if (type === "approval_request") {
      const requestId = (event.request_id as string) ?? "";
      const approval: ApprovalItem = {
        requestId,
        approvalKind: (event.kind as string) ?? "command",
        command: (event.command as string) ?? null,
        cwd: (event.cwd as string) ?? null,
        reason: (event.reason as string) ?? null,
        createdAt: event.created_at as number | undefined,
        expiresAt: event.expires_at as number | undefined,
      };
      setApprovals((prev) => [
        ...prev.filter((item) => item.requestId !== requestId),
        approval,
      ]);
      onChatStatusChange("activity");
    } else if (type === "approval_resolved") {
      const requestId = event.request_id as string;
      const decision = event.decision as string;
      setApprovals((prev) =>
        prev.map((item) =>
          item.requestId === requestId
            ? { ...item, decision, submitting: undefined, error: undefined }
          : item,
        ),
      );
      window.setTimeout(
        () =>
          setApprovals((current) =>
            current.filter((item) => item.requestId !== requestId),
          ),
        1600,
      );
      onChatStatusChange("activity");
    } else if (type === "approval_failed") {
      const requestId = event.request_id as string;
      setApprovals((prev) =>
        prev.map((item) =>
          item.requestId === requestId
            ? {
                ...item,
                submitting: undefined,
                error:
                  (event.message as string) ??
                  "Codex did not accept the approval decision.",
              }
            : item,
        ),
      );
      onChatStatusChange("activity");
    } else if (type === "subagent") {
      const key = event.thread_id as string;
      const kind = event.kind as string;
      setAgents((prev) => {
        const agent = prev[key] ?? {
          name: (event.name as string) ?? "subagent",
          status: "running" as const,
          lines: [],
          text: null,
        };
        if (kind === "tool")
          return {
            ...prev,
            [key]: { ...agent, lines: [...agent.lines, agentLineFromEvent(event)] },
          };
        if (kind === "message")
          return { ...prev, [key]: { ...agent, text: event.text as string } };
        if (kind === "complete")
          return { ...prev, [key]: { ...agent, status: "done" } };
        return { ...prev, [key]: agent };
      });
      if (kind === "started" && !completedAgentsRef.current.has(key)) {
        setAgentOrder((prev) => (prev.includes(key) ? prev : [...prev, key]));
      } else if (kind === "complete" && !completedAgentsRef.current.has(key)) {
        // Leave the dock; the box re-appears inline at the completion point.
        completedAgentsRef.current.add(key);
        setAgentOrder((prev) => prev.filter((id) => id !== key));
        setItems((prev) => [...prev, { kind: "subagent", threadId: key }]);
      }
    } else if (type === "item.completed") {
      const item = (event.item ?? {}) as Record<string, unknown>;
      if (
        item.type === "mcp_tool_call"
        && item.server === "project"
        && (item.tool === "workflow_create" || item.tool === "workflow_update")
      ) {
        const payload = parseToolResult(item);
        const workflow = payload?.workflow;
        if (workflow && typeof workflow === "object" && !Array.isArray(workflow)) {
          const workflowId = (workflow as Record<string, unknown>).workflow_id;
          const revisionId = (workflow as Record<string, unknown>).revision_id;
          if (typeof workflowId === "string" && typeof revisionId === "string") {
            authoredWorkflowsRef.current.set(workflowId, revisionId);
          }
        }
      }
      if (item.type === "agent_message") {
        setLiveMessage(null);
        setItems((prev) => [
          ...prev,
          {
            kind: "assistant",
            text: (item.text as string) ?? "",
            at: event.at as string | undefined,
            messageId: item.message_id as string | undefined,
            tidx: event.tidx as number | undefined,
            executorId: item.executor_id as string | undefined,
            model: item.model as string | undefined,
            operationId: item.operation_id as string | undefined,
          },
        ]);
      } else if (item.type === "reasoning" && item.text) {
        setLiveReasoning(null);
        setItems((prev) => [
          ...prev,
          { kind: "reasoning", text: item.text as string },
        ]);
      } else {
        const activity = activityFromEvent(event);
        if (activity) {
          setItems((prev) => [...prev, activity]);
          if (item.type === "mcp_tool_call") onLedgerActivity();
        }
      }
    } else if (type === "text_repaired") {
      // one swap event covers assistant messages and review reports alike
      const id = event.id as string;
      const patch = {
        text: event.repaired as string,
        latexOriginal: event.original as string,
      };
      setItems((prev) =>
        prev.map((it) =>
          (it.kind === "assistant" && it.messageId === id) ||
          (it.kind === "review" && it.reviewId === id) ||
          (it.kind === "writer" && it.writerKey === id)
            ? { ...it, ...patch }
            : it,
        ),
      );
    } else if (type === "review") {
      const subject = (event.subject ?? {}) as {
        claim_id?: string;
        proof_id?: string;
        name?: string;
      };
      setItems((prev) => [
        ...prev,
        {
          kind: "review",
          reviewId: (event.review_id as string) ?? "",
          status: (event.status as string) ?? "completed",
          verdict: (event.verdict as string | null) ?? null,
          subjectId: subject.proof_id ?? subject.claim_id ?? "",
          documentName: subject.name,
          text: (event.text as string) ?? "",
        },
      ]);
      // its dock row is gone the moment the outcome lands
      setReviews((prev) =>
        prev.filter((r) => r.review_id !== (event.review_id as string)),
      );
    } else if (type === "writer") {
      setItems((prev) => [
        ...prev,
        {
          kind: "writer",
          writerId: (event.writer_id as string) ?? "",
          writerKey: (event.writer_key as string) ?? "",
          filename: (event.filename as string) ?? "",
          status: (event.status as string) ?? "paused",
          activation: (event.activation as number) ?? 1,
          text: (event.text as string) ?? "",
          at: event.at as string | undefined,
        },
      ]);
    } else if (type === "error") {
      setItems((prev) => [
        ...prev,
        {
          kind: "assistant",
          text: `**error:** ${event.message as string}`,
          at: event.at as string | undefined,
          error: true,
        },
      ]);
    }
  };

  handleEventRef.current = handleEvent;

  // No optimistic append: the message comes back on the event stream
  // (as it does for every other window), so all windows render alike.
  // If the stream died while the page was backgrounded, resync so the
  // just-sent message (and everything missed) appears.
  const send = async (message: string) => {
    // Sending is an explicit request to return to the live edge, even if
    // the reader had scrolled upward in the previous turn.
    const scroller = scrollRef.current;
    if (scroller) scrollTo(scroller, scroller.scrollHeight, true);
    if (!runSelection) {
      setItems((prev) => [
        ...prev,
        {
          kind: "assistant",
          text: "**error:** message not delivered — run configuration is still loading",
          error: true,
        },
      ]);
      return;
    }
    try {
      await sendMessage(
        projectId,
        chatId,
        message,
        runSelection,
      );
      const source = sourceRef.current;
      if (!source || source.readyState === EventSource.CLOSED) loadChat(true);
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      setItems((prev) => [
        ...prev,
        {
          kind: "assistant",
          text: `**error:** message not delivered — ${detail}`,
          error: true,
        },
      ]);
    }
  };

  const stop = () => {
    void interruptChat(projectId, chatId);
  };

  const decide = async (requestId: string, decision: string) => {
    setApprovals((prev) =>
      prev.map((item) =>
        item.requestId === requestId
          ? { ...item, submitting: decision, error: undefined }
          : item,
      ),
    );
    try {
      await answerApproval(projectId, chatId, requestId, decision);
      setApprovals((prev) =>
        prev.map((item) =>
          item.requestId === requestId
            ? { ...item, decision, submitting: undefined, error: undefined }
            : item,
        ),
      );
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      setApprovals((prev) =>
        prev.map((item) =>
          item.requestId === requestId
            ? { ...item, submitting: undefined, error: detail }
            : item,
        ),
      );
    }
  };

  const toggleGroup = (start: number) =>
    setOpenGroups((prev) => {
      const next = new Set(prev);
      if (next.has(start)) next.delete(start);
      else next.add(start);
      return next;
    });

  const describeStep = (item: Item): string => {
    if (item.kind === "reasoning")
      // The last line: where the step ended up, not where it started.
      return lastReasoningLine(item.text) || "thinking";
    if (item.kind === "activity") return item.detail;
    return "";
  };

  const finalAssistantByOperation = new Map<string, Item>();
  items.forEach((item) => {
    if (item.kind === "assistant" && item.operationId && !item.error) {
      finalAssistantByOperation.set(item.operationId, item);
    }
  });

  const renderItem = (item: Item, i: number) =>
    item.kind === "user" ? (
      <UserMessage key={item.k ?? i} item={item} />
    ) : item.kind === "assistant" ? (
      <AssistantMessage
        key={item.k ?? i}
        item={item}
        handoff={
          item.operationId
          && finalAssistantByOperation.get(item.operationId) === item
          && !busy
            ? {
                projectId,
                chatId,
                workspaceId: chat.workspace?.workspace_id,
                onCreated: onHandoffCreated,
              }
            : undefined
        }
      />
    ) : item.kind === "reasoning" ? (
      <ReasoningStep key={item.k ?? i} text={item.text} />
    ) : item.kind === "subagent" ? (
      agents[item.threadId] ? (
        <AgentBox
          key={item.k ?? i}
          agent={agents[item.threadId]}
          open={expandedAgent === item.threadId}
          onToggle={() =>
            setExpandedAgent((current) =>
              current === item.threadId ? null : item.threadId,
            )
          }
          inline
        />
      ) : null
    ) : item.kind === "review" ? (
      <ReviewBox
        key={item.k ?? i}
        item={item}
        open={expandedAgent === item.reviewId}
        onToggle={() =>
          setExpandedAgent((current) =>
            current === item.reviewId ? null : item.reviewId,
          )
        }
      />
    ) : item.kind === "writer" ? (
      <WriterBox
        key={item.k ?? i}
        item={item}
        open={expandedAgent === item.writerKey}
        onToggle={() =>
          setExpandedAgent((current) =>
            current === item.writerKey ? null : item.writerKey,
          )
        }
      />
    ) : item.kind === "approval" ? (
      <ApprovalCard key={item.k ?? i} item={item} onDecide={decide} />
    ) : (
      <div key={item.k ?? i} className="activity">
        <ActivityLine
          label={item.label}
          detail={item.detail}
          tex={item.tex}
          body={item.body}
          error={item.error}
        />
      </div>
    );

  const lastNode = nodes[nodes.length - 1];
  const activeGroup =
    busy && !liveMessage && lastNode?.kind === "group" ? lastNode : null;
  const turnExecutor = runSelection?.harness_id
    ?? executionOptions?.default.harness_id
    ?? (isDevelopment ? "codex" : "reasoning-native");
  const turnModel = runSelection?.model
    ?? executionOptions?.default.model
    ?? model;
  const [codexPrice, setCodexPrice] = useState<ModelPriceInfo | null>(null);

  useEffect(() => {
    if (turnExecutor !== "codex" || !turnModel) {
      setCodexPrice(null);
      return;
    }
    let cancelled = false;
    void fetchModelPrices("all")
      .then((catalog) => {
        if (cancelled) return;
        setCodexPrice(
          catalog.models.find(
            (candidate) =>
              candidate.model_id === turnModel ||
              candidate.canonical_slug === turnModel,
          ) ?? null,
        );
      })
      .catch(() => {
        if (!cancelled) setCodexPrice(null);
      });
    return () => {
      cancelled = true;
    };
  }, [turnExecutor, turnModel]);

  const displayedUsage =
    usage || reviewUsage
      ? {
          input: (usage?.input ?? 0) + (reviewUsage?.input ?? 0),
          cached: (usage?.cached ?? 0) + (reviewUsage?.cached ?? 0),
          output: (usage?.output ?? 0) + (reviewUsage?.output ?? 0),
          contextPct: usage?.contextPct ?? null,
        }
      : null;
  const estimatedCostUsd =
    turnExecutor === "codex" && displayedUsage
      ? estimateOpenRouterTokenCost(displayedUsage, codexPrice)
      : null;

  // Nothing has been said yet: the greeting and the composer sit together in
  // the middle of the column, and the composer slides down to its docked
  // position when the first message lands (see .chat-col-tail).
  const transcriptEmpty = !loading && items.length === 0 && !busy;

  return (
    <div className={`chat-col ${transcriptEmpty ? "chat-col-empty" : ""}`}>
      <div
        className={`chat-scroll ${transcriptEmpty ? "chat-scroll-bottom" : ""}`}
        ref={scrollRef}
        onScroll={onScroll}
      >
        <div className="chat-inner">
          {loading ? (
            <div className="empty">loading…</div>
          ) : (
            transcriptEmpty && (
              <div className="chat-greeting">
                {isDevelopment ? (
                  <>
                    <strong>What are we working on?</strong>
                    <p>
                      {prettyExecutor(turnExecutor)} · <span className="mono">{turnModel}</span> may
                      read and edit{" "}
                      <span className="mono">{chat.workspace?.name ?? "repository"}</span> from this
                      chat. Commits and pushes remain explicit requests.
                    </p>
                    <p className="development-workspace-path mono">{chat.workspace?.path}</p>
                  </>
                ) : (
                  <>
                    <strong>What are we working on?</strong>
                    <p>
                      This session runs on {prettyExecutor(turnExecutor)} ·{" "}
                      <span className="mono">{turnModel}</span>. The run summary in the composer
                      changes the route, target, or reasoning policy.
                    </p>
                  </>
                )}
              </div>
            )
          )}
          {hasMore && !loading && (
            <button type="button" className="load-earlier" onClick={loadOlder}>
              {paging ? "loading earlier…" : "load earlier"}
            </button>
          )}
          {nodes.map((node) =>
            node.kind === "plain" ? (
              renderItem(node.item, node.index)
            ) : (
              <ActivityGroup
                key={`g-${groupKey(node)}`}
                entries={node.entries}
                active={node === activeGroup}
                liveTail={
                  node === activeGroup && liveReasoning?.text
                    ? liveReasoning.text
                    : null
                }
                open={openGroups.has(groupKey(node))}
                onToggle={() => toggleGroup(groupKey(node))}
                describe={describeStep}
                renderStep={(entry) => renderItem(entry.item, entry.index)}
              />
            ),
          )}
          {/* The turn's first step, still streaming with nothing landed
              yet: the same "1 step" group box it will become, keyed at
              the index the step will land on so its toggle carries over. */}
          {!activeGroup && liveReasoning && liveReasoning.text && (
            <ActivityGroup
              key={`g-${items.length}`}
              entries={[]}
              active
              liveTail={liveReasoning.text}
              open={openGroups.has(items.length)}
              onToggle={() => toggleGroup(items.length)}
              describe={describeStep}
              renderStep={() => null}
            />
          )}
          {liveMessage && liveMessage.text && (
            <div className="msg-assistant live">
              <Markdown text={liveMessage.text} />
            </div>
          )}
          {busy && !liveMessage && !activeGroup && !liveReasoning && (
            <div
              className="typing-indicator"
              role="status"
              aria-label="working…"
            >
              <i aria-hidden="true" />
              <i aria-hidden="true" />
              <i aria-hidden="true" />
            </div>
          )}
        </div>
      </div>
      <div className="chat-input-row">
        {showJump && (
          <button
            type="button"
            className="jump-bottom"
            aria-label="Jump to the latest message"
            title="jump to the latest message"
            onClick={jumpToBottom}
          >
            ↓
          </button>
        )}
        {approvals.length > 0 && (
          <section
            className="approval-dock"
            aria-label="Codex approval requests"
            aria-live="assertive"
          >
            <div className="approval-dock-title">Action needed</div>
            {approvals.map((approval) => (
              <ApprovalCard
                key={approval.requestId}
                item={approval}
                onDecide={(requestId, decision) =>
                  void decide(requestId, decision)
                }
              />
            ))}
          </section>
        )}
        {/* Subagents on top, task list below, sharing one vertical
            budget: the task dock takes priority, so the subagent dock
            gives up rows (down to a single "N running" box) as an open
            task list fills the space. */}
        <div className="dock-stack">
          <SubagentDock
            projectId={projectId}
            agents={agents}
            order={agentOrder}
            reviews={reviews}
            writers={writers}
            expanded={expandedAgent}
            onToggle={(id) =>
              setExpandedAgent((current) => (current === id ? null : id))
            }
            cap={
              tasks.length === 0
                ? 4
                : tasksCollapsed
                  ? 3
                  : tasks.length >= 5
                    ? 1
                    : 2
            }
          />
          <TaskDock
            tasks={tasks}
            goal={goal}
            collapsed={tasksCollapsed}
            onToggle={toggleTasksCollapsed}
          />
        </div>
        {executionOptions && runSelection ? (
          <ChatInput
            draftKey={`workbench-chat-draft:${projectId}/${chatId}`}
            busy={busy}
            reviewsRunning={reviews.length > 0}
            executionOptions={executionOptions}
            selection={runSelection}
            preflight={executionPreflight}
            usage={displayedUsage}
            estimatedCostUsd={estimatedCostUsd}
            projectId={projectId}
            chatId={chatId}
            onSelection={setRunSelection}
            onSend={(message) => void send(message)}
            onStop={stop}
            onOpenConnections={onOpenConnections}
          />
        ) : (
          <div className="chat-input-box">
            <div className="empty">loading run configuration…</div>
          </div>
        )}
      </div>
      {/* Grows to push the composer up beside the greeting while the
          transcript is empty, and relaxes back to nothing — carrying the
          composer down with it — once the first message lands. */}
      <div className="chat-col-tail" aria-hidden="true" />
      {threadId && (
        <div style={{ display: "none" }} data-thread-id={threadId} />
      )}
    </div>
  );
}
