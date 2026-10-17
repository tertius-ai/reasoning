// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import { BotRun, ModelRun, topicPath } from "./organization-api";
import { recordTiming } from "./polling";

/**
 * One authenticated server event stream per open topic, shared by the views that show it. While the stream is
 * live, views poll only every LIVE_SAFETY_POLL_MS as a safety net; when it is unavailable they poll as before.
 */
export type TopicStreamStatus = "connecting" | "live" | "unavailable";
export type MessageWatermark = { sequence: number; updates: number };
export type AgentThread = { root_run: string; status: string; reason?: string | null; requested_by: string; tasks: number };
export type TopicGoal = { id: string; status: string; owned_by: string } | null;
export type TopicEventListener = {
  status?: (status: TopicStreamStatus) => void;
  messages?: (watermark: MessageWatermark) => void;
  runs?: (runs: ModelRun[]) => void;
  agents?: (threads: AgentThread[]) => void;
  goal?: (goal: TopicGoal) => void;
  bots?: (runs: BotRun[]) => void;
};

export const LIVE_SAFETY_POLL_MS = 30_000;
const READY_TIMEOUT_MS = 10_000;
/** Close a hidden tab's stream after this long; polling pauses in hidden tabs anyway. */
const HIDDEN_CLOSE_MS = 60_000;
/** Keep a stream briefly after its last view unmounts, so re-rendered views reuse it. */
const IDLE_CLOSE_MS = 2_000;
const MAX_RETRY_MS = 60_000;

type RunPatch = Omit<ModelRun, "output_text"> & { output_text?: string; output_append?: string };
type Latest = { messages?: MessageWatermark; runs?: ModelRun[]; agents?: AgentThread[]; goal?: TopicGoal; bots?: BotRun[] };

/** Apply a `runs` event to the runs received on this connection; unchanged runs keep their identity. */
export function applyRunPatch(known: Map<string, ModelRun>, patch: { order: string[]; runs: RunPatch[] }): ModelRun[] {
  if (!Array.isArray(patch?.order) || !Array.isArray(patch.runs)) throw new Error("invalid runs event");
  for (const item of patch.runs) {
    const { output_append, ...rest } = item;
    if (output_append !== undefined) {
      const base = known.get(item.id);
      if (!base) throw new Error("output appended to an unknown run");
      known.set(item.id, { ...rest, output_text: base.output_text + output_append } as ModelRun);
    } else known.set(item.id, rest as ModelRun);
  }
  const order = new Set(patch.order);
  for (const id of [...known.keys()]) if (!order.has(id)) known.delete(id);
  return patch.order.map(id => {
    const run = known.get(id);
    if (!run) throw new Error("runs event names an unknown run");
    return run;
  });
}

/** Incremental Server-Sent Events parser: feed decoded text, receive complete events. */
export class EventParser {
  private buffer = "";
  private name = "message";
  private data: string[] = [];
  constructor(private readonly dispatch: (name: string, data: string) => void) {}
  push(text: string) {
    this.buffer += text;
    let index: number;
    while ((index = this.buffer.search(/\r\n|\r|\n/)) >= 0) {
      const line = this.buffer.slice(0, index);
      this.buffer = this.buffer.slice(index + (this.buffer.startsWith("\r\n", index) ? 2 : 1));
      if (line === "") {
        if (this.data.length) this.dispatch(this.name, this.data.join("\n"));
        this.name = "message"; this.data = [];
      } else if (line.startsWith(":")) continue;
      else {
        const colon = line.indexOf(":"), field = colon < 0 ? line : line.slice(0, colon);
        const value = colon < 0 ? "" : line.slice(colon + 1).replace(/^ /, "");
        if (field === "event") this.name = value;
        else if (field === "data") this.data.push(value);
      }
    }
    if (this.buffer.length > 4_000_000) throw new Error("event too large");
  }
}

class TopicConnection {
  readonly listeners = new Set<TopicEventListener>();
  status: TopicStreamStatus = "connecting";
  latest: Latest = {};
  private controller: AbortController | null = null;
  private retry: ReturnType<typeof setTimeout> | undefined;
  private idle: ReturnType<typeof setTimeout> | undefined;
  private hidden: ReturnType<typeof setTimeout> | undefined;
  private failures = 0;
  private closed = false;

  constructor(private readonly path: string, private readonly forget: () => void) {
    if (typeof document !== "undefined") document.addEventListener("visibilitychange", this.visibility);
    this.open();
  }

  add(listener: TopicEventListener) {
    clearTimeout(this.idle);
    this.listeners.add(listener);
    listener.status?.(this.status);
    const { messages, runs, agents, goal, bots } = this.latest;
    if (messages) listener.messages?.(messages);
    if (runs) listener.runs?.(runs);
    if (agents) listener.agents?.(agents);
    if (goal !== undefined) listener.goal?.(goal);
    if (bots) listener.bots?.(bots);
  }

  remove(listener: TopicEventListener) {
    this.listeners.delete(listener);
    if (!this.listeners.size) this.idle = setTimeout(() => this.close(), IDLE_CLOSE_MS);
  }

  private close() {
    this.closed = true;
    clearTimeout(this.retry); clearTimeout(this.hidden);
    if (typeof document !== "undefined") document.removeEventListener("visibilitychange", this.visibility);
    this.controller?.abort();
    this.forget();
  }

