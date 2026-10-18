// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import React, { createContext, useContext, useEffect, useSyncExternalStore } from "react";
import { Hierarchy, KIND_SYMBOL, MentionQuery, TopicKind, organizationPath, request } from "./organization-api";

/**
 * Links to messages, and references to topics and projects written in Markdown.
 *
 * A message stores a reference as `#[topic:ID]` or `#[project:ID]`, never the target's name. Readers learn
 * the current name only from the server's access-checked `/references` endpoint; a target the reader cannot
 * open renders as the neutral placeholder "unavailable".
 */
export type ReferenceKind = "topic" | "project";
export const REFERENCE_PATTERN = /#\[(topic|project):([a-f0-9]{32})\]/g;
/** The server resolves at most this many references in one request. */
export const REFERENCE_BATCH = 100;
export const referenceToken = (kind: ReferenceKind, id: string) => `#[${kind}:${id}]`;

export type ResolvedTopic = { id: string; name: string; kind: TopicKind; channel_id: string; channel_name: string;
  project_id: string; project_name: string };
export type ResolvedProject = { id: string; name: string };
export type ReferenceResolution = { topics: Record<string, ResolvedTopic | null>; projects: Record<string, ResolvedProject | null> };

/** A same-tab application URL, for example `?organization=O&topic=T&message=M`. */
export const appLink = (params: Record<string, string>) => "/organization.html?" + new URLSearchParams(params);
export const messageLink = (origin: string, organizationId: string, topicId: string, messageId: string) =>
  origin + appLink({ organization: organizationId, topic: topicId, message: messageId });
export const topicLink = (origin: string, organizationId: string, topicId: string) =>
  origin + appLink({ organization: organizationId, topic: topicId });
export const referenceHref = (organizationId: string, kind: ReferenceKind, id: string) =>
  appLink({ organization: organizationId, [kind]: id });

/** Whether `href` opens this application, so it can navigate in place rather than in a new tab. */
export function applicationHref(href: string | undefined): boolean {
  if (!href || typeof window === "undefined") return false;
  try {
    const url = new URL(href, window.location.href);
    return url.origin === window.location.origin && url.pathname === "/organization.html";
  } catch { return false; }
}

/** Follow an application link in the current tab; modified clicks keep the browser's default behavior. */
export function followApplicationLink(event: React.MouseEvent<HTMLAnchorElement>): void {
  if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  window.history.pushState(null, "", event.currentTarget.href);
  window.dispatchEvent(new PopStateEvent("popstate"));
}

export const referenceLabel = (kind: ReferenceKind, target: ResolvedTopic | ResolvedProject) =>
  kind === "topic" ? `${KIND_SYMBOL[(target as ResolvedTopic).kind] ?? ""}-${target.name}` : target.name;

