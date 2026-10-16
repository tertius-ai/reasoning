import React, { useEffect, useRef, useState } from "react";
import { ApiError, Hierarchy, KnowledgeEntry, aborted, errorMessage, organizationPath, request } from "./organization-api";
import { ResearchMarkdown } from "./MathematicsBrowser";

type Write = { client_id: string; kind: "journal"; title: string; body: string; state: "draft" | "published";
  scope_kind?: "project" | "organization"; scope_id?: string | null; expected_revision?: number; unpublish?: boolean };
type Attempt = { path: string; method: "POST" | "PUT"; body: Write };
type DeleteAttempt = { id: string; title: string; client_id: string; expected_revision: number };

export function ResearchBlog({ organizationId, hierarchy, canWrite, initialProjectId, onAuthLost }: {
  organizationId: string; hierarchy: Hierarchy; canWrite: boolean; initialProjectId?: string; onAuthLost: () => void;
}) {
  const [posts, setPosts] = useState<KnowledgeEntry[]>([]), [drafts, setDrafts] = useState<KnowledgeEntry[]>([]);
  const [project, setProject] = useState(""), [tab, setTab] = useState<"published" | "drafts">("published");
  const [editing, setEditing] = useState(false), [selected, setSelected] = useState<KnowledgeEntry | null>(null);
  const [title, setTitle] = useState(""), [body, setBody] = useState(""), [scope, setScope] = useState(initialProjectId ?? hierarchy.projects[0]?.id ?? "");
  const [preview, setPreview] = useState(false), [busy, setBusy] = useState(false), [error, setError] = useState("");
  const [attempt, setAttempt] = useState<Attempt | null>(null), [refresh, setRefresh] = useState(0);
  const [deleteAttempt, setDeleteAttempt] = useState<DeleteAttempt | null>(null);
  const [cursors, setCursors] = useState<{ published: string | null; drafts: string | null }>({ published: null, drafts: null });
  const [next, setNext] = useState(cursors);
  const saving = useRef(false), pending = useRef<Attempt | null>(null);
  const root = organizationPath(organizationId), blog = `/blog/${encodeURIComponent(organizationId)}`;
  // Keep the static blog's theme and math enhancement in the public release graph.
  useEffect(() => { void import("./blogTheme"); void import("./blogMathBootstrap"); }, []);
  // The authenticated editor and static reader share one server-owned stylesheet.
  useEffect(() => {
    const stylesheet = document.createElement("link");
    stylesheet.rel = "stylesheet"; stylesheet.href = "/blog.css";
    document.head.append(stylesheet);
    return () => stylesheet.remove();
  }, []);
  const fail = (cause: unknown) => { if (cause instanceof ApiError && cause.status === 401) onAuthLost(); else setError(errorMessage(cause)); };
  useEffect(() => {
    // The published and draft lists are independent: request both at once and show each as it arrives.
    const abort = new AbortController();
    const failed = (cause: unknown) => { if (!aborted(cause)) fail(cause); };
    void request<{ posts: KnowledgeEntry[]; next_before: string | null }>(root + "/blog?"
      + (project ? "project=" + encodeURIComponent(project) + "&" : "")
      + (cursors.published ? "before=" + encodeURIComponent(cursors.published) : ""), { signal: abort.signal })
      .then(published => {
        if (!abort.signal.aborted) { setPosts(published.posts); setNext(current => ({ ...current, published: published.next_before })); }
      }).catch(failed);
    void request<KnowledgeEntry[]>(root + "/knowledge?kind=journal&limit=50"
      + (project ? "&project_id=" + encodeURIComponent(project) : "")
      + (cursors.drafts ? "&before=" + encodeURIComponent(cursors.drafts) : ""), { signal: abort.signal })
      .then(entries => {
        if (abort.signal.aborted) return;
        setDrafts(entries.filter(entry => entry.state === "draft")); const last = entries.at(-1);
        setNext(current => ({ ...current, drafts: entries.length === 50 && last ? `${last.revised_at}:${last.id}` : null }));
      }).catch(failed);
    return () => abort.abort();
  }, [root, refresh, onAuthLost, cursors, project]);
  const edit = async (entry?: KnowledgeEntry) => {
    setError(""); setPreview(false);
    try {
      const current = entry ? await request<KnowledgeEntry>(root + "/knowledge/" + entry.id) : null;
      setSelected(current); setTitle(current?.title ?? ""); setBody(current?.body ?? "");
      setScope(current?.project_id ?? initialProjectId ?? hierarchy.projects[0]?.id ?? ""); setEditing(true);
    } catch (cause) { fail(cause); }
  };
  const save = async (state: "draft" | "published", unpublish = false) => {
    if (saving.current) return;
    saving.current = true;
    const captured: Attempt = pending.current ?? { path: root + "/knowledge" + (selected ? "/" + selected.id : ""), method: selected ? "PUT" : "POST",
      body: { client_id: crypto.randomUUID(), kind: "journal", title, body, state,
        ...(selected ? { expected_revision: selected.head_revision } : { scope_kind: scope ? "project" : "organization", scope_id: scope || null }),
        ...(unpublish ? { unpublish: true } : {}) } };
    pending.current = captured; setAttempt(captured); setBusy(true); setError("");
    try {
      await request<KnowledgeEntry>(captured.path, { method: captured.method, body: captured.body });
      pending.current = null; setAttempt(null); setEditing(false); setRefresh(value => value + 1);
      setCursors({ published: null, drafts: null });
      setTab(state === "draft" ? "drafts" : "published");
    } catch (cause) {
      if (cause instanceof ApiError && [400, 403, 404, 409, 413, 422, 429].includes(cause.status)) { pending.current = null; setAttempt(null); }
      fail(cause);
    } finally { saving.current = false; setBusy(false); }
  };
  const remove = async (post?: KnowledgeEntry) => {
    if (saving.current) return;
    if (!deleteAttempt && (!post || !window.confirm(`Delete “${post.title}”${post.published_revision != null ? " and its published version" : ""}? It will disappear from the blog and agent context. Its revision history remains retained.`))) return;
    const captured = deleteAttempt ?? { id: post!.id, title: post!.title, client_id: crypto.randomUUID(), expected_revision: post!.head_revision };
    saving.current = true; setBusy(true); setError(""); setDeleteAttempt(captured);
    try {
      await request(root + "/blog/" + captured.id, { method: "DELETE", body: { client_id: captured.client_id, expected_revision: captured.expected_revision } });
      setDeleteAttempt(null); setEditing(false); setSelected(null); setRefresh(value => value + 1); setCursors({ published: null, drafts: null });
    } catch (cause) {
      if (cause instanceof ApiError && [400, 403, 404, 409, 413, 422, 429].includes(cause.status)) setDeleteAttempt(null);
      fail(cause);
    } finally { saving.current = false; setBusy(false); }
  };
  const rows = (tab === "published" ? posts : drafts).filter(post => !project || post.project_id === project);
  return <section className="org-blog research-blog" aria-label="Research blog">
    <header className="blog-masthead"><div><span className="blog-eyebrow">Organization research · Editor</span><h2>Research blog</h2></div><a href={blog}>Read blog</a>
      {canWrite && <button disabled={busy || !!attempt || !!deleteAttempt} onClick={() => void edit()}>New post</button>}</header>
    {error && <p role="alert" className="org-error">{error}</p>}
    {deleteAttempt && <div className="blog-actions"><span role="status">Deletion unconfirmed: {deleteAttempt.title}</span><button disabled={busy} onClick={() => void remove()}>Retry unchanged deletion</button></div>}
    {editing ? <form className="blog-editor" onSubmit={event => { event.preventDefault(); void save("draft"); }}>
      <fieldset disabled={busy || !!attempt || !!deleteAttempt}><label>Title<input aria-label="Title" required maxLength={300} value={title} onChange={event => setTitle(event.target.value)} /></label>
        {!selected && <label>Project tag<select aria-label="Project tag" value={scope} onChange={event => setScope(event.target.value)}>
          {hierarchy.projects.map(value => <option key={value.id} value={value.id}>{value.name}</option>)}<option value="">Organization</option></select></label>}
        <label>Markdown<textarea aria-label="Markdown" required rows={12} maxLength={64000} value={body} onChange={event => setBody(event.target.value)} /></label>
      </fieldset>
      <div className="blog-actions"><button type="button" aria-expanded={preview} onClick={() => setPreview(!preview)}>Preview</button>
      {attempt ? <button type="button" disabled={busy} onClick={() => void save(attempt.body.state, !!attempt.body.unpublish)}>Retry unchanged save</button> : <>
        <button disabled={busy || !!deleteAttempt}>Save draft</button><button type="button" disabled={busy || !!deleteAttempt || !title.trim() || !body.trim()} onClick={() => void save("published")}>Publish</button>
        {selected?.published_revision != null && <button type="button" disabled={busy || !!deleteAttempt} onClick={() => {
          if (window.confirm("Unpublish this post? Its revisions remain available to authorized editors.")) void save("draft", true);
        }}>Unpublish</button>}
      </>}
      {canWrite && selected && <button type="button" disabled={busy || !!attempt || !!deleteAttempt} onClick={() => void remove(selected)}>Delete post</button>}
      <button type="button" disabled={busy || !!attempt || !!deleteAttempt} onClick={() => setEditing(false)}>Close editor</button>
      </div>
      {preview && <article className="org-blog-preview blog-article"><span className="blog-eyebrow">Preview</span><h1>{title}</h1><ResearchMarkdown>{body}</ResearchMarkdown></article>}
      {attempt && <p role="status">Save outcome unconfirmed. Retry retains the exact draft and request identity.</p>}
    </form> : <>
      <nav className="blog-toolbar" aria-label="Post filters"><button aria-pressed={tab === "published"} onClick={() => setTab("published")}>Published</button>
        {canWrite && <button aria-pressed={tab === "drafts"} onClick={() => setTab("drafts")}>Drafts</button>}
        <label>Project<select aria-label="Project" value={project} onChange={event => { setProject(event.target.value); setCursors({ published: null, drafts: null }); }}><option value="">All projects</option>
          {hierarchy.projects.map(value => <option key={value.id} value={value.id}>{value.name}</option>)}</select></label>
        <button onClick={() => setRefresh(value => value + 1)}>Refresh</button></nav>
      {!rows.length && <p>No {tab} posts.</p>}
      {rows.map(post => <article className="org-blog-post blog-entry" key={post.id}>
        <div className="blog-meta"><span className="blog-tag">{hierarchy.projects.find(value => value.id === post.project_id)?.name ?? "Organization"}</span><span>{post.author_name}</span><time dateTime={new Date(post.revised_at * 1000).toISOString()}>{new Date(post.revised_at * 1000).toISOString().slice(0, 10)}</time></div>
        <h3>{tab === "published" ? <a href={blog + "/" + post.id}>{post.title}</a> : post.title}</h3>
        {tab === "drafts" && <p className="blog-excerpt">{post.body.replace(/\s+/g, " ").slice(0, 240)}{post.body.length > 240 ? "…" : ""}</p>}
        {canWrite && <div className="blog-actions"><button disabled={busy || !!deleteAttempt} onClick={() => void edit(post)}>Edit draft</button>
          <button disabled={busy || !!deleteAttempt} onClick={() => void remove(post)}>Delete post</button></div>}
      </article>)}
      <nav className="blog-pages" aria-label="Blog pages"><button disabled={!cursors[tab]} onClick={() => setCursors({ ...cursors, [tab]: null })}>Newest</button>
        <button disabled={!next[tab]} onClick={() => setCursors({ ...cursors, [tab]: next[tab] })}>Older posts</button></nav>
    </>}
  </section>;
}
