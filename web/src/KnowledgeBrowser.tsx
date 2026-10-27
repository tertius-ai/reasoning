import React, { useEffect, useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import {
  ApiError, Hierarchy, KnowledgeEntry, KnowledgeKind, aborted, errorMessage, organizationPath, request,
} from "./organization-api";
import { normalizeMathDelimiters, rehypeMath } from "./markdownMath";
import { MathematicsBrowser, ResearchMarkdown } from "./MathematicsBrowser";
import { MarkdownReferenceAnchor, remarkReferences } from "./references";
import { ResearchBibliography } from "./ResearchBibliography";

const LABELS: Record<KnowledgeKind, string> = { ledger: "Ledger", bibliography: "Bibliography", journal: "Research blog" };
const STATES: Record<KnowledgeKind, string[]> = {
  ledger: ["note", "conjecture", "established", "refuted"],
  bibliography: ["active", "archived"],
  journal: ["draft", "published"],
};

function scopeLabel(entry: KnowledgeEntry, hierarchy: Hierarchy): string {
  if (entry.scope_kind === "organization") return "Entire organization";
  const project = hierarchy.projects.find((item) => item.id === entry.project_id);
  if (entry.scope_kind === "project") return project?.name ?? "Project";
  const topic = hierarchy.topics.find((item) => item.id === entry.topic_id);
  const channel = hierarchy.channels.find((item) => item.id === topic?.channel_id);
  return `${project?.name ?? "Project"} / #${channel?.name ?? "channel"} / ${topic?.name ?? "topic"}`;
}

export function KnowledgeBrowser({ organizationId, hierarchy, canWrite, onAuthLost, initialKind = "journal", notesOnly = false }: {
  organizationId: string; hierarchy: Hierarchy; canWrite: boolean; onAuthLost: () => void; initialKind?: KnowledgeKind; notesOnly?: boolean;
}) {
  const [kind, setKind] = useState<KnowledgeKind>(initialKind);
  const [entries, setEntries] = useState<KnowledgeEntry[]>([]);
  const [selected, setSelected] = useState<KnowledgeEntry | null>(null);
  const [query, setQuery] = useState("");
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [state, setState] = useState("draft");
  const [scopeKind, setScopeKind] = useState<"organization" | "project" | "topic">("organization");
  const [scopeId, setScopeId] = useState("");
  const [busy, setBusy] = useState(false);
  const [clientId, setClientId] = useState(() => crypto.randomUUID());
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [preview, setPreview] = useState(false);
  const root = organizationPath(organizationId);
  const scopeOptions = useMemo(() => scopeKind === "project"
    ? hierarchy.projects.map((item) => ({ id: item.id, label: item.name }))
    : hierarchy.topics.map((topic) => {
      const channel = hierarchy.channels.find((item) => item.id === topic.channel_id);
      const project = hierarchy.projects.find((item) => item.id === channel?.project_id);
      return { id: topic.id, label: `${project?.name ?? "Project"} / #${channel?.name ?? "channel"} / ${topic.name}` };
    }), [hierarchy, scopeKind]);

  useEffect(() => {
    const controller = new AbortController(); setError("");
    const suffix = `?kind=${kind}&q=${encodeURIComponent(query)}`;
    void request<KnowledgeEntry[]>(root + "/knowledge" + suffix, { signal: controller.signal })
      .then((rows) => {
        if (controller.signal.aborted) return;
        setEntries(rows);
        setSelected((current) => current ? rows.find((item) => item.id === current.id) ?? rows[0] ?? null : rows[0] ?? null);
      }).catch((cause) => {
        if (aborted(cause)) return;
        if (cause instanceof ApiError && cause.status === 401) onAuthLost(); else setError(errorMessage(cause));
      });
    return () => controller.abort();
  }, [root, kind, query, refresh, onAuthLost]);

  const startNew = () => {
    setSelected(null); setEditing(true); setTitle(""); setBody(""); setState(STATES[kind][0]); setScopeKind("organization"); setScopeId("");
    setClientId(crypto.randomUUID()); setError("");
  };
  const startEdit = () => {
    if (!selected) return;
    setEditing(true); setTitle(selected.title); setBody(selected.body); setState(selected.state); setClientId(crypto.randomUUID()); setError("");
  };
  const submit = async (event: React.FormEvent) => {
    event.preventDefault(); setBusy(true); setError("");
    try {
      const value = await request<KnowledgeEntry>(root + "/knowledge" + (selected ? "/" + selected.id : ""), {
        method: selected ? "PUT" : "POST",
        body: {
          client_id: clientId, kind, title, body, state,
          scope_kind: selected ? null : scopeKind,
          scope_id: selected || scopeKind === "organization" ? null : scopeId,
          expected_revision: selected?.revision ?? null,
        },
      });
      setSelected(value); setEditing(false); setRefresh((number) => number + 1);
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 401) onAuthLost(); else setError(errorMessage(cause));
    } finally { setBusy(false); }
  };
  const openRevision = async (revision: number) => {
    if (!selected) return;
    setBusy(true); setError("");
    try { setSelected(await request<KnowledgeEntry>(root + `/knowledge/${selected.id}?revision=${revision}`)); }
    catch (cause) { setError(errorMessage(cause)); } finally { setBusy(false); }
  };

  return <section className="org-knowledge" aria-label="Organization knowledge">
    <header><div><h3>Shared knowledge</h3><p>Organization-wide records with project and topic views.</p></div>
      {canWrite && kind === "journal" && <button className="org-primary" onClick={startNew}>New post</button>}</header>
    {!notesOnly && <nav aria-label="Knowledge collections">{(Object.keys(LABELS) as KnowledgeKind[]).map((value) =>
      <button key={value} aria-pressed={kind === value} onClick={() => { setKind(value); setSelected(null); setEditing(false); setState(STATES[value][0]); }}>{LABELS[value]}</button>)}</nav>}
    {!notesOnly && kind === "ledger" && <MathematicsBrowser organizationId={organizationId} canWrite={canWrite} onAuthLost={onAuthLost} />}
    {!notesOnly && kind === "bibliography" && <ResearchBibliography organizationId={organizationId} onAuthLost={onAuthLost} />}
    <details key={kind} open={kind === "journal"}>
    {kind !== "journal" && <summary>{kind === "ledger" ? "Notes and imported source records" : "Bibliography notes"}</summary>}
    {canWrite && kind !== "journal" && <button onClick={startNew}>New {kind === "ledger" ? "note" : "bibliography note"}</button>}
    <label className="org-knowledge-search"><span className="org-visually-hidden">Search {LABELS[kind]}</span>
      <input type="search" value={query} placeholder={`Search ${LABELS[kind].toLowerCase()}`} onChange={(event) => setQuery(event.target.value)} /></label>
    {error && <p role="alert" className="org-error">{error}</p>}
    <div className="org-knowledge-body">
      <aside aria-label={LABELS[kind] + " entries"}>{entries.map((entry) => <button key={entry.id} aria-current={selected?.id === entry.id ? "true" : undefined}
        onClick={() => { setSelected(entry); setEditing(false); }}><strong>{entry.title}</strong><small>{entry.state} · {scopeLabel(entry, hierarchy)}</small></button>)}
        {!entries.length && <p>No entries yet.</p>}</aside>
      <article>
        {editing ? <form onSubmit={(event) => void submit(event)}>
          <label>Title<input required maxLength={300} value={title} onChange={(event) => setTitle(event.target.value)} /></label>
          {!selected && <><label>Scope<select value={scopeKind} onChange={(event) => { const value = event.target.value as typeof scopeKind; setScopeKind(value); setScopeId(""); }}>
            <option value="organization">Entire organization</option><option value="project">One project</option><option value="topic">One topic</option>
          </select></label>{scopeKind !== "organization" && <label>{scopeKind === "project" ? "Project" : "Topic"}<select required value={scopeId} onChange={(event) => setScopeId(event.target.value)}>
            <option value="">Choose…</option>{scopeOptions.map((option) => <option key={option.id} value={option.id}>{option.label}</option>)}</select></label>}</>}
          <label>Status<select value={state} onChange={(event) => setState(event.target.value)}>{STATES[kind].map((value) => <option key={value}>{value}</option>)}</select></label>
          <label>Markdown<textarea required rows={16} maxLength={64000} value={body} onChange={(event) => setBody(event.target.value)} /></label>
          {kind === "journal" && <><button type="button" aria-expanded={preview} onClick={() => setPreview(!preview)}>Preview</button>
            {preview && <div className="org-knowledge-markdown"><ResearchMarkdown>{body}</ResearchMarkdown></div>}
            <p className="org-help">Private to the selected organization, project, or topic audience. Never a public website.</p></>}
          <div><button className="org-primary" disabled={busy}>{busy ? "Saving…" : selected ? "Save revision" : kind === "journal" && state === "draft" ? "Save draft" : "Publish entry"}</button>
            <button type="button" disabled={busy} onClick={() => setEditing(false)}>Cancel</button></div>
        </form> : selected ? <>
          <header><div><small>{selected.state} · {scopeLabel(selected, hierarchy)}</small><h2>{selected.title}</h2>
            <p>Revision {selected.revision} · {selected.author_name} · {new Date(selected.revised_at * 1000).toLocaleString()}</p></div>
            {canWrite && selected.revision === selected.head_revision && <button onClick={startEdit}>Edit</button>}</header>
          <div className="org-knowledge-markdown"><ReactMarkdown remarkPlugins={[remarkGfm, remarkMath, remarkReferences]} rehypePlugins={[rehypeMath]} skipHtml
            components={{ a: MarkdownReferenceAnchor }}>{normalizeMathDelimiters(selected.body)}</ReactMarkdown></div>
          {selected.head_revision > 1 && <nav aria-label="Revision history">{Array.from({ length: Math.min(selected.head_revision, 10) }, (_, index) => selected.head_revision - index)
            .map((revision) => <button key={revision} aria-current={revision === selected.revision ? "true" : undefined}
              disabled={busy} onClick={() => void openRevision(revision)}>v{revision}</button>)}</nav>}
        </> : <div className="org-empty"><h2>{LABELS[kind]}</h2><p>Select an entry or create the first one.</p></div>}
      </article>
    </div>
    </details>
  </section>;
}
