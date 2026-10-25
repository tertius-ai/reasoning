import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { PlusIcon } from "./ComposerIcons";
import {
  BibliographyDraft,
  BibliographyConnection,
  BibliographyItem,
  BibliographyResource,
  BibliographySummary,
  ProjectInfo,
  addBibliographySummary,
  attachBibliographyItem,
  bibliographyExportUrl,
  bibliographyResourceUrl,
  createBibliographyItem,
  deleteBibliographyItem,
  detachBibliographyItem,
  fetchBibliography,
  fetchBibliographyConnections,
  uploadBibliographyResource,
} from "./api";
import { Markdown } from "./Markdown";

export type BibliographyScope = "global" | "project" | "chat";

export interface BibliographyContext {
  scope: BibliographyScope;
  projectId: string | null;
  projectName: string | null;
  chatId: string | null;
  chatName: string | null;
}

const textResource = (resource: BibliographyResource) =>
  resource.media_type.startsWith("text/") ||
  /\.(md|markdown|txt|tex|log|json|jsonl|py|lean|toml|ya?ml|csv|bib)$/i.test(resource.name);

const markdownResource = (resource: BibliographyResource) =>
  resource.media_type === "text/markdown" || /\.(md|markdown)$/i.test(resource.name);

function formatBytes(value: number) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function sourceUrl(item: BibliographyItem) {
  if (item.url) return item.url;
  if (item.doi) return `https://doi.org/${item.doi}`;
  if (item.arxiv_id) return `https://arxiv.org/abs/${item.arxiv_id}`;
  return null;
}

function ResourceReader({ item, resource }: { item: BibliographyItem; resource: BibliographyResource }) {
  const url = bibliographyResourceUrl(item.bibliography_id, resource.resource_id);
  const isText = textResource(resource);
  const [text, setText] = useState<string | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    setText(null);
    setError("");
    if (!isText) return;
    let cancelled = false;
    fetch(url)
      .then((response) => {
        if (!response.ok) throw new Error(`Could not open document (HTTP ${response.status})`);
        return response.text();
      })
      .then((value) => { if (!cancelled) setText(value); })
      .catch((reason) => { if (!cancelled) setError(String(reason)); });
    return () => { cancelled = true; };
  }, [isText, url]);

  if (resource.media_type.startsWith("image/")) {
    return <img className="library-document-image" src={url} alt={resource.name} />;
  }
  if (resource.media_type === "application/pdf" || /\.pdf$/i.test(resource.name)) {
    return <iframe className="library-document-frame" src={url} title={resource.name} />;
  }
  if (!isText) {
    return <div className="library-document-empty">No inline reader for this format. <a href={url} target="_blank" rel="noreferrer">Open the document</a>.</div>;
  }
  if (error) return <div className="bibliography-error">{error}</div>;
  if (text === null) return <div className="library-document-empty">Reading document…</div>;
  return markdownResource(resource)
    ? <div className="library-document-markdown"><Markdown text={text} /></div>
    : <pre className="library-document-text">{text}</pre>;
}

function Identifiers({ item }: { item: BibliographyItem }) {
  return (
    <span className="bibliography-identifiers">
      {item.doi && <a href={`https://doi.org/${item.doi}`} target="_blank" rel="noreferrer">doi:{item.doi}</a>}
      {item.arxiv_id && <a href={`https://arxiv.org/abs/${item.arxiv_id}`} target="_blank" rel="noreferrer">arXiv:{item.arxiv_id}</a>}
      {!item.doi && !item.arxiv_id && item.url && <a href={item.url} target="_blank" rel="noreferrer">source</a>}
    </span>
  );
}

const summaryKindLabel: Record<BibliographySummary["kind"], string> = {
  overview: "overview",
  reading_note: "reading note",
  relevance: "relevance",
  critique: "critique",
};

const connectionGlyph: Record<BibliographyConnection["kind"], string> = {
  project: "▱",
  chat: "□",
  ledger_reference: "⌁",
  claim: "⊢",
  workflow: "◇",
  run: "▶",
  artifact: "▤",
};

