// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { EffortPicker } from "./ModelBrowser";
import { ApiError, BotRun, OrganizationPrices, Presence, aborted, errorMessage, organizationPath, request, sharedRequest } from "./organization-api";
import "./bot-direct.css";
import { CardStop } from "./TopicActivity";

/** Your own bot as presence reports it, with its effort levels per model and whether it may run local tools. */
export type OwnBot = Presence["bots"][number];
/** A bot run in your direct conversation with your bot. */
export type DirectBotRun = BotRun & { effort?: string | null; direct_conversation_id?: string | null };
type Approval = { id: string; run_id: string; bot_id: string; command_json: string; command_sha256: string; created_at: number };
const ACTIVE = new Set(["queued", "running", "waiting_approval"]);
const engineLabel = (engine?: string | null) => engine === "claude" ? "Claude Code" : engine === "codex" ? "Codex" : "Bot";

/** Your bot from presence, read at most every few seconds however many views ask. */
export function useOwnBot(organizationId: string, botId: string | null, onAuthLost: () => void): OwnBot | null {
  const [bot, setBot] = useState<OwnBot | null>(null);
  useEffect(() => {
    if (!botId) return;
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const presence = await sharedRequest<Presence>(organizationPath(organizationId) + "/presence", { maxAge: 2500, signal: controller.signal });
        if (!controller.signal.aborted) setBot(presence.bots.find(item => item.id === botId) ?? null);
      } catch (cause) { if (!aborted(cause) && cause instanceof ApiError && cause.status === 401) onAuthLost(); }
      if (!controller.signal.aborted) timer = setTimeout(poll, 5000);
    };
    void poll(); return () => { controller.abort(); clearTimeout(timer); };
  }, [organizationId, botId, onAuthLost]);
  return bot;
}

/** The runs and pending approvals of your conversation with your bot; both refresh quickly while the bot works. */
export function useBotRuns(organizationId: string, root: string, enabled: boolean, wake: unknown, onAuthLost: () => void) {
  const [runs, setRuns] = useState<DirectBotRun[]>([]), [approvals, setApprovals] = useState<Approval[]>([]);
  const [error, setError] = useState(""), [refresh, setRefresh] = useState(0);
  const active = runs.some(run => ACTIVE.has(run.status)), waiting = runs.some(run => run.status === "waiting_approval");
  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      let busy = false;
      try {
        const rows = await request<DirectBotRun[]>(root + "/bot-runs", { signal: controller.signal });
        busy = rows.some(run => ACTIVE.has(run.status));
        let pending: Approval[] = [];
        if (rows.some(run => run.status === "waiting_approval")) {
          const ids = new Set(rows.map(run => run.id));
          pending = (await request<Approval[]>(organizationPath(organizationId) + "/my-bots/execution-approvals", { signal: controller.signal }))
            .filter(approval => ids.has(approval.run_id));
        }
        if (!controller.signal.aborted) { setRuns(rows); setApprovals(pending); setError(""); }
      } catch (cause) {
        if (aborted(cause)) return;
        if (cause instanceof ApiError && cause.status === 401) { onAuthLost(); return; }
        setError(errorMessage(cause));
      }
      if (!controller.signal.aborted) timer = setTimeout(poll, busy ? 1500 : 8000);
    };
    void poll(); return () => { controller.abort(); clearTimeout(timer); };
  }, [organizationId, root, enabled, wake, refresh, onAuthLost]);
  const reload = useCallback(() => setRefresh(value => value + 1), []);
  const stop = useCallback(async (run: DirectBotRun) => {
    try { await request(organizationPath(organizationId) + "/bot-runs/" + run.id + "/stop", { method: "POST", body: {} }); }
    catch (cause) { if (cause instanceof ApiError && cause.status === 401) onAuthLost(); else setError(errorMessage(cause)); }
    reload();
  }, [organizationId, onAuthLost, reload]);
  const decide = useCallback(async (approval: Approval, allow: boolean) => {
    try {
      await request(organizationPath(organizationId) + "/my-bots/execution-approvals/" + approval.id, { method: "POST",
        body: { allow, request_sha256: approval.command_sha256 } });
    } catch (cause) { if (cause instanceof ApiError && cause.status === 401) onAuthLost(); else setError(errorMessage(cause)); }
    reload();
  }, [organizationId, onAuthLost, reload]);
  return { runs, approvals, active, waiting, error, stop, decide, reload };
}

