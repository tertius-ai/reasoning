import React, { Suspense, lazy, useCallback, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import { ApiError, aborted, errorMessage, organizationPath, request } from "./organization-api";
import { normalizeMathDelimiters } from "./markdownMath";
import "./mathematics-browser.css";
import { GraphView } from "./Graph";
import type { ClaimDetail, ClaimSummary, GraphPayload, ReferenceSummary } from "./api";
import { RichTextPolicy } from "./RichTextPolicy";
import { LedgerHumanChecks } from "./LedgerHumanChecks";
import { MarkdownMedia } from "./TopicMedia";

const loadLedger = () => import("./Ledger");
// Selecting or pointing at a statement starts loading the inspector alongside its record.
const preloadLedger = () => { void loadLedger().catch(() => { /* The inspector's boundary reports load failures. */ }); };
const LedgerDetail = lazy(() => loadLedger().then(module => ({ default: module.Detail })));
const ReferenceDetail = lazy(() => loadLedger().then(module => ({ default: module.RefDetail })));

type Claim = ClaimSummary;
type Detail = ClaimDetail;
const label = (claim: Claim) => claim.authority === "review" ? "LLM-reviewed" : claim.kind === "definition" ? "Definition" : claim.status;
export function ResearchMarkdown({ children }: { children: string }) {
  return <ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[rehypeKatex]} components={{ img: MarkdownMedia,
    p: ({ node, children }) => node?.children.some(child => child.type === "element" && child.tagName === "img")
      ? <div className="topic-media-paragraph">{children}</div> : <p>{children}</p>,
  }} skipHtml>{normalizeMathDelimiters(children)}</ReactMarkdown>;
}

