// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
/**
 * Your private side chat beside an AI chat topic. You ask the conversation's chosen model privately; it reads the topic's
 * shared conversation, and ledger statements it records belong to the topic as usual. Only you see the side chat. It reads
 * like the conversation, in the speakers' colors with your messages on the right, but its rails are dashed.
 *
 * "Bring to chat" on a reply posts its exchange to the topic as one folded group, under your own words and, when someone
 * asked since your last message, as an answer to them. Earlier exchanges can come along; those already in the topic are
 * linked, never repeated.
 */
import React, { useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { type MentionBinding, editMentions, encodeMentions } from "./composerMentions";
import { ReturnIcon } from "./ComposerIcons";
import { TONES, mentionInitials, speakerKey, toneClass } from "./mentions";
import { replyExcerpt } from "./MessageReplies";
import { modelName, replyTimeline } from "./ReplyAdditions";
import { ReplyFooter } from "./ReplyFooter";
import { ReplyTimeline } from "./ReplyTimeline";
import { type AboutDraft, type AnswerTarget, postTime } from "./SideChatGroup";
import { ApiError, BroughtPost, Member, Message, ReplyAddition, SideChat as SideChatState, SideMessage, SideRun, Topic, aborted,
  errorMessage, mentionQuery, organizationPath, request, topicPath } from "./organization-api";
import { startPolling } from "./polling";
import { appLink, followApplicationLink } from "./references";
import "./side-chat.css";

const ACTIVE = new Set(["queued", "running"]);
const ENDED = new Set(["failed", "cancelled", "interrupted"]);

/** The earlier side messages a reply read: everything in the side chat before the question it answers. */
export function earlierMessages(messages: SideMessage[], reply: SideMessage): SideMessage[] {
  const question = messages.find(message => message.id === reply.reply_to);
  const before = question ? question.sequence : reply.sequence;
  return messages.filter(message => message.sequence < before);
}

/** The exchange a reply closes: its question, when it is still in view, the side messages added to the reply that it read,
 * and the reply itself. */
export function exchange(messages: SideMessage[], reply: SideMessage): SideMessage[] {
  const read = new Set(reply.additions?.map(addition => addition.message_id));
  return messages.filter(message => message.id === reply.reply_to || read.has(message.id) || message.id === reply.id);
}

/** The side messages added to a side reply: those its run lists, then any posted since the side chat last read its runs. */
export function sideAdditions(run: SideRun, messages: SideMessage[]): ReplyAddition[] {
  const listed = run.additions ?? [], known = new Set(listed.map(addition => addition.message_id));
  return [...listed, ...messages.flatMap(message => message.addition?.run_id === run.id && !known.has(message.id) ? [{
    message_id: message.id, run_id: run.id, added_by: message.owner_id, moved: false, cancelled_runs: [], created_at: message.created_at,
    read_at: message.addition.read_at, read_step: message.addition.read_step, content: message.content }] : [])];
}

/** An answered exchange, with the topic post that brought it if one did. */
export type Exchange = { question?: SideMessage; reply: SideMessage; brought: BroughtPost | null };

/** The answered exchanges a reply read, newest first. */
export function earlierExchanges(messages: SideMessage[], reply: SideMessage): Exchange[] {
  const before = earlierMessages(messages, reply), byId = new Map(before.map(message => [message.id, message]));
  return before.filter(message => message.author_kind === "assistant").map(answer => {
    const question = answer.reply_to ? byId.get(answer.reply_to) : undefined;
    return { question, reply: answer, brought: answer.brought ?? question?.brought ?? null };
  }).reverse();
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

/** A question whose reply has not come (at work, ended without an answer, or never asked for) and the model that is to give it. */
export type SideWaiting = { question: SideMessage; run?: SideRun; state: "working" | "ended" | "none"; model: string };

/**
 * The side chat as it reads: consecutive messages of one speaker grouped as the conversation groups them, and after a question
 * whose reply has not come, the reply's place on the model's side, which the reply takes when it comes. A message added to a
 * reply shows in that reply's steps while it works and folded under the answer that read it; one taken back stays here as a
 * message of its own, and one the reply finished without reading follows that answer, as the next question.
 */
export function sideTranscript(messages: SideMessage[], runs: SideRun[], model: string): (SideMessage[] | SideWaiting)[] {
  const working = new Set(runs.filter(run => ACTIVE.has(run.status)).map(run => run.id));
  const folded = new Set(messages.flatMap(message => message.additions?.map(addition => addition.message_id) ?? []));
  const answered = new Set(messages.flatMap(message => message.author_kind === "assistant" && message.inference_run_id ? [message.inference_run_id] : []));
  const shown: SideMessage[] = [], unread = new Map<string, SideMessage[]>();
  for (const message of messages) {
    const run = message.addition?.run_id;
    if (run && (working.has(run) || folded.has(message.id))) continue;
    if (run && answered.has(run)) { unread.set(run, [...unread.get(run) ?? [], message]); continue; }
    shown.push(message);
    if (message.author_kind === "assistant" && message.inference_run_id) shown.push(...unread.get(message.inference_run_id) ?? []);
  }
  const items: (SideMessage[] | SideWaiting)[] = [];
  for (const message of shown) {
    const last = items.at(-1);
    if (Array.isArray(last) && last[0].author_kind === message.author_kind && last[0].assistant_model === message.assistant_model) last.push(message);
    else items.push([message]);
    // A reply's place is named for its model; a message a reply finished without reading, for that reply's.
    const waiting = pending(messages, runs, message), added = runs.find(run => run.id === message.addition?.run_id);
    if (waiting) items.push({ question: message, ...waiting, model: waiting.run?.model ?? added?.model ?? model });
  }
  return items;
}

type Speaker = Pick<SideMessage, "owner_id" | "author_kind" | "assistant_model">;
const sideKey = (message: Speaker) => speakerKey({ author_id: message.owner_id, author_kind: message.author_kind,
  assistant_model: message.assistant_model });
/** Who is to speak in a reply's place: the model that gives it. */
const waitingSpeaker = (item: SideWaiting): Speaker => ({ owner_id: item.question.owner_id, author_kind: "assistant", assistant_model: item.model });

/** The conversation's colors, extended to side-chat speakers who have not spoken in the conversation. */
export function sideTones(tones: Map<string, number>, messages: Speaker[]): Map<string, number> {
  const extended = new Map(tones);
  for (const message of messages) if (!extended.has(sideKey(message))) extended.set(sideKey(message), extended.size % TONES);
  return extended;
}

/** A text box grows with its text up to `limit` pixels and then scrolls; it has no size handle. */
function grow(element: HTMLTextAreaElement | null, limit: number) {
  if (!element) return;
  element.style.height = "auto";
  element.style.height = Math.min(element.scrollHeight, limit) + "px";
}

const clock = (seconds: number) => new Date(seconds * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
const Icon = ({ children }: { children: React.ReactNode }) => <svg width="15" height="15" viewBox="0 0 24 24" fill="none"
  stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{children}</svg>;
const COPY = <Icon><rect x="8" y="8" width="12" height="12" rx="2" /><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2" /></Icon>;
const COPIED = <Icon><path d="M5 12l5 5L20 7" /></Icon>;
const BRING = <Icon><path d="M7 17L17 7" /><path d="M8 7h9v9" /></Icon>;
const LOCK = <Icon><rect x="5" y="11" width="14" height="10" rx="2" /><path d="M8 11V7a4 4 0 0 1 8 0v4" /></Icon>;
const LINK = <Icon><path d="M9 15l6-6" /><path d="M11 6l.46-.46a5 5 0 0 1 7.07 7.07L18 13" /><path d="M13 18l-.4.4a5 5 0 0 1-7.07-7.07L6 11" /></Icon>;

const caption = (item: { question?: SideMessage; reply: SideMessage }) =>
  [item.question && replyExcerpt(item.question.content, 44), replyExcerpt(item.reply.content, 44)].filter(Boolean).join(" · ");

/**
 * The menu under a reply: its exchange always goes; earlier exchanges can be ticked, and those already in the topic are
 * linked instead. Your words go above the group; `@` mentions a member. A suggested answer target can be removed.
 */
function BringMenu({ messages, reply, answer: suggested, people, tones, busy, onBring, onCancel }: {
  messages: SideMessage[]; reply: SideMessage; answer?: AnswerTarget; people: Pick<Member, "id" | "name">[];
  tones: Map<string, number>; busy: boolean;
  onBring: (ids: string[], words: string, replyTo: string | null, clientId: string) => void; onCancel: () => void;
}) {
  const own = exchange(messages, reply), earlier = earlierExchanges(messages, reply);
  const question = own.find(message => message.id === reply.reply_to);
  // A question asked about someone's post answers that post; otherwise the newest message from someone else.
  const about = question?.about;
  const answer = about ? { id: about.message_id, name: about.author_name ?? "someone", excerpt: `the post at ${postTime(about.created_at)}` }
    : suggested;
  const [kept, setKept] = useState<ReadonlySet<string>>(() => new Set());
  const [words, setWords] = useState(""), [bindings, setBindings] = useState<MentionBinding[]>([]);
  const [answering, setAnswering] = useState(true);
  const [mention, setMention] = useState<{ start: number; end: number; query: string } | null>(null);
  const [active, setActive] = useState(0);
  const field = useRef<HTMLTextAreaElement>(null), clientId = useRef(crypto.randomUUID()), id = useId();
  useEffect(() => { requestAnimationFrame(() => field.current?.focus()); }, []);
  useEffect(() => grow(field.current, 120), [words]);
  // The caret goes after a chosen mention in the same commit as the new text, so the next key lands after it.
  const caretAfter = useRef<number | null>(null);
  useLayoutEffect(() => {
    if (caretAfter.current === null || !field.current) return;
    field.current.setSelectionRange(caretAfter.current, caretAfter.current);
    caretAfter.current = null;
  }, [words]);
  const matches = mention ? people.filter(person => person.name.toLocaleLowerCase().includes(mention.query.toLocaleLowerCase())).slice(0, 8) : [];
  const target = answering ? answer : undefined, count = 1 + kept.size;
  const submit = () => {
    if (busy) return;
    const ids = [...earlier.filter(item => kept.has(item.reply.id)).flatMap(item => exchange(messages, item.reply).map(message => message.id)),
      ...own.map(message => message.id)];
    onBring(ids, encodeMentions(words, bindings).trim(), target?.id ?? null, clientId.current);
  };
  const choose = (person: Pick<Member, "id" | "name">) => {
    if (!mention) return;
    const label = "@" + person.name, text = words.slice(0, mention.start) + label + " " + words.slice(mention.end);
    setBindings([...editMentions(words, text, bindings), { start: mention.start, end: mention.start + label.length, id: person.id, label }]);
    caretAfter.current = mention.start + label.length + 1;
    setWords(text); setMention(null);
    field.current?.focus();
  };
  const track = (input: HTMLTextAreaElement) => { setMention(mentionQuery(input.value, input.selectionStart ?? 0)); setActive(0); };
  return <div className="org-side-bring" role="group" aria-label="Bring to chat" onKeyDown={event => {
    if (event.key !== "Escape") return;
    event.stopPropagation();
    if (matches.length) setMention(null); else onCancel();
  }}>
    <p className="org-side-bring-title">Bring to chat</p>
    <ul className="org-side-exchanges">
      <li><span className="org-side-exchange-mark">{LOCK}</span>
        <span><strong>This exchange</strong><small>{caption({ question, reply })}</small></span></li>
      {earlier.map(item => item.brought
        ? <li key={item.reply.id} className="org-side-exchange-posted"><span className="org-side-exchange-mark">{LINK}</span>
          <span><strong>In chat since {postTime(item.brought.created_at)}</strong><small>{caption(item)} · linked, not repeated</small></span></li>
        : <li key={item.reply.id}><label><input type="checkbox" checked={kept.has(item.reply.id)} disabled={busy} onChange={event => {
          const checked = event.target.checked;
          setKept(current => {
            const next = new Set(current);
            if (checked) next.add(item.reply.id); else next.delete(item.reply.id);
            return next;
          });
        }} />
          <span><strong>Earlier: {postTime((item.question ?? item.reply).created_at)}</strong><small>{caption(item)}</small></span></label></li>)}
    </ul>
    {answer && answering && <p className="org-side-answer"><span aria-hidden="true">↳</span>
      <span>Answer <strong>{answer.name}</strong> · {answer.excerpt}</span>
      <button type="button" aria-label={`Post without answering ${answer.name}`} title="Post without answering" disabled={busy}
        onClick={() => { setAnswering(false); field.current?.focus(); }}>×</button></p>}
    <label className="org-visually-hidden" htmlFor={id}>Your words</label>
    <textarea id={id} ref={field} className="org-side-words" rows={1} value={words} maxLength={4000} disabled={busy}
      placeholder="Add your own words · @ mentions someone" aria-autocomplete="list"
      aria-controls={matches.length ? id + "-people" : undefined}
      aria-activedescendant={matches.length ? `${id}-person-${matches[active % matches.length].id}` : undefined}
      onChange={event => {
        const text = event.target.value;
        setBindings(editMentions(words, text, bindings)); setWords(text); track(event.target);
      }}
      onSelect={event => track(event.currentTarget)}
      onKeyDown={event => {
        if (event.nativeEvent.isComposing) return;
        if (matches.length && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
          event.preventDefault();
          setActive(value => (value + (event.key === "ArrowDown" ? 1 : matches.length - 1)) % matches.length);
        } else if (matches.length && (event.key === "Enter" || event.key === "Tab") && !event.shiftKey) {
          event.preventDefault(); choose(matches[active % matches.length]);
        } else if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); submit(); }
      }} />
    {matches.length > 0 && <div id={id + "-people"} role="listbox" className="org-mention-menu org-side-people" aria-label="Mention a member">
      {matches.map((person, index) => <button type="button" role="option" tabIndex={-1} id={`${id}-person-${person.id}`} key={person.id}
        aria-selected={index === active % matches.length} onMouseDown={event => event.preventDefault()} onClick={() => choose(person)}>
        <span className={"org-mention-avatar " + toneClass(tones, person.id)} aria-hidden="true">{mentionInitials(person.name)}</span>
        <span className="org-mention-option-name">{person.name}</span></button>)}</div>}
    <div className="org-side-bring-actions">
      <small>Enter brings · Shift+Enter, new line</small>
      <button type="button" disabled={busy} onClick={onCancel}>Cancel</button>
      <button type="button" className="org-side-primary" disabled={busy} onClick={submit}>
        {`Bring ${count} exchange${count === 1 ? "" : "s"}${target ? " as a reply" : ""}`}</button>
    </div>
  </div>;
}

export function SideChat({ organizationId, topic, userId, canWrite, model, effort, maxTokens, people, tones, answer, onClose, onBrought,
  onShowPost, onAuthLost, renderMarkdown, onFocused, hidden = false, about, onAbout, focusMessage, onShowAbout }: {
  organizationId: string; topic: Topic; userId: string; canWrite: boolean;
  /** The conversation's chosen reply model and effort, which the side chat asks; side chats answer with OpenRouter models. */
  model: string; effort: string | null; maxTokens: number | null;
  /** Members your words can mention (yourself left out), and the conversation's speaker colors. */
  people: Pick<Member, "id" | "name">[]; tones: Map<string, number>;
  /** The topic message a brought post offers to answer: the newest from someone else since your last message. */
  answer?: AnswerTarget;
  onClose: () => void; onBrought: (message: Message) => void; onAuthLost: () => void;
  /** Shows the topic post that brought a reply: scrolls to it when it is on screen, or follows its link. */
  onShowPost?: (id: string, event: React.MouseEvent<HTMLAnchorElement>) => void;
  renderMarkdown: (markdown: string) => React.ReactNode;
  /** Hidden, but kept as it is, while the panel shows a brought exchange instead. */
  hidden?: boolean;
  /** A brought post your next question is about, shown above the box until you ask or remove it. */
  about?: AboutDraft | null; onAbout?: (about: AboutDraft | null) => void;
  /** A side message to scroll to and mark, as when a brought exchange continues here. */
  focusMessage?: { id: string; nonce: number } | null;
  /** Called once the side chat has scrolled to `focusMessage`, so it is not done again when the side chat reopens. */
  onFocused?: () => void;
  /** Shows the topic message a question was about. */
  onShowAbout?: (messageId: string) => void;
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
  const [copied, setCopied] = useState<string | null>(null);
  const [marked, setMarked] = useState<string | null>(null);
  const [asking, setAsking] = useState<string | null>(null);
  const [addingTo, setAddingTo] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const latest = useRef(0), clientId = useRef(crypto.randomUUID()), poller = useRef<{ wake(): void } | null>(null);
  const viewport = useRef<HTMLDivElement>(null), textarea = useRef<HTMLTextAreaElement>(null), atEnd = useRef(true);
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
  // As a reply at work takes steps and added messages, the side chat follows it while you are at its end.
  const progress = runs.map(run => `${run.activity?.length ?? 0}:${run.additions?.length ?? 0}`).join();
  useEffect(() => {
    const view = viewport.current;
    if (view && atEnd.current) view.scrollTop = view.scrollHeight;
  }, [progress]);
  // The reply your next message joins, while it is at work; once it finishes, the next message asks again.
  const adding = addingTo ? runs.find(run => run.id === addingTo && ACTIVE.has(run.status) && !run.cancel_requested) : undefined;
  const addingQuestion = adding && messages.find(message => message.id === adding.side_chat.question_id);
  useEffect(() => { if (addingTo && !adding) setAddingTo(null); }, [addingTo, adding]);
  // Asking about a post is a question of its own.
  useEffect(() => { if (about) setAddingTo(null); }, [about?.message_id]);
  useEffect(() => { requestAnimationFrame(() => textarea.current?.focus()); }, []);
  useEffect(() => grow(textarea.current, 160), [text]);
  // Asking about a post starts here with the box ready.
  useEffect(() => { if (about) requestAnimationFrame(() => textarea.current?.focus()); }, [about?.message_id]);
  // A brought exchange that continues here is scrolled into view and marked for a moment, once.
  useEffect(() => {
    if (!focusMessage || loading) return;
    viewport.current?.querySelector(`[data-side-message-id="${focusMessage.id}"]`)?.scrollIntoView({ block: "center" });
    setMarked(focusMessage.id);
    onFocused?.();
  }, [focusMessage?.nonce, loading]);
  useEffect(() => {
    if (!marked) return;
    const timer = setTimeout(() => setMarked(null), 2600);
    return () => clearTimeout(timer);
  }, [marked]);
  const transcript = useMemo(() => sideTranscript(messages, runs, model), [messages, runs, model]);
  // A reply's place takes the color its reply will have.
  const colors = useMemo(() => sideTones(tones, [...messages, ...transcript.flatMap(item => Array.isArray(item) ? [] : [waitingSpeaker(item)])]),
    [tones, messages, transcript]);
  const unsupported = !model ? "Choose a reply model in the conversation's model menu to ask privately."
    : model.startsWith("bot/") ? "Side chats answer with OpenRouter models; choose one in the conversation's model menu." : "";
  const ask = async (questionId: string) => {
    const body = { question_id: questionId, models: [model], client_id: crypto.randomUUID(),
      ...(effort ? { effort } : {}), ...(maxTokens ? { max_completion_tokens: maxTokens } : {}) };
    // Until the reply's run comes back, the question's place already says it is being answered.
    setAsking(questionId);
    try {
      const started = await request<SideRun[]>(root + "/replies", { method: "POST", body });
      setRuns(existing => [...started, ...existing.filter(run => !started.some(item => item.id === run.id))]);
      poller.current?.wake();
    } finally { setAsking(current => current === questionId ? null : current); }
  };
  const send = async (event?: React.FormEvent) => {
    event?.preventDefault();
    if (!canWrite || sending || !text.trim() || unsupported) return;
    setSending(true); setError(""); setNotice("");
    const target = adding, body = { content: text, client_id: clientId.current, ...(about ? { about_message_id: about.message_id } : {}) };
    const posted = (message: SideMessage) => {
      setMessages(existing => mergeSide(existing, [message]));
      setText(""); clientId.current = crypto.randomUUID(); setAddingTo(null);
    };
    try {
      if (target) {
        try {
          // The message joins the reply at work, which reads it before its next step; it asks for no reply of its own.
          posted(await request<SideMessage>(root + "/messages", { method: "POST", body: { ...body, adds_to: target.id } }));
          poller.current?.wake();
          return;
        } catch (cause) {
          // The reply finished first: nothing was posted, so the same message is asked as a new question.
          if (!(cause instanceof ApiError) || cause.status !== 409) throw cause;
          setNotice(`${modelName(target.model)}'s reply finished first, so this was asked as a new question.`);
        }
      }
      const question = await request<SideMessage>(root + "/messages", { method: "POST", body });
      posted(question); onAbout?.(null);
      await ask(question.id);
    } catch (cause) { failed(cause); } finally { setSending(false); }
  };
  const retry = (questionId: string) => { setError(""); void ask(questionId).catch(failed); };
  const toggleAdding = (run: SideRun) => {
    if (addingTo === run.id) { setAddingTo(null); return; }
    onAbout?.(null); setAddingTo(run.id);
    requestAnimationFrame(() => textarea.current?.focus());
  };
  // An added message the reply has not read can be taken back; it stays in the side chat as a message of its own.
  const undo = (addition: ReplyAddition) => void request<SideMessage>(root + "/additions/" + encodeURIComponent(addition.message_id),
    { method: "DELETE" }).then(message => {
    setMessages(existing => mergeSide(existing, [message]));
    setRuns(existing => existing.map(run => run.id === addition.run_id
      ? { ...run, additions: run.additions?.filter(item => item.message_id !== addition.message_id) } : run));
  }).catch(failed);
  const stop = (run: SideRun) => void request(organizationPath(organizationId) + "/inference-runs/" + encodeURIComponent(run.id) + "/cancel",
    { method: "POST", body: {} }).then(() => poller.current?.wake()).catch(failed);
  const copy = (message: SideMessage) => void navigator.clipboard.writeText(message.content).then(() => {
    setCopied(message.id);
    setTimeout(() => setCopied(current => current === message.id ? null : current), 1500);
  }).catch(() => setError("Could not copy; select the text and copy it instead."));
  // Which side messages are in the topic changes only by bringing; after a refusal, read the whole side chat again.
  const reload = () => void request<SideChatState>(root).then(data => setMessages(data.messages)).catch(failed);
  const bring = async (ids: string[], words: string, replyTo: string | null, client: string) => {
    setBusy(true); setError(""); setNotice("");
    try {
      const post = await request<Message>(root + "/bring", { method: "POST",
        body: { message_ids: ids, client_id: client, ...(words ? { words } : {}), ...(replyTo ? { reply_to: replyTo } : {}) } });
      const carried = new Set(post.side_chat?.messages.map(message => message.id) ?? ids);
      setMessages(existing => existing.map(message => carried.has(message.id)
        ? { ...message, brought: { message_id: post.id, created_at: post.created_at } } : message));
      onBrought(post); setBringing(null);
      const count = messages.filter(message => carried.has(message.id) && message.author_kind === "assistant").length;
      setNotice(`Brought ${count} exchange${count === 1 ? "" : "s"} to the topic${replyTo ? " as a reply" : ""}.`);
    } catch (cause) {
      failed(cause);
      if (cause instanceof ApiError && cause.status === 409) reload();
    } finally { setBusy(false); }
  };
  return <aside className="org-side-chat" aria-label="Side chat" hidden={hidden}>
    <header>
      <div><h2>Side chat</h2><small>Private · linked to {topic.name}</small></div>
      <button type="button" className="org-icon-button" aria-label="Close side chat" title="Close side chat" onClick={onClose}>×</button>
    </header>
    <div className="org-side-transcript" ref={viewport} tabIndex={0} aria-label="Side chat messages" onScroll={event => {
      const view = event.currentTarget;
      atEnd.current = view.scrollHeight - view.scrollTop - view.clientHeight < 64;
    }}>
      {loading ? <p role="status" className="org-side-empty">Loading side chat…</p> : !messages.length && <div className="org-side-empty">
        <p>Ask the model privately about this topic. It reads the topic's conversation; only you see this side chat.</p>
        <p>Statements it records go to the topic's ledger as usual. Bring a reply to the chat when it is ready.</p></div>}
      {transcript.map(item => {
        // A reply that has not come waits in its place on the model's side, named and in the color its reply will have. At
        // work, it shows its steps and the messages added to it, as a reply in the conversation does; "+ Add" makes your
        // next message join it.
        if (!Array.isArray(item)) {
          const run = item.state === "working" ? item.run : undefined, answering = Boolean(run) || asking === item.question.id;
          const additions = run ? sideAdditions(run, messages) : [], joining = Boolean(run && addingTo === run.id);
          return <div key={"waiting-" + item.question.id} className={"org-message-group org-speaker-assistant org-side-waiting "
            + toneClass(colors, sideKey(waitingSpeaker(item)))}>
            <article className="org-message org-side-message"><header><strong>{modelName(item.model || "model")}</strong></header>
              {answering ? <div className="org-side-working" role="status">
                <span>Answering · {run ? Math.max(0, Math.round((now / 1000) - (run.created_at ?? now / 1000))) : 0} s</span>
                {canWrite && run && !run.cancel_requested && <button type="button" className="org-side-add" aria-pressed={joining}
                  aria-label={joining ? "Adding to this reply" : "Add to this reply"}
                  title={joining ? "Your next message joins this reply; click to ask a new question instead" : "Add your next message to this reply"}
                  onClick={() => toggleAdding(run)}>{joining ? "Adding…" : "+ Add"}</button>}
                {canWrite && run && <button type="button" onClick={() => stop(run)}>Stop</button>}</div>
                : <div className="org-side-working org-side-ended" role="status">
                  <span>{item.state === "none" ? item.question.addition ? "The reply finished before reading this." : "No reply was requested."
                    : item.run?.status === "cancelled" ? "The reply was stopped." : "The reply ended without an answer."}</span>
                  {canWrite && !unsupported && <button type="button" onClick={() => retry(item.question.id)}>Ask again</button>}</div>}
              {run && replyTimeline(run.activity ?? [], additions, true).length > 0 && <ReplyTimeline activity={run.activity ?? []}
                additions={additions} active userId={userId} onUndo={canWrite ? undo : undefined} label="Reply steps and added messages" />}
            </article></div>;
        }
        const first = item[0], mine = first.author_kind === "human";
        return <div key={first.id} className={"org-message-group org-speaker-" + first.author_kind + " " + toneClass(colors, sideKey(first))
          + (mine ? " org-message-group-mine" : "")}>{item.map((message, index) => {
          return <React.Fragment key={message.id}>
            <article className={"org-message org-side-message" + (index ? " org-message-continuation" : "")
              + (message.id === marked ? " org-message-linked" : "")} data-side-message-id={message.id}>
              <header>{!index && <strong>{mine ? "You" : modelName(message.assistant_model ?? "model")}</strong>}
                <time dateTime={new Date(message.created_at * 1000).toISOString()} title={new Date(message.created_at * 1000).toLocaleString()}>
                  {clock(message.created_at)}</time></header>
              {message.about && <p className="org-side-about"><button type="button" onClick={() => onShowAbout?.(message.about!.message_id)}>
                About {message.about.author_id === userId ? "your" : `${message.about.author_name ?? "someone"}'s`} post at {postTime(message.about.created_at)}
              </button></p>}
              {renderMarkdown(message.content)}
              {message.author_kind === "assistant" && <ReplyFooter message={message} userId={userId} names={{}} />}
              {message.author_kind === "assistant" && <div className="org-side-actions" role="toolbar" aria-label="Reply actions">
                <button type="button" aria-label={copied === message.id ? "Copied" : "Copy reply"} title="Copy"
                  onClick={() => copy(message)}>{copied === message.id ? COPIED : COPY}</button>
                {message.brought ? <a aria-label="Show it in the topic" title={`In the topic since ${postTime(message.brought.created_at)}`}
                  href={appLink({ organization: organizationId, topic: topic.id, message: message.brought.message_id })}
                  onClick={event => onShowPost ? onShowPost(message.brought!.message_id, event) : followApplicationLink(event)}>{LINK}</a>
                  : canWrite && <button type="button" aria-label="Bring to chat" title="Bring to chat" aria-expanded={bringing === message.id}
                    onClick={() => setBringing(current => current === message.id ? null : message.id)}>{BRING}</button>}
              </div>}
            </article>
            {bringing === message.id && <BringMenu messages={messages} reply={message} answer={answer} people={people} tones={colors}
              busy={busy} onBring={(ids, words, replyTo, client) => void bring(ids, words, replyTo, client)}
              onCancel={() => setBringing(null)} />}
          </React.Fragment>;
        })}</div>;
      })}
    </div>
    {error && <p role="alert" className="org-error org-side-error">{error}</p>}
    {notice && <p role="status" className="org-side-notice">{notice}</p>}
    {unsupported && <p role="status" className="org-side-unsupported">{unsupported}</p>}
    {about && <div className="org-side-about-bar" role="status"><span aria-hidden="true">↳</span>
      <span>About <strong>{about.author_name}'s exchange</strong> · {about.title}</span>
      <button type="button" aria-label="Ask without it" title="Ask without it" onClick={() => onAbout?.(null)}>×</button></div>}
    {adding && <div className="org-side-about-bar" role="status"><span aria-hidden="true">↳</span>
      <span>Adding to <strong>{modelName(adding.model)}'s reply</strong>{addingQuestion && <> · {replyExcerpt(addingQuestion.content, 60)}</>}</span>
      <button type="button" aria-label="Stop adding" title="Back to a new question · Esc" onClick={() => setAddingTo(null)}>×</button></div>}
    <form className="org-side-composer" onSubmit={event => void send(event)}>
      <label className="org-visually-hidden" htmlFor="org-side-message">Side chat message</label>
      <textarea id="org-side-message" ref={textarea} rows={1} value={text} maxLength={16000} disabled={!canWrite || sending}
        placeholder={!canWrite ? "You can read this topic but not ask in it." : adding ? "Add to the reply…" : "Ask privately…"}
        onChange={event => setText(event.target.value)} onKeyDown={event => {
          if (event.key === "Escape" && adding) { event.preventDefault(); event.stopPropagation(); setAddingTo(null); }
          if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void send(); }
        }} />
      <button type="submit" className="org-composer-button org-side-send" aria-label={adding ? "Add to the reply" : "Ask privately"}
        title={adding ? "Add · Enter" : "Ask · Enter"} disabled={!canWrite || sending || !text.trim() || Boolean(unsupported)}><ReturnIcon /></button>
    </form>
  </aside>;
}