function summaryProvenance(summary: BibliographySummary) {
  const model = typeof summary.provenance.model === "string" ? summary.provenance.model : null;
  const executor = typeof summary.provenance.executor === "string" ? summary.provenance.executor : null;
  const source = typeof summary.provenance.source === "string" ? summary.provenance.source : null;
  return [model, executor, source, new Date(summary.created_at).toLocaleDateString()].filter(Boolean).join(" · ");
}

function askDraft(item: BibliographyItem) {
  return `I want to ask about the Library source [${item.citation_key}], “${item.title}” (bibliography_id: ${item.bibliography_id}). Use bibliography_get to inspect its metadata, layered summaries, documents, and durable connections. Read relevant attached documents with bibliography_resource_read and inspect linked ledger claims or workflow artifacts when useful. Distinguish what the source establishes from our later interpretation, and cite exact page, theorem, section, or artifact locators when available.\n\nMy question: `;
}

function summaryDraft(item: BibliographyItem, scope: BibliographyScope) {
  return `Read and summarize the Library source [${item.citation_key}], “${item.title}” (bibliography_id: ${item.bibliography_id}). Use bibliography_get and bibliography_resource_read rather than relying only on metadata. Cover the main result, hypotheses, method, limitations, and relevance to the surrounding research; name exact locators for important claims. Compare existing layered notes instead of silently replacing them. When the account is ready, append it with bibliography_summary_add using kind=overview and scope=${scope}, including your executor, model, chat or run identifiers in provenance when known.`;
}

function workflowDraft(item: BibliographyItem) {
  return `Investigate Library source [${item.citation_key}], “${item.title}” (bibliography_id: ${item.bibliography_id}). Every research agent should begin with bibliography_get; agents that need the paper should read attached documents through bibliography_resource_read. Produce a saved report that separates the source's stated results, our derived claims, open questions, and precise locators. Preserve the bibliography_id in the final artifact so this workflow run remains connected to the source.`;
}

