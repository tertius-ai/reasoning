// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
type Binding = { start: number; end: number; label: string; wire: string };
export type MessageEditDraft = { text: string; bindings: Binding[] };

/** Readable text for editing or copying; `reference` labels `#[topic:ID]` and `#[project:ID]` tokens. */
export function readableMessageEdit(content: string, people: { id: string; name: string }[],
  reference: (kind: "topic" | "project", id: string) => string = () => "#reference"): MessageEditDraft {
  const names = new Map(people.map(person => [person.id, person.name]));
  const bindings: Binding[] = [];
  let text = "", end = 0;
  for (const match of content.matchAll(/@\[([a-f0-9]{32})(?::([^\]]+))?\]|#\[(topic|project):([a-f0-9]{32})\]/g)) {
    text += content.slice(end, match.index);
    const label = match[3] ? reference(match[3] as "topic" | "project", match[4])
      : "@" + (names.get(match[1]) ?? "departed member") + (match[2] ? "-" + match[2] : "");
    bindings.push({ start: text.length, end: text.length + label.length, label, wire: match[0] });
    text += label; end = match.index + match[0].length;
  }
  return { text: text + content.slice(end), bindings };
}

export function updateMessageEdit(before: MessageEditDraft, text: string): MessageEditDraft {
  let start = 0, end = before.text.length, nextEnd = text.length;
  while (start < end && start < nextEnd && before.text[start] === text[start]) start++;
  while (end > start && nextEnd > start && before.text[end - 1] === text[nextEnd - 1]) { end--; nextEnd--; }
  const shift = nextEnd - end;
  return { text, bindings: before.bindings.flatMap(binding => binding.end <= start ? [binding]
    : binding.start >= end ? [{ ...binding, start: binding.start + shift, end: binding.end + shift }] : []) };
}

export function encodeMessageEdit(draft: MessageEditDraft): string {
  let content = draft.text;
  for (const binding of [...draft.bindings].sort((a, b) => b.start - a.start)) {
    if (draft.text.slice(binding.start, binding.end) === binding.label)
      content = content.slice(0, binding.start) + binding.wire + content.slice(binding.end);
  }
  return content;
}
