import React, { useEffect, useRef, useState } from "react";
import { ActivityDialog } from "./TopicActivity";
import "./topic-commands.css";

export const TOPIC_COMMANDS = [
  { id: "attach", label: "Attach a picture or PDF", kinds: ["alpha", "beta", "gamma"] },
  { id: "play", label: "Start or resume", kinds: ["beta", "gamma"] },
  { id: "stop", label: "Stop current work", kinds: ["beta", "gamma"] },
  { id: "goal", label: "Set or inspect a goal", kinds: ["beta"] },
  { id: "ledger", label: "Shared ledger", kinds: ["beta", "gamma"] },
  { id: "files", label: "Topic files", kinds: ["beta", "gamma"] },
  { id: "context", label: "Inspect research context", kinds: ["beta"] },
  { id: "tools", label: "Research settings", kinds: ["beta"] },
  { id: "proposals", label: "Review proposed actions", kinds: ["beta"] },
  { id: "reviews", label: "Independent reviews", kinds: ["beta"] },
  { id: "compare", label: "Compare model answers", kinds: ["gamma"] },
  { id: "workflow", label: "Build a workflow", kinds: ["gamma"] },
  { id: "json", label: "Workflow definition", kinds: ["gamma"] },
  { id: "compute", label: "Run a computation", kinds: ["beta", "gamma"] },
  { id: "uncap", label: "Edit an uncapped copy of the latest workflow", kinds: ["gamma"] },
  { id: "tex", label: "Compile a PDF from TeX", kinds: ["beta", "gamma"] },
] as const;
export type TopicCommand = typeof TOPIC_COMMANDS[number]["id"];
export function dispatchTopicCommand(topicId: string, command: TopicCommand) {
  window.dispatchEvent(new CustomEvent("reasoning-topic-command", { detail: { topicId, command } }));
}
export function useTopicCommand(topicId: string, onCommand: (command: TopicCommand) => void) {
  const handler = useRef(onCommand); handler.current = onCommand;
  useEffect(() => {
    const receive = (event: Event) => {
      const detail = (event as CustomEvent).detail;
      if (detail?.topicId === topicId && TOPIC_COMMANDS.some(item => item.id === detail.command)) handler.current(detail.command);
    };
    window.addEventListener("reasoning-topic-command", receive);
    return () => window.removeEventListener("reasoning-topic-command", receive);
  }, [topicId]);
}
export function publishComputeActivity(topicId: string, active: boolean) {
  window.dispatchEvent(new CustomEvent("reasoning-topic-compute", { detail: { topicId, active } }));
}
export function useComputeActivity(topicId: string) {
  const [active, setActive] = useState(false);
  useEffect(() => {
    setActive(false);
    const receive = (event: Event) => {
      const detail = (event as CustomEvent).detail;
      if (detail?.topicId === topicId) setActive(Boolean(detail.active));
    };
    window.addEventListener("reasoning-topic-compute", receive);
    return () => window.removeEventListener("reasoning-topic-compute", receive);
  }, [topicId]);
  return active;
}
export function SlashCommandPalette({ kind, text, onSelect }: {
  topicId?: string; kind: string; text: string; onSelect: (command: TopicCommand) => void;
}) {
  const [index, setIndex] = useState(0), [dismissed, setDismissed] = useState(false), root = useRef<HTMLDivElement>(null);
  const query = /^\/([a-z]*)$/i.exec(text)?.[1];
  const choices = query === undefined ? [] : TOPIC_COMMANDS.filter(command => (command.kinds as readonly string[]).includes(kind) && command.id.startsWith(query.toLowerCase()));
  useEffect(() => { setIndex(0); setDismissed(false); }, [query]);
  useEffect(() => {
    if (!choices.length || dismissed) return;
    const keyboard = (event: KeyboardEvent) => {
      if (event.isComposing || !((event.target instanceof HTMLTextAreaElement && event.target.id === "org-message") || root.current?.contains(event.target as Node))) return;
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); setDismissed(true); return; }
      if (["ArrowDown", "ArrowUp", "Tab", "Enter"].includes(event.key) && !event.shiftKey && !event.ctrlKey && !event.metaKey) {
        event.preventDefault(); event.stopPropagation(); event.stopImmediatePropagation();
        if (event.key === "Enter" || event.key === "Tab") onSelect(choices[index % choices.length].id);
        else setIndex(current => (current + (event.key === "ArrowDown" ? 1 : -1) + choices.length) % choices.length);
      }
    };
    window.addEventListener("keydown", keyboard, true);
    return () => window.removeEventListener("keydown", keyboard, true);
  }, [choices.map(item => item.id).join(","), index, onSelect, dismissed]);
  if (!choices.length || dismissed) return null;
  return <div ref={root} className="org-slash-palette" role="listbox" aria-label="Topic commands">
    {choices.map((command, position) => <button key={command.id} type="button" role="option" aria-selected={position === index}
      onMouseDown={event => event.preventDefault()} onClick={() => onSelect(command.id)}><code>/{command.id}</code><span>{command.label}</span></button>)}
  </div>;
}
export function ShortcutsHelp({ onClose }: { onClose: () => void }) {
  return <ActivityDialog title="Keyboard shortcuts and commands" onClose={onClose}>
    <dl className="org-shortcut-list"><dt>?</dt><dd>Toggle help outside text fields</dd><dt>Enter</dt><dd>Send a message</dd>
      <dt>Shift+Enter</dt><dd>New line</dd><dt>Ctrl/⌘+K</dt><dd>Search organization messages</dd>
      <dt>Ctrl/⌘+B</dt><dd>Toggle project navigation</dd>
      <dt>Ctrl/⌘+Shift+P</dt><dd>Toggle people and bots</dd>
      <dt>@</dt><dd>Mention a person or bot</dd><dt>#</dt><dd>Reference a topic or project</dd><dt>/</dt><dd>Open topic commands</dd>
      <dt>↑ / ↓ · Enter / Tab</dt><dd>Choose an autocomplete option</dd><dt>Escape</dt><dd>Close dialogs and menus</dd></dl>
    <h3>Topic commands</h3><dl className="org-shortcut-list">{TOPIC_COMMANDS.map(command => <React.Fragment key={command.id}>
      <dt><code>/{command.id}</code></dt><dd>{command.label} <small>· {command.kinds.join(" / ")}</small></dd></React.Fragment>)}</dl>
  </ActivityDialog>;
}
export function TransportControls({ canPlay, canStop, onPlay, onStop, hidePlay = false, playLabel = "Start or resume", stopLabel = "Stop current work", playTitle }: {
  canPlay: boolean; canStop: boolean; onPlay: () => void; onStop?: () => void; hidePlay?: boolean; playLabel?: string; stopLabel?: string;
  playTitle?: string;
}) {
  return <span className="org-transport-controls" aria-label="Topic playback">
    {!hidePlay && <button type="button" aria-label={playLabel} title={playTitle ?? playLabel} disabled={!canPlay} onClick={onPlay}>▶</button>}
    {onStop && <button type="button" aria-label={stopLabel} title={stopLabel} disabled={!canStop} onClick={onStop}>■</button>}
  </span>;
}