/** The run that wrote a bot reply: the runtime posts each reply with the client ID "bot-run-<run>-<part>". */
export function replyRun(runs: DirectBotRun[], clientId?: string): DirectBotRun | undefined {
  const match = clientId?.match(/^bot-run-([a-f0-9]{32})-\d+$/);
  return match ? runs.find(run => run.id === match[1]) : undefined;
}

/** A finished reply's steps, folded into one line you can open. */
export function BotSteps({ run }: { run: DirectBotRun }) {
  const steps = run.activity ?? [];
  const seconds = run.timing?.claim_to_commit_ms != null ? Math.round(run.timing.claim_to_commit_ms / 1000) : null;
  const meta = [run.model, run.effort ? run.effort[0].toUpperCase() + run.effort.slice(1) : null, seconds != null ? `${seconds} s` : null]
    .filter(Boolean).join(" · ");
  return <div className="org-bot-steps">
    {steps.length > 0 && <details><summary>{steps.length === 1 ? "1 step" : `${steps.length} steps`}</summary>
      <ol>{steps.map((step, index) => <li key={index}>{step.label}</li>)}</ol></details>}
    {meta && <small>{meta}</small>}
  </div>;
}

/** While your bot works: one live line under your message, with any approval it needs. */
export function BotWorking({ run, name, online, approvals, onDecide, onStop }: {
  run: DirectBotRun; name: string; online: boolean; approvals: Approval[];
  onDecide: (approval: Approval, allow: boolean) => void;
  /** Stops this reply, from the card's header. */
  onStop?: () => void;
}) {
  const latest = run.activity?.at(-1)?.label;
  const line = run.status === "waiting_approval" ? "Waiting for your approval"
    : run.status === "queued" ? (online ? "Starting…" : `Waiting for ${name} to come online`) : latest ? `${latest}…` : "Thinking…";
  return <div className="org-message-group org-speaker-bot org-bot-working" aria-live="polite">
    <article className="org-message">
      <header><strong>{name}</strong>{onStop && <CardStop label="Stop this bot reply" onStop={onStop} />}</header>
      <p className="org-bot-status"><i aria-hidden="true" />{line}</p>
      {approvals.filter(approval => approval.run_id === run.id).map(approval => <div key={approval.id} className="org-bot-approval" role="group"
        aria-label={`${name} asks to run a command`}>
        <p>{name} wants to run, once, in this conversation's workspace on your computer:</p>
        <pre><code>{(JSON.parse(approval.command_json) as string[]).map(part => JSON.stringify(part)).join(" ")}</code></pre>
        <span><button type="button" className="org-primary" onClick={() => onDecide(approval, true)}>Approve</button>
          <button type="button" onClick={() => onDecide(approval, false)}>Decline</button></span>
      </div>)}
    </article>
  </div>;
}

/** The ⓘ beside the bot's model: its engine, runner, models and local tools. */
export function BotDetails({ bot }: { bot: OwnBot | null }) {
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
    <button type="button" className="org-composer-info" aria-label="Bot details" title="Bot details" aria-expanded={open}
      onClick={() => setOpen(value => !value)}>i</button>
    {open && bot && <div className="org-capability-details" role="group" aria-label="Bot details">
      <strong>{bot.name}</strong><p>Your bot · {engineLabel(bot.engine)}</p>
      <dl><dt>Runner</dt><dd className={bot.online ? "org-capability-on" : undefined}>{bot.online ? "online" : "offline"}</dd>
        <dt>Local tools</dt><dd>{bot.local_tools ? "allowed" : "off"}</dd>
        <dt>Models</dt><dd>{bot.models?.join(", ") || "none yet"}</dd></dl>
    </div>}
  </span>;
}

