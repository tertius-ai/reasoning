// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
/**
 * Side-chat messages someone brought into a topic. The post shows its author's words and the exchange as a card titled by
 * its first question; the card opens the exchange in the right-hand panel, where the side chat lives. Earlier posts the
 * replies built on are links, and side messages that stay private are only counted.
 */
import React, { useEffect, useRef, useState } from "react";
import { toneClass } from "./mentions";
import { replyExcerpt } from "./MessageReplies";
import { modelName } from "./ReplyAdditions";
import { FoldToggle, ReplyBody, foldFor, useOpenReplies } from "./FoldedReply";
import { replyParts } from "./replySummary";
import { appLink, followApplicationLink } from "./references";
import type { BroughtPost, Message, SideChatGroup as Group } from "./organization-api";
import "./side-chat.css";

/** A topic message a brought post can answer, as the side chat offers it. */
export type AnswerTarget = { id: string; name: string; excerpt: string };

/** The newest topic message from someone else since your own last message: the one a brought post offers to answer. */
export function answerTarget(messages: readonly Message[], userId: string, name: (message: Message) => string): AnswerTarget | undefined {
  for (let index = messages.length - 1; index >= 0; index--) {
    const message = messages[index];
    if ((message.author_kind ?? "human") !== "human") continue;
    if (message.author_id === userId) return undefined;
    return { id: message.id, name: name(message), excerpt: replyExcerpt(message.content, 60) };
  }
  return undefined;
}

/** A brought post your next side question is about: whose it is and the card's title. */
export type AboutDraft = { message_id: string; author_name: string; title: string };

export const sideSpeaker = (message: { author_kind: "human" | "assistant"; assistant_model: string | null }, owner: string) =>
  message.author_kind === "assistant" ? modelName(message.assistant_model ?? "model") : owner;

/** A post's time of day, with its date when it is not today. */
export function postTime(seconds: number, now = Date.now()): string {
  const date = new Date(seconds * 1000), time = date.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  return date.toDateString() === new Date(now).toDateString() ? time
    : `${date.toLocaleDateString(undefined, { month: "short", day: "numeric" })}, ${time}`;
}

/** "Builds on your post at 10:10 and 2 side messages that stay private", each earlier post a link to it. */
export function BuildsOn({ linked, omitted, whose, organizationId, topicId, onShowPost }: {
  linked: BroughtPost[]; omitted: number; whose: string; organizationId: string; topicId: string;
  onShowPost?: (id: string, event: React.MouseEvent<HTMLAnchorElement>) => void;
}) {
  if (!linked.length && !omitted) return null;
  const link = (post: BroughtPost, label: string) => <a key={post.message_id} className="org-side-group-link"
    onClick={event => onShowPost ? onShowPost(post.message_id, event) : followApplicationLink(event)}
    href={appLink({ organization: organizationId, topic: topicId, message: post.message_id })}>{label}</a>;
  const posts = linked.length === 1 ? link(linked[0], `${whose} post at ${postTime(linked[0].created_at)}`)
    : linked.length > 1 && <>{whose} posts at {linked.map((post, index) => <React.Fragment key={post.message_id}>
      {index ? index === linked.length - 1 ? " and " : ", " : ""}{link(post, postTime(post.created_at))}</React.Fragment>)}</>;
  return <p className="org-side-group-note">Builds on {posts}{posts && omitted > 0 && " and "}
    {omitted > 0 && `${omitted} side message${omitted === 1 ? " that stays" : "s that stay"} private`}.</p>;
}

type Brought = Group["messages"][number];

/** A brought group's title: its first question, or its first message when it starts with an answer. */
export function groupTitle(group: Pick<Group, "messages">): string {
  const first = group.messages.find(message => message.author_kind === "human") ?? group.messages[0];
  return first ? replyExcerpt(first.content, 90) : "Side chat";
}

/** "Your side chat · 2 exchanges · fast": whose side chat, how many answers it brings, and the model that gave them. */
export function cardDetails(group: Pick<Group, "messages">, whose: string): string {
  const answers = group.messages.filter(message => message.author_kind === "assistant"), model = answers[0]?.assistant_model;
  return [`${whose} side chat`, `${answers.length} exchange${answers.length === 1 ? "" : "s"}`, model ? modelName(model) : ""]
    .filter(Boolean).join(" · ");
}

const Icon = ({ children }: { children: React.ReactNode }) => <svg width="17" height="17" viewBox="0 0 24 24" fill="none"
  stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{children}</svg>;
const EXCHANGE = <Icon><path d="M21 14l-3-3h-7a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h9a1 1 0 0 1 1 1v10" />
  <path d="M14 15v2a1 1 0 0 1-1 1H6l-3 3V11a1 1 0 0 1 1-1h2" /></Icon>;
const PANEL = <Icon><rect x="4" y="4" width="16" height="16" rx="2" /><path d="M15 4v16" /></Icon>;

