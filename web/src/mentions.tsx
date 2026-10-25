// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import React, { createContext, forwardRef, useContext } from "react";
import type { MentionBinding } from "./composerMentions";

/** The reader's own account id: mentions of the reader are marked like Signal's mentions of you. */
export const MentionSelf = createContext<string>("");

const MENTION_URL = /^#mention\/([a-f0-9]{32})$/;
/** The member a Markdown link produced by `mentionMarkdown` names. */
export function mentionFromHref(href: string | undefined): string | null {
  const match = href ? MENTION_URL.exec(href) : null;
  return match ? match[1] : null;
}

/** A mention in a sent message: tinted text, amber when it names the reader. */
export function MentionTag({ id, children }: { id: string; children: React.ReactNode }) {
  const self = useContext(MentionSelf);
  return <span className={"org-mention" + (id === self ? " org-mention-self" : "")} data-member-id={id}>{children}</span>;
}

/** Up to two initials for the mention picker's avatar. */
export function mentionInitials(name: string): string {
  const words = name.trim().split(/\s+/).filter(Boolean);
  return (words.length > 1 ? [words[0], words.at(-1)!] : words).map(word => Array.from(word)[0]).join("").toLocaleUpperCase();
}

/** The palette's size, as in Signal: 36 hues (organization.css). */
export const TONES = 36;
type Speaker = { author_id: string; author_kind?: string; assistant_model?: string | null };

/** Whose color a message takes: a model's replies are colored by the model, not the requester. */
export const speakerKey = (message: Speaker) => message.author_kind === "assistant" ? "model:" + (message.assistant_model ?? "") : message.author_id;

/**
 * Colors for one chat, as in Signal's groups: its participants, sorted by id, take the palette in order, so everyone in a
 * chat has a different color until the palette runs out. A person's color can therefore differ between chats. Current
 * participants come first, so their colors do not move when older history loads; anyone else who spoke there (a former
 * member, a model) follows in the same way.
 */
export function chatTones(participants: Iterable<string>, speakers: Iterable<string> = []): Map<string, number> {
  const tones = new Map<string, number>();
  const assign = (ids: Iterable<string>) => [...new Set(ids)].filter(id => !tones.has(id)).sort()
    .forEach(id => tones.set(id, tones.size % TONES));
  assign(participants);
  assign(speakers);
  return tones;
}

/** The class giving a person, bot or model its chat color: their dot, guide line, name and picker avatar. */
export const toneClass = (tones: Map<string, number>, id: string) => "org-person-tone-" + (tones.get(id) ?? 0);
export const speakerTone = (message: Speaker, tones: Map<string, number>) => toneClass(tones, speakerKey(message));

/**
 * Paints tints behind bound mentions in the composer. A textarea cannot style parts of its text, so this mirror
 * repeats the text invisibly with the same metrics and marks each mention's range; the textarea sits on top.
 */
export const MentionMirror = forwardRef<HTMLDivElement, { text: string; bindings?: MentionBinding[]; self: string }>(
  function MentionMirror({ text, bindings = [], self }, ref) {
    const parts: React.ReactNode[] = [];
    let position = 0;
    for (const binding of [...bindings].filter(item => !item.wire && item.end <= text.length).sort((left, right) => left.start - right.start)) {
      if (binding.start < position) continue;
      parts.push(text.slice(position, binding.start));
      parts.push(<mark key={binding.start} className={"org-mention" + (binding.id === self ? " org-mention-self" : "")}>
        {text.slice(binding.start, binding.end)}</mark>);
      position = binding.end;
    }
    parts.push(text.slice(position) + "​");
    return <div ref={ref} className="org-composer-mirror" aria-hidden="true">{parts}</div>;
  });