  private readonly visibility = () => {
    clearTimeout(this.hidden);
    if (document.hidden) this.hidden = setTimeout(() => {
      clearTimeout(this.retry);
      const controller = this.controller;
      this.controller = null;
      controller?.abort();
      this.setStatus("unavailable");
    }, HIDDEN_CLOSE_MS);
    else if (!this.controller && !this.closed) { clearTimeout(this.retry); this.open(); }
  };

  private setStatus(status: TopicStreamStatus) {
    if (status === this.status) return;
    this.status = status;
    for (const listener of [...this.listeners]) listener.status?.(status);
  }

  private emit<K extends keyof Latest>(name: K, value: Latest[K]) {
    this.latest[name] = value;
    for (const listener of [...this.listeners]) (listener[name] as ((value: Latest[K]) => void) | undefined)?.(value);
  }

  private open() {
    if (this.closed || this.controller || (typeof document !== "undefined" && document.hidden)) return;
    const controller = new AbortController();
    this.controller = controller;
    void this.read(controller).then(({ outcome, live }) => {
      // Closed, or paused for a hidden tab: the visibility handler reopens it.
      if (this.closed || this.controller !== controller) return;
      this.controller = null;
      // A connection that stayed live for a while clears earlier failures, so a flapping proxy still backs off.
      if (live >= 30_000) this.failures = 0;
      let delay: number;
      if (outcome === "ended") delay = 250 + Math.random() * 750;
      else {
        this.failures += 1;
        delay = outcome === "refused" ? MAX_RETRY_MS : Math.min(MAX_RETRY_MS, 1000 * 2 ** (this.failures - 1));
        this.setStatus("unavailable");
      }
      clearTimeout(this.retry);
      this.retry = setTimeout(() => this.open(), delay);
    });
  }

  /**
   * Read one connection: "ended" is a scheduled server end (lifetime or shutdown), "refused" a definitive refusal,
   * "failed" anything else. `live` is how long the connection was live, in milliseconds.
   */
  private async read(controller: AbortController): Promise<{ outcome: "ended" | "refused" | "failed"; live: number }> {
    const known = new Map<string, ModelRun>();
    let ready = false, readyAt = 0, ended: string | null = null;
    const result = (outcome: "ended" | "refused" | "failed") => ({ outcome, live: ready ? Date.now() - readyAt : 0 });
    let watchdog: ReturnType<typeof setTimeout> | undefined;
    const arm = (milliseconds: number) => { clearTimeout(watchdog); watchdog = setTimeout(() => controller.abort(), milliseconds); };
    arm(READY_TIMEOUT_MS);
    try {
      const response = await fetch("/api" + this.path, { credentials: "same-origin", cache: "no-store", signal: controller.signal,
        headers: { Accept: "text/event-stream", "Cache-Control": "no-transform" } });
      if (!response.ok || !response.body || !(response.headers.get("content-type") ?? "").startsWith("text/event-stream")) {
        await response.body?.cancel().catch(() => undefined);
        return result(response.status >= 400 && response.status < 500 || response.status === 503 ? "refused" : "failed");
      }
      let heartbeat = 15_000;
      const parser = new EventParser((name, data) => {
        const value = JSON.parse(data);
        recordTiming("event:" + name);
        if (name === "ready") {
          ready = true; readyAt = Date.now();
          if (Number.isSafeInteger(value?.heartbeat_ms) && value.heartbeat_ms > 0) heartbeat = value.heartbeat_ms;
          this.setStatus("live");
        } else if (name === "end") ended = String(value?.reason ?? "");
        else if (!ready) throw new Error("event before ready");
        else if (name === "messages") {
          if (!Number.isSafeInteger(value?.sequence) || !Number.isSafeInteger(value?.updates)) throw new Error("invalid watermark");
          this.emit("messages", { sequence: value.sequence, updates: value.updates });
        } else if (name === "runs") this.emit("runs", applyRunPatch(known, value));
        else if (name === "agents" && Array.isArray(value)) this.emit("agents", value);
        else if (name === "goal") this.emit("goal", value);
        else if (name === "bot-runs" && Array.isArray(value)) this.emit("bots", value);
      });
      const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
      for (;;) {
        const { done, value } = await reader.read();
        if (done || ended !== null) break;
        parser.push(value);
        // Any bytes, including heartbeat comments, prove the connection is alive.
        arm(ready ? heartbeat * 3 : READY_TIMEOUT_MS);
        if (ended !== null) { await reader.cancel().catch(() => undefined); break; }
      }
      // Lifetime and shutdown ends reconnect; a lost session or topic access falls back to polling, which reports it.
      return result(ended === "lifetime" || ended === "shutdown" ? "ended" : ended !== null ? "refused" : "failed");
    } catch {
      return result("failed");
    } finally {
      clearTimeout(watchdog);
      this.latest = {};
    }
  }
}

const connections = new Map<string, TopicConnection>();

/** Subscribe to a topic's event stream; the returned function unsubscribes. Listeners receive the latest state at once. */
export function subscribeTopicEvents(organizationId: string, topicId: string, listener: TopicEventListener): () => void {
  if (typeof fetch !== "function" || typeof TextDecoderStream !== "function") {
    listener.status?.("unavailable");
    return () => undefined;
  }
  const path = topicPath(organizationId, topicId) + "/events";
  let connection = connections.get(path);
  if (!connection) {
    const created: TopicConnection = new TopicConnection(path, () => { if (connections.get(path) === created) connections.delete(path); });
    connections.set(path, connection = created);
  }
  connection.add(listener);
  const current = connection;
  return () => current.remove(listener);
}