/** The active `#` fragment before the caret, as for `@` mentions: at a word boundary and without spaces. */
export function referenceQuery(content: string, cursor: number): MentionQuery | null {
  if (!Number.isInteger(cursor) || cursor < 0 || cursor > content.length) return null;
  const match = /(?:^|[\s([{])#([^\s#[\]()]*)$/u.exec(content.slice(0, cursor));
  return match ? { start: cursor - match[1].length - 1, end: cursor, query: match[1] } : null;
}

export type ReferenceCandidate = { kind: ReferenceKind; id: string; name: string; label: string; detail: string };
/** Topics and projects of the writer's own hierarchy, which the server already limits to what they can open. */
export function referenceCandidates(hierarchy: Hierarchy): ReferenceCandidate[] {
  const projects = new Map(hierarchy.projects.map(project => [project.id, project]));
  const channels = new Map(hierarchy.channels.map(channel => [channel.id, channel]));
  return [...hierarchy.topics.flatMap(topic => {
    const channel = channels.get(topic.channel_id), project = channel && projects.get(channel.project_id);
    return channel && project ? [{ kind: "topic" as const, id: topic.id, name: topic.name, label: `${KIND_SYMBOL[topic.kind]}-${topic.name}`,
      detail: `${project.name} / #${channel.name}` }] : [];
  }), ...hierarchy.projects.map(project => ({ kind: "project" as const, id: project.id, name: project.name, label: project.name, detail: "Project" }))];
}
/** Candidates whose name contains `query`, names that start with it first, at most `limit`. */
export function matchReferences(candidates: ReferenceCandidate[], query: string, limit = 20): ReferenceCandidate[] {
  const needle = query.toLocaleLowerCase();
  const matching = candidates.filter(candidate => candidate.name.toLocaleLowerCase().includes(needle));
  return [...matching.filter(candidate => candidate.name.toLocaleLowerCase().startsWith(needle)),
    ...matching.filter(candidate => !candidate.name.toLocaleLowerCase().startsWith(needle))].slice(0, limit);
}

type MarkdownNode = { type: string; value?: string; url?: string; children?: MarkdownNode[] };
const REFERENCE_URL = /^#reference\/(topic|project)\/([a-f0-9]{32})$/;
/** The reference a Markdown link produced by `remarkReferences` points to. */
export function referenceFromHref(href: string | undefined): { kind: ReferenceKind; id: string } | null {
  const match = href ? REFERENCE_URL.exec(href) : null;
  return match ? { kind: match[1] as ReferenceKind, id: match[2] } : null;
}
function splitReferences(value: string): MarkdownNode[] {
  const nodes: MarkdownNode[] = [];
  let position = 0;
  for (const match of value.matchAll(REFERENCE_PATTERN)) {
    if (match.index! > position) nodes.push({ type: "text", value: value.slice(position, match.index) });
    nodes.push({ type: "link", url: `#reference/${match[1]}/${match[2]}`, children: [{ type: "text", value: match[0] }] });
    position = match.index! + match[0].length;
  }
  if (position < value.length) nodes.push({ type: "text", value: value.slice(position) });
  return nodes;
}
function linkReferences(node: MarkdownNode): void {
  if (!node.children) return;
  node.children = node.children.flatMap(child => {
    if (child.type === "text" && child.value?.includes("#[")) return splitReferences(child.value);
    // Links cannot nest, and code and math are not text nodes.
    if (child.type !== "link" && child.type !== "linkReference") linkReferences(child);
    return [child];
  });
}
/** Remark plugin: reference tokens in Markdown text become links rendered by `ReferenceLink`. */
export function remarkReferences() { return (tree: MarkdownNode) => linkReferences(tree); }

type Entry = { value?: ResolvedTopic | ResolvedProject | null; stale: boolean; loading: boolean };
/**
 * Batches and caches reference resolution for one organization. Each mounted reference watches its target;
 * `invalidate` (after a hierarchy change such as a rename or a membership change) resolves watched targets
 * again. Failed requests retry after a delay.
 */
export class ReferenceResolver {
  private entries = new Map<string, Entry>();
  private watched = new Map<string, number>();
  private queue = new Set<string>();
  private listeners = new Set<() => void>();
  private scheduled = false;
  private version = 0;
  constructor(readonly organizationId: string,
    private fetcher: (path: string) => Promise<ReferenceResolution> = path => request<ReferenceResolution>(path),
    private retryMs = 15000) {}
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  getVersion = () => this.version;
  entry(kind: ReferenceKind, id: string): Entry | undefined { return this.entries.get(kind + ":" + id); }
  /** A plain-text label for copying and editing: `#name`, `#unavailable`, or `#reference` before resolution. */
  text(kind: ReferenceKind, id: string): string {
    const value = this.entry(kind, id)?.value;
    return "#" + (value ? referenceLabel(kind, value) : value === null ? "unavailable" : "reference");
  }
  watch(kind: ReferenceKind, id: string): () => void {
    const key = kind + ":" + id;
    this.watched.set(key, (this.watched.get(key) ?? 0) + 1);
    this.want(key);
    return () => {
      const count = (this.watched.get(key) ?? 1) - 1;
      if (count > 0) this.watched.set(key, count); else this.watched.delete(key);
    };
  }
  invalidate(): void {
    for (const entry of this.entries.values()) entry.stale = true;
    for (const key of this.watched.keys()) this.want(key);
  }
  private changed(): void { this.version++; for (const listener of [...this.listeners]) listener(); }
  private want(key: string): void {
    const entry = this.entries.get(key);
    if (entry && (!entry.stale || entry.loading)) return;
    this.queue.add(key);
    if (this.scheduled) return;
    this.scheduled = true;
    setTimeout(() => void this.flush(), 0);
  }
  private async flush(): Promise<void> {
    this.scheduled = false;
    const keys = [...this.queue]; this.queue.clear();
    await Promise.all(Array.from({ length: Math.ceil(keys.length / REFERENCE_BATCH) }, (_, index) =>
      this.resolveBatch(keys.slice(index * REFERENCE_BATCH, (index + 1) * REFERENCE_BATCH))));
  }
  private async resolveBatch(keys: string[]): Promise<void> {
    for (const key of keys) this.entries.set(key, { ...this.entries.get(key), stale: true, loading: true });
    const ids = (kind: ReferenceKind) => keys.filter(key => key.startsWith(kind + ":")).map(key => key.slice(kind.length + 1));
    const topics = ids("topic"), projects = ids("project"), query = new URLSearchParams();
    if (topics.length) query.set("topics", topics.join(","));
    if (projects.length) query.set("projects", projects.join(","));
    try {
      const result = await this.fetcher(organizationPath(this.organizationId) + "/references?" + query);
      for (const id of topics) this.entries.set("topic:" + id, { value: result.topics?.[id] ?? null, stale: false, loading: false });
      for (const id of projects) this.entries.set("project:" + id, { value: result.projects?.[id] ?? null, stale: false, loading: false });
    } catch {
      for (const key of keys) this.entries.set(key, { ...this.entries.get(key), stale: true, loading: false });
      setTimeout(() => { for (const key of keys) if (this.watched.has(key)) this.want(key); }, this.retryMs);
    }
    this.changed();
  }
}

export const ReferenceContext = createContext<ReferenceResolver | null>(null);
const unsubscribed = () => () => {};
const initialVersion = () => 0;
function useReference(kind: ReferenceKind, id: string) {
  const resolver = useContext(ReferenceContext);
  useSyncExternalStore(resolver?.subscribe ?? unsubscribed, resolver?.getVersion ?? initialVersion, initialVersion);
  useEffect(() => resolver?.watch(kind, id), [resolver, kind, id]);
  return { resolver, value: resolver?.entry(kind, id)?.value };
}

/** A reference rendered as a link with the target's current name, or a neutral placeholder. */
export function ReferenceLink({ kind, id }: { kind: ReferenceKind; id: string }) {
  const { resolver, value } = useReference(kind, id);
  if (resolver && value === undefined) return <span className="org-reference org-reference-pending" aria-label="Loading reference">…</span>;
  if (!resolver || !value) return <span className="org-reference org-reference-unavailable" title="This reference is unavailable">unavailable</span>;
  const label = referenceLabel(kind, value);
  const topic = kind === "topic" ? value as ResolvedTopic : null;
  return <a className="org-reference" href={referenceHref(resolver.organizationId, kind, id)} onClick={followApplicationLink}
    title={topic ? `${topic.project_name} / #${topic.channel_name} / ${label}` : `Project ${label}`}>{label}</a>;
}

/** The current name of a reference as text, for places that cannot contain links. */
export function ReferenceName({ kind, id }: { kind: ReferenceKind; id: string }) {
  const { value } = useReference(kind, id);
  return <span className="org-reference-name">{value ? "#" + referenceLabel(kind, value) : value === null ? "#unavailable" : "#…"}</span>;
}

/** Plain text with each reference token replaced by its current name. */
export function ReferenceText({ text }: { text: string }) {
  const parts: React.ReactNode[] = [];
  let position = 0;
  for (const match of text.matchAll(REFERENCE_PATTERN)) {
    if (match.index! > position) parts.push(text.slice(position, match.index));
    parts.push(<ReferenceName key={match.index} kind={match[1] as ReferenceKind} id={match[2]} />);
    position = match.index! + match[0].length;
  }
  parts.push(text.slice(position));
  return <>{parts}</>;
}

/** A Markdown anchor for knowledge pages: references render with `ReferenceLink`; other links stay as written. */
export function MarkdownReferenceAnchor({ href, title, children }: { href?: string; title?: string; children?: React.ReactNode }) {
  const reference = referenceFromHref(href);
  return reference ? <ReferenceLink kind={reference.kind} id={reference.id} /> : <a href={href} title={title}>{children}</a>;
}
