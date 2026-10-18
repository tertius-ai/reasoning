// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
/** A readable composer label bound to its encoded form; `wire` carries a `#[topic:ID]` or `#[project:ID]` reference. */
export type MentionBinding = { start: number; end: number; id: string; label: string; model?: string; effort?: string; wire?: string };
export function editMentions(before: string, after: string, bindings: MentionBinding[] = []): MentionBinding[] {
  let start = 0;
  while (start < before.length && start < after.length && before[start] === after[start]) start++;
  let end = before.length, nextEnd = after.length;
  while (end > start && nextEnd > start && before[end - 1] === after[nextEnd - 1]) { end--; nextEnd--; }
  const shift = nextEnd - end;
  return bindings.flatMap(binding => binding.end <= start ? [binding] : binding.start >= end
    ? [{ ...binding, start: binding.start + shift, end: binding.end + shift }] : []);
}
export function encodeMentions(text: string, bindings: MentionBinding[] = []): string {
  let result = text;
  for (const binding of [...bindings].sort((a, b) => b.start - a.start)) {
    if (binding.wire !== undefined) {
      if (/^#\[(topic|project):[a-f0-9]{32}\]$/.test(binding.wire) && text.slice(binding.start, binding.end) === binding.label)
        result = result.slice(0, binding.start) + binding.wire + result.slice(binding.end);
    } else if (/^[a-f0-9]{32}$/.test(binding.id) && text.slice(binding.start, binding.end) === binding.label) {
      const model = binding.model && /^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$/.test(binding.model) ? ":" + binding.model : "";
      const effort = model && binding.effort && /^(low|medium|high|xhigh|max)$/.test(binding.effort) ? "!" + binding.effort : "";
      result = result.slice(0, binding.start) + `@[${binding.id}${model}${effort}]` + result.slice(binding.end);
    }
  }
  return result;
}
