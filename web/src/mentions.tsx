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

const TONES = 6;
/** A stable avatar color per member, so the same person always looks the same in the picker. */
export function mentionTone(id: string): number {
  let hash = 0;
  for (const character of id) hash = (hash * 31 + character.charCodeAt(0)) >>> 0;
  return hash % TONES;
}

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
