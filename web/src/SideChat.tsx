// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
/**
 * Your private side chat beside an AI chat topic. You ask the topic's model privately; it reads the topic's shared
 * conversation, and ledger statements it records belong to the topic as usual. Only you see the side chat.
 *
 * "Bring to chat" on a reply posts it to the topic as one folded group, with the earlier side messages it builds on
 * (all of them, or those you keep ticked), or with only its own exchange and a short note in their place.
 */
import React, { useEffect, useRef, useState } from "react";
import { replyExcerpt } from "./MessageReplies";
import { modelName } from "./ReplyAdditions";
import { StatementChips, sideSpeaker } from "./SideChatGroup";
import { ApiError, Message, SideChat as SideChatState, SideMessage, SideRun, Topic, aborted, errorMessage, organizationPath, request,
  topicPath } from "./organization-api";
import { startPolling } from "./polling";
import "./side-chat.css";

const ACTIVE = new Set(["queued", "running"]);
const ENDED = new Set(["failed", "cancelled", "interrupted"]);

/** The earlier side messages a reply read: everything in the side chat before the question it answers. */
export function earlierMessages(messages: SideMessage[], reply: SideMessage): SideMessage[] {
  const question = messages.find(message => message.id === reply.reply_to);
  const before = question ? question.sequence : reply.sequence;
  return messages.filter(message => message.sequence < before);
}

/** The exchange a reply closes: its question, when it is still in view, and the reply itself. */
export function exchange(messages: SideMessage[], reply: SideMessage): SideMessage[] {
  const question = messages.find(message => message.id === reply.reply_to);
  return question ? [question, reply] : [reply];
}

export function mergeSide(existing: SideMessage[], incoming: SideMessage[]): SideMessage[] {
  if (!incoming.length) return existing;
  const byId = new Map(existing.map(message => [message.id, message]));
  for (const message of incoming) byId.set(message.id, message);
  return [...byId.values()].sort((a, b) => a.sequence - b.sequence);
}

/** What happened to a question that has no answer yet: a reply at work, one that ended without an answer, or none asked. */
export function pending(messages: SideMessage[], runs: SideRun[], question: SideMessage): { run?: SideRun; state: "working" | "ended" | "none" } | null {
  if (question.author_kind !== "human" || messages.some(message => message.reply_to === question.id)) return null;
  const run = runs.find(item => item.side_chat.question_id === question.id);
  if (!run) return { state: "none" };
  if (ACTIVE.has(run.status)) return { run, state: "working" };
  return ENDED.has(run.status) || run.status === "succeeded" ? { run, state: "ended" } : { run, state: "working" };
}

function BringPanel({ messages, reply, busy, onBring, onCancel }: {
  messages: SideMessage[]; reply: SideMessage; busy: boolean;
  onBring: (ids: string[], note: boolean) => void; onCancel: () => void;
}) {
  const earlier = earlierMessages(messages, reply), pair = exchange(messages, reply);
  const [kept, setKept] = useState(() => new Set(earlier.map(message => message.id)));
  const count = kept.size + pair.length;
  return <div className="org-side-bring" role="group" aria-label="Bring to chat" onKeyDown={event => {
    if (event.key === "Escape") { event.stopPropagation(); onCancel(); }
  }}>
    <p>{earlier.length ? `This reply builds on ${earlier.length} earlier side message${earlier.length === 1 ? "" : "s"}.`
      : "Post this exchange to the topic as one folded group."}</p>
    {earlier.length > 0 && <ul>{earlier.map(message => <li key={message.id}><label>
      <input type="checkbox" checked={kept.has(message.id)} disabled={busy} onChange={event => setKept(current => {
        const next = new Set(current);
        if (event.target.checked) next.add(message.id); else next.delete(message.id);
        return next;
      })} />
      <span><strong>{sideSpeaker(message, "You")}</strong> {replyExcerpt(message.content, 70)}</span>
    </label></li>)}</ul>}
    <div className="org-side-bring-actions">
      <button type="button" className="org-side-primary" disabled={busy} autoFocus
        onClick={() => onBring([...earlier.filter(message => kept.has(message.id)), ...pair].map(message => message.id), false)}>
        {count === earlier.length + pair.length && earlier.length ? `Bring all ${count}` : `Bring ${count}`}</button>
      {earlier.length > 0 && <button type="button" disabled={busy} onClick={() => onBring(pair.map(message => message.id), true)}>
        Bring {pair.length} with a context note</button>}
      <button type="button" disabled={busy} onClick={onCancel}>Cancel</button>
    </div>
  </div>;
}