export function MathematicsBrowser({ organizationId, topicId, projectId, canWrite = false, onAuthLost }: {
  organizationId: string; topicId?: string; projectId?: string; canWrite?: boolean; onAuthLost: () => void;
}) {
  const [claims, setClaims] = useState<Claim[]>([]), [selected, setSelected] = useState<Detail | null>(null);
  const [reference, setReference] = useState<ReferenceSummary | null>(null);
  const [query, setQuery] = useState(""), [error, setError] = useState(""), [loading, setLoading] = useState(true);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [editing, setEditing] = useState(false), [statement, setStatement] = useState(""), [kind, setKind] = useState("claim");
  const [clientId, setClientId] = useState(() => crypto.randomUUID()), [busy, setBusy] = useState(false), [refresh, setRefresh] = useState(0);
  const root = organizationPath(organizationId) + "/mathematics";
  const [view, setView] = useState<"graph" | "list">("graph");
  const selection = useRef(0);
  const scopeQuery = topicId ? "topic_id=" + encodeURIComponent(topicId) : projectId ? "project_id=" + encodeURIComponent(projectId) : "";
  const loadGraph = useCallback(() => request<GraphPayload>(root + "/graph?" + scopeQuery), [root, scopeQuery]);
  // The record that opened a statement is handed to its inspector once instead of being read again.
  const opened = useRef<{ id: string; refresh: number; at: number; detail: Promise<ClaimDetail> } | null>(null);
  const currentRefresh = useRef(refresh); currentRefresh.current = refresh;
  const readClaim = (id: string) => {
    const detail = request<ClaimDetail>(root + "/" + id + "?history=true");
    opened.current = { id, refresh: currentRefresh.current, at: Date.now(), detail };
    return detail;
  };
  const loadClaim = useCallback((id: string) => {
    const handoff = opened.current;
    opened.current = null;
    if (handoff && handoff.id === id && handoff.refresh === currentRefresh.current && Date.now() - handoff.at < 10000) return handoff.detail;
    return request<ClaimDetail>(root + "/" + id + "?history=true");
  }, [root]);
  const fail = (cause: unknown) => { if (cause instanceof ApiError && cause.status === 401) onAuthLost(); else setError(errorMessage(cause)); };
  useEffect(() => {
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    const load = async () => {
      try {
        const value = await request<{ claims?: Record<string, Claim>; matches?: Claim[]; counts?: Record<string, number> }>(
          root + `?q=${encodeURIComponent(query)}&${scopeQuery}`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        setClaims(value.matches ?? Object.values(value.claims ?? {})); setCounts(value.counts ?? {}); setLoading(false); setError("");
      } catch (cause) { if (!aborted(cause)) { fail(cause); setLoading(false); } }
      if (!controller.signal.aborted) timer = setTimeout(load, 5000);
    };
    void load(); return () => { controller.abort(); clearTimeout(timer); };
  }, [root, scopeQuery, query, refresh, onAuthLost]);
  const open = async (claimId: string, force = false) => {
    if ((editing || busy) && !force) return;
    const attempt = ++selection.current;
    preloadLedger();
    try { const value = await readClaim(claimId) as Detail & { record?: { claim_id?: string; reference_id?: string }; revision?: ReferenceSummary };
      if (value.record?.reference_id && value.revision) {
        if (attempt === selection.current) { setSelected(null); setReference(value.revision); }
      } else if (value.record?.claim_id) {
        const claim = await readClaim(value.record.claim_id);
        if (attempt === selection.current) { setSelected(claim); setReference(null); }
      } else if (value.claim_id && attempt === selection.current) { setSelected(value); setReference(null); }
    } catch (cause) { if (attempt === selection.current) fail(cause); }
  };
  useEffect(() => {
    const id = new URLSearchParams(window.location.search).get("claim");
    if (id && /^clm_[a-f0-9]{24}$/.test(id)) void open(id);
  }, [root]);
  return <section className="org-mathematics org-ledger-workbench" aria-label="Mathematical research ledger">
    <header><div><h3>Research ledger</h3><p>Precise statements. Pinned dependencies. Independent evidence.</p></div>
      <small>{Object.entries(counts).map(([name, count]) => `${count} ${name.replaceAll("_", " ")}`).join(" · ")}</small>
      <button disabled={editing || busy} onClick={() => setRefresh(value => value + 1)}>Refresh</button>
      {canWrite && <button disabled={busy} onClick={() => { setSelected(null); setReference(null); setStatement(""); setKind("claim"); setClientId(crypto.randomUUID()); setEditing(true); }}>Record statement</button>}</header>
    {editing && <form onSubmit={(event) => {
      event.preventDefault(); setBusy(true); setError("");
      void request<{ claim_id: string; refused?: string }>(root, { method: "POST", body: {
        name: selected ? "ledger_refine" : "ledger_claim", client_id: clientId, topic_id: topicId ?? null, project_id: projectId ?? null,
        arguments: selected ? { claim_id: selected.claim_id, statement, expected_revision_id: selected.current_revision_id } : { statement, kind },
      } }).then((value) => { if (value.refused) { setError("The statement changed. Cancel and reopen its current revision before editing."); return; }
        setEditing(false); setRefresh((value) => value + 1); void open(value.claim_id, true);
      }).catch(fail).finally(() => setBusy(false));
    }}>
      {!selected && <label>Type<select value={kind} disabled={busy} onChange={(event) => setKind(event.target.value)}><option value="claim">Claim</option><option value="definition">Definition</option></select></label>}
      <label>Precise statement<textarea required maxLength={12000} rows={5} value={statement} disabled={busy} onChange={(event) => setStatement(event.target.value)} /></label>
      <p className="org-help">{topicId ? "Shared within this project." : "Shared with the organization."} Recording a statement does not certify it.</p>
      <button disabled={busy}>{busy ? "Saving…" : "Save statement"}</button><button type="button" disabled={busy} onClick={() => setEditing(false)}>Cancel</button>
    </form>}
    <input aria-label="Search mathematical claims" type="search" placeholder="Find a definition, conjecture, or proof…" value={query} onChange={(event) => setQuery(event.target.value)} />
    {error && <p role="alert" className="org-error">{error}</p>}
    <nav className="org-ledger-tabs" aria-label="Ledger views"><button aria-pressed={view === "graph"} onClick={() => setView("graph")}>Dependency graph</button>
      <button aria-pressed={view === "list"} onClick={() => setView("list")}>Statements</button></nav>
    {view === "graph" && !query && <div className="org-ledger-graph"><GraphView projectId={projectId ?? topicId ?? organizationId}
      version={String(refresh)} loadGraph={loadGraph} onOpen={id => void open(id)} /></div>}
    <div className={"org-mathematics-layout" + (view === "graph" && !query ? " org-ledger-graph-detail" : "")}><div className="org-mathematics-list" hidden={view === "graph" && !query}>
      {claims.map((claim) => <button key={claim.claim_id} disabled={editing || busy} aria-current={selected?.claim_id === claim.claim_id ? "true" : undefined}
        onPointerEnter={preloadLedger} onFocus={preloadLedger} onClick={() => void open(claim.claim_id)}>
        <span className="org-math-status" data-status={claim.status}>{label(claim)}</span><ResearchMarkdown>{claim.statement_text}</ResearchMarkdown>
        <small>{claim.claim_id}</small></button>)}
      {!claims.length && <p className="org-help">{loading ? "Loading the mathematical record…" : "No matching claims. Research agents record reusable mathematics here as they work."}</p>}
    </div><article>{selected ? <>
      <header><span className="org-math-status" data-status={selected.status}>{label(selected)}</span>
        {canWrite && <button onClick={() => { setStatement(selected.statement_text); setClientId(crypto.randomUUID()); setEditing(true); }}>Edit</button>}
        <button onClick={() => { ++selection.current; setSelected(null); setEditing(false); }} aria-label="Close claim">×</button></header>
      <section className="org-ledger-inspector"><RichTextPolicy.Provider value={{ onOpenLedger: id => void open(id), allowArtifacts: false, allowImages: false }}>
        <Suspense fallback={<p role="status">Loading proof inspector…</p>}><LedgerDetail
        projectId={projectId ?? topicId ?? organizationId} id={selected.claim_id} version={String(refresh)} loadClaim={loadClaim}
        renderHumanCheck={(objectId, revisionId) => <LedgerHumanChecks root={root} objectId={objectId} revisionId={revisionId} canWrite={canWrite} />}
        onBack={() => { ++selection.current; setSelected(null); }} onOpen={id => void open(id)} /></Suspense></RichTextPolicy.Provider></section>
      <p className="org-help">LLM review is independent evidence, not machine-checked formalization.</p>
    </> : reference ? <Suspense fallback={<p role="status">Loading reference…</p>}><ReferenceDetail projectId={projectId ?? organizationId}
      reference={reference} citing={claims.filter(claim => claim.reference_id === reference.reference_id)} sourceEnabled={false}
      onBack={() => { ++selection.current; setReference(null); }} onOpenClaim={id => void open(id)} onChanged={() => setRefresh(value => value + 1)} /></Suspense>
      : <div className="org-math-empty"><span>λ</span><h3>Follow the mathematics</h3><p>Select a statement to inspect its argument and dependencies.</p></div>}</article></div>
  </section>;
}