export function Bibliography({
  projectId,
  chatId,
  projects,
  onNavigateConnection,
  onDraftPrompt,
  onUseInWorkflow,
  onScopeChange,
  onBrowseArtifacts,
}: {
  projectId: string | null;
  chatId: string | null;
  projects: ProjectInfo[];
  onNavigateConnection?: (connection: BibliographyConnection) => void;
  onDraftPrompt?: (projectId: string, message: string) => void;
  onUseInWorkflow?: (projectId: string, item: BibliographyItem, initialPrompt: string) => void;
  onScopeChange?: (context: BibliographyContext) => void;
  onBrowseArtifacts?: (projectId: string) => void;
}) {
  const [scope, setScope] = useState<BibliographyScope>("global");
  const activeProjects = useMemo(
    () => projects.filter((candidate) => !candidate.archived),
    [projects],
  );
  const [scopeProjectId, setScopeProjectId] = useState(
    () => projectId ?? activeProjects[0]?.id ?? "",
  );
  const scopeProject = activeProjects.find((candidate) => candidate.id === scopeProjectId)
    ?? activeProjects[0]
    ?? null;
  const [scopeChatId, setScopeChatId] = useState(
    () => projectId === scopeProjectId ? chatId ?? "" : "",
  );
  const scopeChats = scopeProject?.chats.filter((candidate) => !candidate.archived) ?? [];
  const scopeChat = scopeChats.find((candidate) => candidate.id === scopeChatId)
    ?? scopeChats[0]
    ?? null;
  const [query, setQuery] = useState("");
  const [items, setItems] = useState<BibliographyItem[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [connections, setConnections] = useState<BibliographyConnection[]>([]);
  const [connectionsLoading, setConnectionsLoading] = useState(false);
  const [connectionsRefresh, setConnectionsRefresh] = useState(0);
  const [resourceId, setResourceId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [adding, setAdding] = useState(false);
  const [addingSummary, setAddingSummary] = useState(false);
  const [busy, setBusy] = useState(false);
  const [deleteArmed, setDeleteArmed] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [form, setForm] = useState({
    item_type: "article",
    title: "",
    authors: "",
    year: "",
    doi: "",
    arxiv_id: "",
    container_title: "",
    url: "",
    tags: "",
  });
  const [summaryForm, setSummaryForm] = useState<{
    kind: BibliographySummary["kind"];
    title: string;
    text: string;
  }>({ kind: "reading_note", title: "", text: "" });

  useEffect(() => {
    if (!scopeProject) {
      if (scope !== "global") setScope("global");
      return;
    }
    if (scopeProject.id !== scopeProjectId) setScopeProjectId(scopeProject.id);
    if (scope === "chat" && !scopeChat) setScope("project");
  }, [scope, scopeChat, scopeProject, scopeProjectId]);

  useEffect(() => onScopeChange?.({
    scope,
    projectId: scope === "global" ? null : scopeProject?.id ?? null,
    projectName: scope === "global" ? null : scopeProject?.name ?? null,
    chatId: scope === "chat" ? scopeChat?.id ?? null : null,
    chatName: scope === "chat" ? scopeChat?.title ?? null : null,
  }), [onScopeChange, scope, scopeChat?.id, scopeChat?.title, scopeProject?.id, scopeProject?.name]);

  const coordinates = scope === "global"
    ? { project: null, chat: null }
    : scope === "project"
      ? { project: scopeProject?.id ?? null, chat: null }
      : { project: scopeProject?.id ?? null, chat: scopeChat?.id ?? null };

  const load = useCallback(() => {
    setLoading(true);
    setError("");
    return fetchBibliography(coordinates.project, coordinates.chat, query)
      .then(({ items: loaded }) => {
        setItems(loaded);
        setSelectedId((current) => loaded.some((item) => item.bibliography_id === current)
          ? current
          : loaded[0]?.bibliography_id ?? null);
      })
      .catch((reason) => setError(String(reason)))
      .finally(() => setLoading(false));
  }, [coordinates.chat, coordinates.project, query]);

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 120);
    return () => window.clearTimeout(timer);
  }, [load]);

  const selected = useMemo(
    () => items.find((item) => item.bibliography_id === selectedId) ?? null,
    [items, selectedId],
  );
  const selectedResource = selected?.resources.find((resource) => resource.resource_id === resourceId) ?? null;

  useEffect(() => {
    setResourceId((current) => selected?.resources.some((resource) => resource.resource_id === current)
      ? current
      : selected?.resources[0]?.resource_id ?? null);
    setDeleteArmed(false);
  }, [selected]);

  useEffect(() => {
    setConnections([]);
    if (!selectedId) return;
    let cancelled = false;
    setConnectionsLoading(true);
    void fetchBibliographyConnections(selectedId)
      .then(({ connections: loaded }) => { if (!cancelled) setConnections(loaded); })
      .catch((reason) => { if (!cancelled) setError(String(reason)); })
      .finally(() => { if (!cancelled) setConnectionsLoading(false); });
    return () => { cancelled = true; };
  }, [connectionsRefresh, selectedId]);

  const attach = (item: BibliographyItem, target: "project" | "chat") => {
    if (!scopeProject || (target === "chat" && !scopeChat)) return;
    setBusy(true);
    setError("");
    void attachBibliographyItem(item.bibliography_id, scopeProject.id, target === "chat" ? scopeChat?.id ?? null : null)
      .then(() => { setConnectionsRefresh((value) => value + 1); return load(); })
      .catch((reason) => setError(String(reason)))
      .finally(() => setBusy(false));
  };

  const detach = (item: BibliographyItem, target: "project" | "chat") => {
    if (!scopeProject || (target === "chat" && !scopeChat)) return;
    setBusy(true);
    setError("");
    void detachBibliographyItem(item.bibliography_id, scopeProject.id, target === "chat" ? scopeChat?.id ?? null : null)
      .then(() => { setConnectionsRefresh((value) => value + 1); return load(); })
      .catch((reason) => setError(String(reason)))
      .finally(() => setBusy(false));
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const authors = form.authors.split(/\n|;/).map((value) => value.trim()).filter(Boolean);
    if (!form.title.trim() || authors.length === 0) return;
    const draft: BibliographyDraft = {
      item_type: form.item_type,
      title: form.title.trim(),
      authors,
      year: form.year ? Number(form.year) : null,
      doi: form.doi.trim() || null,
      arxiv_id: form.arxiv_id.trim() || null,
      container_title: form.container_title.trim() || null,
      url: form.url.trim() || null,
      tags: form.tags.split(",").map((value) => value.trim()).filter(Boolean),
    };
    setBusy(true);
    setError("");
    void createBibliographyItem(draft)
      .then(async ({ item }) => {
        if (scope === "project" && scopeProject) await attachBibliographyItem(item.bibliography_id, scopeProject.id);
        if (scope === "chat" && scopeProject && scopeChat) await attachBibliographyItem(item.bibliography_id, scopeProject.id, scopeChat.id);
        setForm({ item_type: "article", title: "", authors: "", year: "", doi: "", arxiv_id: "", container_title: "", url: "", tags: "" });
        setSelectedId(item.bibliography_id);
        setAdding(false);
        await load();
      })
      .catch((reason) => setError(String(reason)))
      .finally(() => setBusy(false));
  };

  const remove = (item: BibliographyItem) => {
    if (!deleteArmed) { setDeleteArmed(true); return; }
    setBusy(true);
    void deleteBibliographyItem(item.bibliography_id)
      .then(() => { setSelectedId(null); return load(); })
      .catch((reason) => setError(String(reason)))
      .finally(() => setBusy(false));
  };

  const uploadResource = (file: File | null) => {
    if (!selected || !file) return;
    setBusy(true);
    setError("");
    void uploadBibliographyResource(selected.bibliography_id, file)
      .then(load)
      .catch((reason) => setError(String(reason)))
      .finally(() => {
        setBusy(false);
        if (fileInputRef.current) fileInputRef.current.value = "";
      });
  };

  const submitSummary = (event: FormEvent) => {
    event.preventDefault();
    if (!selected || !summaryForm.text.trim()) return;
    setBusy(true);
    setError("");
    void addBibliographySummary(
      selected.bibliography_id,
      {
        kind: summaryForm.kind,
        title: summaryForm.title.trim() || null,
        text: summaryForm.text.trim(),
        provenance: { source: "user", surface: "library" },
      },
      coordinates.project,
      coordinates.chat,
    )
      .then(() => {
        setSummaryForm({ kind: "reading_note", title: "", text: "" });
        setAddingSummary(false);
        return load();
      })
      .catch((reason) => setError(String(reason)))
      .finally(() => setBusy(false));
  };

  const scopeLabel = scope === "global" ? "all research" : scope === "project" ? scopeProject?.name : scopeChat?.title;
  const projectConnected = Boolean(scopeProject && connections.some((connection) => connection.kind === "project" && connection.project_id === scopeProject.id));
  const chatConnected = Boolean(scopeProject && scopeChat && connections.some((connection) => connection.kind === "chat" && connection.project_id === scopeProject.id && connection.chat_id === scopeChat.id));
  return (
    <main className="bibliography-col">
      <header className="bibliography-header">
        <div>
          <span className="bibliography-kicker">common room / library</span>
          <h1>Library</h1>
          <p>Sources, reading documents, and the research that grows around them.</p>
        </div>
        <div className="bibliography-header-actions">
          <button disabled={!scopeProject} onClick={() => scopeProject && onBrowseArtifacts?.(scopeProject.id)}>browse artifacts</button>
          <a className="bibliography-export" href={bibliographyExportUrl(coordinates.project, coordinates.chat, query)} download>export .bib</a>
          <button className="confirm" onClick={() => setAdding((value) => !value)}>{adding ? "close" : "add reference"}</button>
        </div>
      </header>
      {adding && (
        <form className="bibliography-form" onSubmit={submit}>
          <label className="wide"><span>title</span><input autoFocus required value={form.title} onChange={(event) => setForm({ ...form, title: event.target.value })} /></label>
          <label className="wide"><span>authors · one per line or separated by ;</span><textarea required rows={2} value={form.authors} onChange={(event) => setForm({ ...form, authors: event.target.value })} /></label>
          <label><span>type</span><select value={form.item_type} onChange={(event) => setForm({ ...form, item_type: event.target.value })}><option value="article">article</option><option value="preprint">preprint</option><option value="book">book</option><option value="inproceedings">conference paper</option><option value="phdthesis">thesis</option><option value="misc">other</option></select></label>
          <label><span>year</span><input inputMode="numeric" value={form.year} onChange={(event) => setForm({ ...form, year: event.target.value })} /></label>
          <label><span>DOI</span><input value={form.doi} onChange={(event) => setForm({ ...form, doi: event.target.value })} /></label>
          <label><span>arXiv id</span><input value={form.arxiv_id} onChange={(event) => setForm({ ...form, arxiv_id: event.target.value })} /></label>
          <label className="wide"><span>journal / venue</span><input value={form.container_title} onChange={(event) => setForm({ ...form, container_title: event.target.value })} /></label>
          <label><span>URL</span><input value={form.url} onChange={(event) => setForm({ ...form, url: event.target.value })} /></label>
          <label><span>tags · comma separated</span><input value={form.tags} onChange={(event) => setForm({ ...form, tags: event.target.value })} /></label>
          <div className="bibliography-form-actions"><button type="button" onClick={() => setAdding(false)}>cancel</button><button className="confirm" disabled={busy} type="submit">{busy ? "saving…" : `save to ${scope}`}</button></div>
        </form>
      )}
      {error && <div className="bibliography-error">{error}</div>}
      <div className="library-workspace">
        <aside className="library-index" aria-label="Library catalogue">
          <div className="bibliography-toolbar">
            <label><span>view</span><select value={scope} onChange={(event) => setScope(event.target.value as BibliographyScope)}><option value="global">Global library</option><option value="project" disabled={!scopeProject}>Project library</option><option value="chat" disabled={!scopeChat}>Chat library</option></select></label>
            <label><span>{scope === "global" ? "actions in project" : "project"}</span><select value={scopeProject?.id ?? ""} disabled={!scopeProject} onChange={(event) => { setScopeProjectId(event.target.value); setScopeChatId(""); }}><option value="" disabled>Choose a project</option>{activeProjects.map((candidate) => <option key={candidate.id} value={candidate.id}>{candidate.name}</option>)}</select></label>
            {scope === "chat" && <label><span>chat</span><select value={scopeChat?.id ?? ""} onChange={(event) => setScopeChatId(event.target.value)}>{scopeChats.map((candidate) => <option key={candidate.id} value={candidate.id}>{candidate.title}</option>)}</select></label>}
            <label className="bibliography-search"><span>search</span><input value={query} placeholder="author, title, DOI, arXiv, tag…" onChange={(event) => setQuery(event.target.value)} /></label>
            <span className="bibliography-count">{loading ? "reading…" : `${items.length} source${items.length === 1 ? "" : "s"} · ${scopeLabel || "local view"}`}</span>
          </div>
          <div className="bibliography-list">
            {!loading && items.length === 0 && <div className="bibliography-empty"><strong>No sources in this view.</strong><span>Add a reference or choose the global library.</span></div>}
            {items.map((item) => (
              <button className={`bibliography-card ${item.bibliography_id === selectedId ? "selected" : ""}`} key={item.bibliography_id} onClick={() => setSelectedId(item.bibliography_id)} aria-pressed={item.bibliography_id === selectedId}>
                <span className="bibliography-card-key">[{item.citation_key}]</span>
                <strong>{item.title}</strong>
                <span>{item.authors.join(" · ")}</span>
                <small>{[item.container_title, item.year].filter(Boolean).join(" · ") || "date and venue not recorded"}{item.resources.length ? ` · ${item.resources.length} document${item.resources.length === 1 ? "" : "s"}` : ""}</small>
              </button>
            ))}
          </div>
        </aside>
        <section className="library-reader" aria-live="polite">
          {!selected ? (
            <div className="library-reader-empty"><strong>Select a source</strong><span>Its documents and research record will open here.</span></div>
          ) : (
            <>
              <header className="library-reader-head">
                <span className="bibliography-card-key">[{selected.citation_key}] · {selected.item_type}</span>
                <h2>{selected.title}</h2>
                <p className="bibliography-authors">{selected.authors.join(" · ")}</p>
                <p className="bibliography-venue">{[selected.container_title, selected.publisher, selected.year].filter(Boolean).join(" · ") || "date and venue not recorded"}</p>
                <div className="bibliography-meta"><Identifiers item={selected} />{selected.tags.map((tag) => <span className="bibliography-tag" key={tag}>{tag}</span>)}</div>
                {(selected.notes || selected.view.project?.notes || selected.view.chat?.notes) && <p className="bibliography-note">{selected.view.chat?.notes || selected.view.project?.notes || selected.notes}</p>}
                <div className="library-reader-actions">
                  <button className="confirm" disabled={!onDraftPrompt || !scopeProject} title={scopeProject ? "Draft a question in a research chat" : "Choose a project before asking"} onClick={() => scopeProject && onDraftPrompt?.(scopeProject.id, askDraft(selected))}>ask…</button>
                  <button disabled={!onDraftPrompt || !scopeProject} title={scopeProject ? `Draft a ${scope} summary request` : "Choose a project before summarizing"} onClick={() => scopeProject && onDraftPrompt?.(scopeProject.id, summaryDraft(selected, scope))}>summarize</button>
                  <button disabled={!onUseInWorkflow || !scopeProject} title={scopeProject ? "Create a project workflow seeded with this source" : "Choose a project before creating a workflow"} onClick={() => scopeProject && onUseInWorkflow?.(scopeProject.id, selected, workflowDraft(selected))}>use in workflow</button>
                  <button className="upload-labelled" disabled={busy} onClick={() => fileInputRef.current?.click()}><PlusIcon size={13} />attach document</button>
                  <input ref={fileInputRef} className="library-file-input" type="file" accept=".bib,.csv,.json,.jsonl,.md,.markdown,.pdf,.tex,.txt,image/*,text/*" onChange={(event) => uploadResource(event.target.files?.[0] ?? null)} />
                  {sourceUrl(selected) && <a className="bibliography-export" href={sourceUrl(selected)!} target="_blank" rel="noreferrer">open source</a>}
                  {scopeProject && (projectConnected ? <button disabled={busy} onClick={() => detach(selected, "project")}>remove from project</button> : <button disabled={busy} onClick={() => attach(selected, "project")}>add to project</button>)}
                  {scopeProject && scopeChat && (chatConnected ? <button disabled={busy} onClick={() => detach(selected, "chat")}>remove from chat</button> : <button disabled={busy} onClick={() => attach(selected, "chat")}>add to chat</button>)}
                  {scope === "global" && <button className={deleteArmed ? "danger" : ""} disabled={busy} onClick={() => remove(selected)}>{deleteArmed ? "confirm delete" : "delete"}</button>}
                </div>
              </header>
              <section className="library-summaries" aria-label="Layered summaries and notes">
                <div className="library-section-label">
                  <span>summaries + notes</span>
                  <div><small>{selected.summaries.length || "none yet"}</small><button onClick={() => setAddingSummary((value) => !value)}>{addingSummary ? "close" : "add note"}</button></div>
                </div>
                {addingSummary && (
                  <form className="library-summary-form" onSubmit={submitSummary}>
                    <div className="library-summary-form-row">
                      <label><span>kind</span><select value={summaryForm.kind} onChange={(event) => setSummaryForm({ ...summaryForm, kind: event.target.value as BibliographySummary["kind"] })}><option value="reading_note">reading note</option><option value="overview">overview</option><option value="relevance">relevance</option><option value="critique">critique</option></select></label>
                      <label><span>scope</span><input disabled value={scope === "global" ? "global library" : scope === "project" ? `project · ${scopeProject?.name ?? ""}` : `chat · ${scopeChat?.title ?? ""}`} /></label>
                    </div>
                    <label><span>title · optional</span><input value={summaryForm.title} onChange={(event) => setSummaryForm({ ...summaryForm, title: event.target.value })} /></label>
                    <label><span>note</span><textarea autoFocus required rows={5} value={summaryForm.text} onChange={(event) => setSummaryForm({ ...summaryForm, text: event.target.value })} /></label>
                    <div className="bibliography-form-actions"><button type="button" onClick={() => setAddingSummary(false)}>cancel</button><button className="confirm" disabled={busy || !summaryForm.text.trim()} type="submit">{busy ? "saving…" : "save without replacing earlier notes"}</button></div>
                  </form>
                )}
                {selected.summaries.length === 0 && !addingSummary ? (
                  <div className="library-document-empty">No summary has been written in this layer yet.</div>
                ) : (
                  <div className="library-summary-list">
                    {selected.summaries.map((summary) => (
                      <article className="library-summary" key={summary.summary_id}>
                        <header><span>{summary.scope} / {summaryKindLabel[summary.kind]}</span><small>{summaryProvenance(summary)}</small></header>
                        {summary.title && <h3>{summary.title}</h3>}
                        <Markdown text={summary.text} />
                      </article>
                    ))}
                  </div>
                )}
              </section>
              <section className="library-connections" aria-label="Research connections">
                <div className="library-section-label"><span>used in research</span><small>{connectionsLoading ? "indexing…" : `${connections.length} connection${connections.length === 1 ? "" : "s"}`}</small></div>
                {!connectionsLoading && connections.length === 0 ? (
                  <div className="library-document-empty">This source is not connected to a project, claim, workflow, run, or artifact yet.</div>
                ) : (
                  <div className="library-connection-list">
                    {connections.map((connection) => (
                      <button key={connection.connection_id} disabled={connection.available === false} onClick={() => onNavigateConnection?.(connection)}>
                        <span className="library-connection-glyph" aria-hidden="true">{connectionGlyph[connection.kind]}</span>
                        <span className="library-connection-copy">
                          <strong>{connection.label || connection.kind}</strong>
                          <small>{connection.kind.replace("_", " ")}{connection.project_name ? ` · ${connection.project_name}` : ""}{connection.available === false ? " · unavailable" : ""}{connection.status ? ` · ${connection.status}` : ""}{connection.locator ? ` · ${connection.locator}` : ""}</small>
                        </span>
                        <span className="library-connection-open" aria-hidden="true">↗</span>
                      </button>
                    ))}
                  </div>
                )}
              </section>
              <div className="library-documents">
                <div className="library-section-label"><span>documents</span><small>{selected.resources.length || "none attached"}</small></div>
                {selected.resources.length === 0 ? (
                  <div className="library-document-empty">No readable documents are attached yet. The source link remains available above.</div>
                ) : (
                  <>
                    <div className="library-document-tabs" role="tablist" aria-label="Attached documents">
                      {selected.resources.map((resource) => <button role="tab" aria-selected={resource.resource_id === resourceId} className={resource.resource_id === resourceId ? "selected" : ""} key={resource.resource_id} onClick={() => setResourceId(resource.resource_id)}><span>{resource.name}</span><small>{formatBytes(resource.bytes)}</small></button>)}
                    </div>
                    {selectedResource && <div className="library-document-reader"><ResourceReader item={selected} resource={selectedResource} /></div>}
                  </>
                )}
              </div>
            </>
          )}
        </section>
      </div>
    </main>
  );
}