export function SideChat({ organizationId, topic, owner, canWrite, model, effort, maxTokens, onClose, onBrought, onAuthLost, renderMarkdown }: {
  organizationId: string; topic: Topic; owner: string; canWrite: boolean;
  /** The conversation's reply model and effort; side chats answer with OpenRouter models. */
  model: string; effort: string | null; maxTokens: number | null;
  onClose: () => void; onBrought: (message: Message) => void; onAuthLost: () => void;
  renderMarkdown: (markdown: string) => React.ReactNode;
}) {
  const root = topicPath(organizationId, topic.id) + "/side-chat";
  const [messages, setMessages] = useState<SideMessage[]>([]);
  const [runs, setRuns] = useState<SideRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const [bringing, setBringing] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const latest = useRef(0), clientId = useRef(crypto.randomUUID()), poller = useRef<{ wake(): void } | null>(null);
  const viewport = useRef<HTMLDivElement>(null), textarea = useRef<HTMLTextAreaElement>(null);
  const failed = (cause: unknown) => {
    if (aborted(cause)) return;
    if (cause instanceof ApiError && cause.status === 401) onAuthLost(); else setError(errorMessage(cause));
  };
  useEffect(() => {
    const controller = new AbortController();
    poller.current = startPolling(async () => {
      try {
        const data = await request<SideChatState>(root + (latest.current ? "?after=" + latest.current : ""), { signal: controller.signal });
        setMessages(existing => mergeSide(existing, data.messages));
        latest.current = Math.max(latest.current, ...data.messages.map(message => message.sequence));
        setRuns(data.runs); setLoading(false);
        // A reply at work is checked often; otherwise nothing changes the side chat but you, so polling stops.
        return data.runs.some(run => ACTIVE.has(run.status)) ? 1500 : null;
      } catch (cause) {
        if (controller.signal.aborted) return null;
        failed(cause); setLoading(false);
        return 5000;
      }
    }, controller.signal);
    return () => controller.abort();
  }, [root]);
  const working = runs.some(run => ACTIVE.has(run.status));
  useEffect(() => {
    if (!working) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [working]);
  useEffect(() => {
    const view = viewport.current;
    if (view) view.scrollTop = view.scrollHeight;
  }, [messages.length, working]);
  useEffect(() => { requestAnimationFrame(() => textarea.current?.focus()); }, []);
  const unsupported = !model ? "Choose a reply model in the conversation's model menu to ask privately."
    : model.startsWith("bot/") ? "Side chats answer with OpenRouter models; choose one in the conversation's model menu." : "";
  const ask = async (questionId: string) => {
    const body = { question_id: questionId, models: [model], client_id: crypto.randomUUID(),
      ...(effort ? { effort } : {}), ...(maxTokens ? { max_completion_tokens: maxTokens } : {}) };
    const started = await request<SideRun[]>(root + "/replies", { method: "POST", body });
    setRuns(existing => [...started, ...existing.filter(run => !started.some(item => item.id === run.id))]);
    poller.current?.wake();
  };
  const send = async (event?: React.FormEvent) => {
    event?.preventDefault();
    if (!canWrite || sending || !text.trim() || unsupported) return;
    setSending(true); setError(""); setNotice("");
    try {
      const question = await request<SideMessage>(root + "/messages", { method: "POST", body: { content: text, client_id: clientId.current } });
      setMessages(existing => mergeSide(existing, [question]));
      setText(""); clientId.current = crypto.randomUUID();
      await ask(question.id);
    } catch (cause) { failed(cause); } finally { setSending(false); }
  };
  const retry = (questionId: string) => { setError(""); void ask(questionId).catch(failed); };
  const stop = (run: SideRun) => void request(organizationPath(organizationId) + "/inference-runs/" + encodeURIComponent(run.id) + "/cancel",
    { method: "POST", body: {} }).then(() => poller.current?.wake()).catch(failed);
  const bring = async (ids: string[], note: boolean) => {
    setBusy(true); setError("");
    try {
      const message = await request<Message>(root + "/bring", { method: "POST", body: { message_ids: ids, note, client_id: crypto.randomUUID() } });
      onBrought(message);
      setBringing(null);
      setNotice(note ? "Brought this exchange to the topic with a context note." : `Brought ${ids.length} messages to the topic.`);
    } catch (cause) { failed(cause); } finally { setBusy(false); }
  };
  return <aside className="org-side-chat" aria-label="Side chat">
    <header>
      <div><h2>Side chat</h2><small>Private · linked to {topic.name}</small></div>
      <button type="button" className="org-icon-button" aria-label="Close side chat" title="Close side chat" onClick={onClose}>×</button>
    </header>
    <div className="org-side-transcript" ref={viewport} tabIndex={0} aria-label="Side chat messages">
      {loading ? <p role="status" className="org-side-empty">Loading side chat…</p> : !messages.length && <div className="org-side-empty">
        <p>Ask the model privately about this topic. It reads the topic's conversation; only you see this side chat.</p>
        <p>Statements it records go to the topic's ledger as usual. Bring a reply to the chat when it is ready.</p></div>}
      {messages.map(message => {
        const state = pending(messages, runs, message);
        return <React.Fragment key={message.id}>
          <article className={"org-side-message org-side-message-" + message.author_kind}>
            <header><strong>{message.author_kind === "assistant" ? modelName(message.assistant_model ?? "model") : "You"}</strong>
              <time dateTime={new Date(message.created_at * 1000).toISOString()}>
                {new Date(message.created_at * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}</time></header>
            {renderMarkdown(message.content)}
            {message.author_kind === "assistant" && <StatementChips statements={message.statements ?? []} />}
            {message.author_kind === "assistant" && canWrite && <button type="button" className="org-side-bring-button"
              aria-expanded={bringing === message.id} onClick={() => setBringing(current => current === message.id ? null : message.id)}>
              Bring to chat</button>}
            {bringing === message.id && <BringPanel messages={messages} reply={message} busy={busy}
              onBring={(ids, note) => void bring(ids, note)} onCancel={() => setBringing(null)} />}
          </article>
          {state?.state === "working" && state.run && <div className="org-side-working" role="status">
            <span>{modelName(state.run.model)} is answering · {Math.max(0, Math.round((now / 1000) - (state.run.created_at ?? now / 1000)))} s</span>
            {canWrite && <button type="button" onClick={() => stop(state.run!)}>Stop</button>}</div>}
          {state && state.state !== "working" && <div className="org-side-working org-side-ended" role="status">
            <span>{state.state === "none" ? "No reply was requested." : state.run?.status === "cancelled" ? "The reply was stopped."
              : "The reply ended without an answer."}</span>
            {canWrite && !unsupported && <button type="button" onClick={() => retry(message.id)}>Ask again</button>}</div>}
        </React.Fragment>;
      })}
    </div>
    {error && <p role="alert" className="org-error org-side-error">{error}</p>}
    {notice && <p role="status" className="org-side-notice">{notice}</p>}
    <form className="org-side-composer" onSubmit={event => void send(event)}>
      <label className="org-visually-hidden" htmlFor="org-side-message">Side chat message</label>
      <textarea id="org-side-message" ref={textarea} rows={2} value={text} maxLength={16000} disabled={!canWrite || sending}
        placeholder={canWrite ? "Ask privately about this topic…" : "You can read this topic but not ask in it."}
        onChange={event => setText(event.target.value)} onKeyDown={event => {
          if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void send(); }
        }} />
      <div className="org-side-composer-row">
        <small>{unsupported || `${modelName(model)} · private`}</small>
        <button type="submit" disabled={!canWrite || sending || !text.trim() || Boolean(unsupported)}>{sending ? "Asking…" : "Ask"}</button>
      </div>
    </form>
  </aside>;
}