/** The bot's model under the message box: a plain name that opens its advertised models. */
export function BotModelPicker({ models, value, disabled, onChange }: {
  models: string[]; value: string; disabled: boolean; onChange: (model: string) => void;
}) {
  const [open, setOpen] = useState(false), box = useRef<HTMLDivElement>(null), button = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (event: PointerEvent) => { if (!box.current?.contains(event.target as Node)) setOpen(false); };
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") { setOpen(false); button.current?.focus(); } };
    document.addEventListener("pointerdown", close); document.addEventListener("keydown", escape);
    return () => { document.removeEventListener("pointerdown", close); document.removeEventListener("keydown", escape); };
  }, [open]);
  return <div className="org-effort-picker" ref={box}>
    <button type="button" ref={button} className="org-composer-pick org-bot-model" aria-label={`Bot model: ${value || "none"}`} title="Bot model"
      aria-haspopup="true" aria-expanded={open} disabled={disabled || !models.length} onClick={() => setOpen(current => !current)}>{value || "No models"}</button>
    {open && <div className="org-model-options org-effort-popover org-bot-models" role="radiogroup" aria-label="Bot models">
      {models.map(model => <label key={model}><input type="radio" name="bot-model" checked={model === value} onChange={() => { onChange(model); setOpen(false); button.current?.focus(); }} />
        <span>{model}</span></label>)}
    </div>}
  </div>;
}

/** The chosen model and effort for your bot, remembered per bot in this browser. */
export function useBotChoice(userId: string, bot: OwnBot | null) {
  const key = bot ? `reasoning-bot-choice:${userId}:${bot.id}` : "";
  const [choice, setChoice] = useState<{ model: string; effort: string }>({ model: "", effort: "" });
  useEffect(() => {
    if (!key) return;
    try { setChoice(JSON.parse(localStorage.getItem(key) ?? "null") ?? { model: "", effort: "" }); } catch { setChoice({ model: "", effort: "" }); }
  }, [key]);
  const models = bot?.models ?? [];
  const model = models.includes(choice.model) ? choice.model : models[0] ?? "";
  const efforts = bot?.efforts?.[model] ?? [];
  const effort = efforts.includes(choice.effort) ? choice.effort : "";
  const update = (next: { model: string; effort: string }) => {
    setChoice(next);
    try { if (key) localStorage.setItem(key, JSON.stringify(next)); } catch { /* Storage is optional. */ }
  };
  // EffortPicker reads a model's levels from a catalog; this one holds only the bot's models.
  const catalog = useMemo(() => ({ models: models.map(id => ({ id, name: id, reasoning_efforts: bot?.efforts?.[id] ?? [] })) }) as unknown as OrganizationPrices,
    [models.join(","), JSON.stringify(bot?.efforts ?? {})]);
  return { model, effort, catalog, setModel: (value: string) => update({ model: value, effort: "" }), setEffort: (value: string) => update({ model, effort: value }) };
}

/** Under the box in a conversation with your bot, as in an AI chat: + on the left; ⓘ, the model and the effort on the right. */
export function BotComposerControls({ attach, bot, choice, disabled }: {
  attach: React.ReactNode; bot: OwnBot | null; choice: ReturnType<typeof useBotChoice>; disabled: boolean;
}) {
  return <div className="org-composer-controls">
    {attach}
    <div className="org-composer-model">
      <span className="org-composer-capabilities"><BotDetails bot={bot} /></span>
      <BotModelPicker models={bot?.models ?? []} value={choice.model} disabled={disabled} onChange={choice.setModel} />
      <EffortPicker model={choice.model} catalog={choice.catalog} effort={choice.effort} disabled={disabled} onSelect={choice.setEffort} />
    </div>
  </div>;
}