/** The card a brought post shows under its author's words; it opens the exchange in the right-hand panel. */
export function SideChatCard({ group, owner, own, open, organizationId, topicId, onOpen, onShowPost }: {
  group: Group; owner: string; own: boolean;
  /** Whether the panel shows this card's exchange. */
  open: boolean;
  organizationId: string; topicId: string; onOpen: () => void;
  onShowPost?: (id: string, event: React.MouseEvent<HTMLAnchorElement>) => void;
}) {
  const footer = (group.linked?.length ?? 0) + group.omitted > 0;
  return <div className="org-side-card" data-open={open || undefined} data-footer={footer || undefined}>
    <button type="button" className="org-side-card-open" aria-expanded={open} aria-controls="org-brought-panel"
      title={open ? "Close the exchange" : "Open the exchange beside the conversation"} onClick={onOpen}>
      <span className="org-side-card-icon">{EXCHANGE}</span>
      <span className="org-side-card-text"><strong>{groupTitle(group)}</strong><small>{cardDetails(group, own ? "Your" : `${owner}'s`)}</small></span>
      <span className="org-side-card-panel">{PANEL}</span>
    </button>
    <BuildsOn linked={group.linked ?? []} omitted={group.omitted} whose={own ? "your" : `${owner}'s`}
      organizationId={organizationId} topicId={topicId} onShowPost={onShowPost} />
  </div>;
}

/**
 * A brought post's exchange in the right-hand panel: read-only and in the side chat's style, with what it builds on. Your
 * own exchange continues in your side chat; someone else's can be asked about privately in yours.
 */
export function BroughtPanel({ post, owner, own, answers, organizationId, topicId, tones, canAsk, renderMarkdown, onClose, onShowPost,
  onContinue, onAsk }: {
  post: Pick<Message, "id" | "created_at"> & { side_chat: Group }; owner: string; own: boolean;
  /** Who the post answers, when it is a reply. */
  answers?: string;
  organizationId: string; topicId: string; tones: Map<string, number>;
  /** Whether you can ask in your side chat; viewers cannot. */
  canAsk: boolean;
  renderMarkdown: (markdown: string) => React.ReactNode;
  onClose: () => void; onShowPost?: (id: string, event: React.MouseEvent<HTMLAnchorElement>) => void;
  onContinue: () => void; onAsk: () => void;
}) {
  const group = post.side_chat, speaker = own ? "You" : owner;
  const [copied, setCopied] = useState(false);
  const [replyOpen, toggleReply] = useOpenReplies();
  const close = useRef<HTMLButtonElement>(null);
  useEffect(() => { close.current?.focus(); }, [post.id]);
  const key = (message: Brought) => message.author_kind === "assistant" ? "model:" + (message.assistant_model ?? "") : group.owner_id;
  const runs: Brought[][] = [];
  for (const message of group.messages) {
    const last = runs.at(-1);
    if (last && key(last[0]) === key(message)) last.push(message); else runs.push([message]);
  }
  const copy = () => void navigator.clipboard.writeText(group.messages.map(message => `${sideSpeaker(message, speaker)}: ${message.content}`)
    .join("\n\n")).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500); }).catch(() => undefined);
  return <aside className="org-side-chat org-brought-panel" id="org-brought-panel" aria-label="Exchange from a side chat"
    onKeyDown={event => { if (event.key === "Escape") { event.stopPropagation(); onClose(); } }}>
    <header>
      <div><h2>{own ? "From your side chat" : `From ${owner}'s side chat`}</h2>
        <small>Brought {postTime(post.created_at)}{answers ? ` · answers ${answers}` : ""}</small></div>
      <button ref={close} type="button" className="org-icon-button" aria-label="Close the exchange" title="Close · Esc" onClick={onClose}>×</button>
    </header>
    {(group.linked?.length ?? 0) + group.omitted > 0 && <div className="org-brought-chain">
      <BuildsOn linked={group.linked ?? []} omitted={group.omitted} whose={own ? "your" : `${owner}'s`} organizationId={organizationId}
        topicId={topicId} onShowPost={onShowPost} /></div>}
    <div className="org-side-transcript" tabIndex={0} aria-label="Brought messages">
      {runs.map(run => {
        const first = run[0], mine = own && first.author_kind === "human";
        return <div key={first.id} className={"org-message-group org-speaker-" + first.author_kind + " " + toneClass(tones, key(first))
          + (mine ? " org-message-group-mine" : "")}>{run.map((message, index) => {
          const parts = message.author_kind === "assistant" ? replyParts(message.content) : null;
          const fold = foldFor(parts, replyOpen(message.id), () => toggleReply(message.id));
          return <article key={message.id} className={"org-message org-side-message" + (index ? " org-message-continuation" : "")}>
            <header>{!index && <strong>{sideSpeaker(message, speaker)}</strong>}
              <time dateTime={new Date(message.created_at * 1000).toISOString()}>{postTime(message.created_at)}</time></header>
            {parts ? <ReplyBody parts={parts} fold={fold} render={renderMarkdown} /> : renderMarkdown(message.content)}
            {fold && <div className="org-usage-row"><FoldToggle fold={fold} /></div>}
          </article>;
        })}</div>;
      })}
    </div>
    <footer className="org-brought-footer">
      <button type="button" onClick={copy}>{copied ? "Copied" : "Copy"}</button>
      {own ? <button type="button" className="org-side-primary" onClick={onContinue}>Continue in side chat</button>
        : canAsk && <button type="button" className="org-side-primary" onClick={onAsk}>Ask privately about this</button>}
    </footer>
  </aside>;
}
