import React, { useEffect, useRef, useState } from "react";
import { ApiError, aborted, errorMessage, request } from "./organization-api";
import { ResearchMarkdown } from "./MathematicsBrowser";

const FIELDS = {
  question: ["Exact question", 8000], hypotheses: ["Definitions and hypotheses", 8000],
  argument: ["Current argument", 20000], sources: ["Sources and checks", 8000],
  failed_approaches: ["Failed approaches", 8000], gaps: ["Remaining gaps", 8000],
  next_steps: ["Next useful steps", 4000], approach: ["Approach", 2000],
} as const;
type Field = keyof typeof FIELDS;
type LinkField = "claim_ids" | "reference_ids" | "entry_ids";
type Pin = { id: string; revision_id?: string; revision?: number };
type Body = Record<Field, string> & Record<LinkField, string[]> & { pins: Partial<Record<LinkField, Pin[]>> };
export type Account = { revision: number; head_revision: number; sha256: string; content_sha256: string; review_cycle: number;
  body: Body; linked_changes: { id: string; changed: boolean; unavailable: boolean }[]; url: string; authority: string; reviews_used: number; review_limit: number;
  history: { revision: number; created_at: number }[] };
type Candidate = Pin & { label: string; kind: string };
type Attempt = { changes: Partial<Body>; expected_revision: number; client_id: string;
  expected_links: Record<string, string | number>; new_approach: boolean };
const empty = (): Body => ({ question: "", hypotheses: "", argument: "", sources: "", failed_approaches: "", gaps: "", next_steps: "",
  approach: "", claim_ids: [], reference_ids: [], entry_ids: [], pins: {} });
const linkField = (item: Candidate): LinkField => item.revision_id ? item.id.startsWith("clm_") ? "claim_ids" : "reference_ids" : "entry_ids";

export function accountReviewRequest(revision: number) {
  return `Please request an independent critique of research account version ${revision} using account_request_review. `
    + "Review the exact hypotheses and coherent argument, identify precise defects and recurring objections, then make targeted repairs if useful. "
    + "Preserve failed approaches and unresolved gaps. Return the best current answer when the review allowance is exhausted; saving or reviewing the account does not certify ledger records.";
}

export function ResearchAccount({ root, canWrite, online, reviewAvailable, onPrepareReview, onAuthLost }: {
  root: string; canWrite: boolean; online: boolean; reviewAvailable: boolean;
  onPrepareReview?: (text: string) => void; onAuthLost: () => void;
}) {
  const queryParams = new URLSearchParams(typeof window === "undefined" ? "" : window.location.search);
  const initial = queryParams.get("topic") === root.split("/").at(-1) ? queryParams.get("account_revision") : null;
  const [version, setVersion] = useState<number | null>(initial && /^\d{1,10}$/.test(initial) ? Number(initial) : null);
  const [account, setAccount] = useState<Account | null>(null), [loading, setLoading] = useState(true);
  const [draft, setDraft] = useState<Body>(empty), [editing, setEditing] = useState(false), [newApproach, setNewApproach] = useState(false);
  const [attempt, setAttempt] = useState<Attempt | null>(null), [busy, setBusy] = useState(false), [error, setError] = useState("");
  const [query, setQuery] = useState(""), [candidates, setCandidates] = useState<Candidate[]>([]);
  const [expectedLinks, setExpectedLinks] = useState<Record<string, string | number>>({});
  const [newer, setNewer] = useState<number | null>(null), [refresh, setRefresh] = useState(0);
  useEffect(() => {
    const linked = () => { const query = new URLSearchParams(window.location.search), revision = query.get("account_revision");
      if (query.get("topic") === root.split("/").at(-1) && revision && /^\d{1,10}$/.test(revision)) {
        setVersion(Number(revision)); setEditing(false); setRefresh(value => value + 1);
      } };
    window.addEventListener("popstate", linked); return () => window.removeEventListener("popstate", linked);
  }, [root]);
  const latest = useRef(account); latest.current = account;
  const fail = (cause: unknown) => { if (cause instanceof ApiError && cause.status === 401) onAuthLost(); else setError(errorMessage(cause)); };
  useEffect(() => {
    const controller = new AbortController(); setLoading(true); setError("");
    void request<Account | null>(root + "/research-account" + (version ? `?revision=${version}` : ""), { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) { setAccount(value); setNewer(null); setLoading(false); } })
      .catch(cause => { if (!aborted(cause)) { fail(cause); setLoading(false); } });
    return () => controller.abort();
  }, [root, version, refresh]);
  useEffect(() => {
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const value = await request<{ revision: number } | null>(root + "/research-account?summary=true", { signal: controller.signal });
        if (!controller.signal.aborted && value && value.revision !== latest.current?.revision) setNewer(value.revision);
      } catch (cause) { if (!aborted(cause)) fail(cause); }
      if (!controller.signal.aborted) timer = setTimeout(poll, 10000);
    };
    timer = setTimeout(poll, 10000);
    return () => { controller.abort(); clearTimeout(timer); };
  }, [root]);
  const edit = () => {
    const value = account?.body ?? empty(); setDraft(structuredClone(value));
    setExpectedLinks(Object.fromEntries(Object.values(value.pins).flat().filter((pin): pin is Pin => !!pin)
      .map(pin => [pin.id, pin.revision_id ?? pin.revision!])));
    setEditing(true); setNewApproach(false); setError("");
  };
  const save = async () => {
    setBusy(true); setError("");
    try {
      const changes = Object.fromEntries([...Object.keys(FIELDS), "claim_ids", "reference_ids", "entry_ids"]
        .filter(key => JSON.stringify(draft[key as keyof Body]) !== JSON.stringify(account?.body[key as keyof Body])
          || (["claim_ids", "reference_ids", "entry_ids"].includes(key) && draft[key as LinkField].some(id => {
            const pin = account?.body.pins[key as LinkField]?.find(item => item.id === id);
            return expectedLinks[id] !== (pin?.revision_id ?? pin?.revision);
          })))
        .map(key => [key, draft[key as keyof Body]]));
      if (!attempt && !Object.keys(changes).length) { setEditing(false); return; }
      const captured: Attempt = attempt ?? { changes, expected_revision: account?.revision ?? 0,
        client_id: crypto.randomUUID(), expected_links: expectedLinks, new_approach: newApproach };
      setAttempt(captured);
      const value = await request<Account>(root + "/research-account", { method: "PUT", body: captured });
      setAccount(value); setAttempt(null); setEditing(false); setNewer(null); setVersion(null);
    } catch (cause) {
      // A definite refusal has no uncertain side effect; retain the user's draft for reconciliation.
      if (cause instanceof ApiError && cause.status >= 400 && cause.status < 500 && cause.status !== 408) setAttempt(null);
      if (cause instanceof ApiError && cause.status === 409) {
        void request<{ revision: number } | null>(root + "/research-account?summary=true").then(value => {
          if (value && value.revision !== account?.revision) setNewer(value.revision);
        }).catch(fail);
      }
      fail(cause);
    } finally { setBusy(false); }
  };
  const current = !!account && account.revision === account.head_revision && !newer;
  return <section className="org-research-account" aria-label="Research account">
    <header><h3>Research account</h3>{account && <span>Version {account.revision}{!current && " · earlier candidate"} · review cycle {account.review_cycle}</span>}</header>
    <p className="org-help">One evolving account of this question. Saving preserves the argument and its gaps; it does not establish a mathematical result.</p>
    {loading && <p role="status">Loading research account…</p>}
    {error && <p role="alert" className="org-error">{error}</p>}
    {newer && <p role="status">Version {newer} is available. {editing ? "Your unsaved text is preserved. Copy any changes before loading the current account." : "Load it to continue the current argument."}
      <button type="button" disabled={busy || !!attempt} onClick={() => { setVersion(null); setRefresh(value => value + 1); setEditing(false); }}>Load current account</button></p>}
    {!loading && !editing && !account && <><p>Ordinary conversation needs no account. Start one for a question you want to keep developing.</p>
      {canWrite && <button type="button" disabled={!online} onClick={edit}>Start research account</button>}</>}
    {!editing && account && <>
      {Object.entries(FIELDS).filter(([key]) => account.body[key as Field]).map(([key, [label]]) => <section key={key}>
        <h4>{label}</h4><ResearchMarkdown>{account.body[key as Field]}</ResearchMarkdown></section>)}
      {account.linked_changes?.map(link => <p key={link.id} role="status">Linked source {link.id} {link.unavailable ? "is unavailable" : "has changed"}. The account retains its pinned revision.</p>)}
      <details><summary>Linked knowledge and exact versions</summary>
        {Object.values(account.body.pins).flat().filter((pin): pin is Pin => !!pin).map(pin => <p key={pin.id}>
          <code>{pin.id}</code> · {pin.revision_id ?? `revision ${pin.revision}`}</p>)}
        <p>These revisions remain pinned when a linked result changes. Read its current version before deliberately updating the link.</p>
      </details>
      <p className="org-help">{account.reviews_used ?? 0}/{account.review_limit ?? 2} critiques requested in this approach. Repeated feedback is a reason to change approach or leave an explicit gap.</p>
      {canWrite && <button type="button" disabled={!online || !current} onClick={edit}>Edit current account</button>}
      {canWrite && <button type="button" disabled={!online || !current || !reviewAvailable || (account.reviews_used ?? 0) >= 2}
        onClick={() => onPrepareReview?.(accountReviewRequest(account.revision))}>Prepare independent critique</button>}
      {!reviewAvailable && <p className="org-help">Choose a native model with research tools and enable Fresh-context review in /tools to request a critique. CLI bots can maintain the account and hand review to that model.</p>}
      {canWrite && current && (account.reviews_used ?? 0) >= 2 && <button type="button" onClick={() => { edit(); setNewApproach(true); }}>Describe a new approach</button>}
      <p className="org-help">Prepare places a request in your message composer. Sending it uses your normal model and budget controls. See /reviews for exact-version reports.</p>
      <label>Saved candidate<select aria-label="Saved account candidate" value={account.revision} disabled={busy} onChange={event => setVersion(Number(event.target.value))}>
        {account.history.map(item => <option key={item.revision} value={item.revision}>Version {item.revision} · {new Date(item.created_at * 1000).toLocaleString()}</option>)}
      </select></label><a href={account.url}>Link to this account version</a>
      {!current && <button type="button" onClick={() => { setVersion(null); setRefresh(value => value + 1); }}>Current account</button>}
    </>}
    {editing && <form onSubmit={event => { event.preventDefault(); void save(); }}>
      {Object.entries(FIELDS).map(([key, [label, maxLength]]) => <label key={key}>{label}<textarea aria-label={label}
        required={key === "question" || key === "approach" && newApproach} maxLength={maxLength} rows={key === "argument" ? 8 : 3}
        disabled={busy || !!attempt} value={draft[key as Field]} onChange={event => setDraft(value => ({ ...value, [key]: event.target.value }))} /></label>)}
      {newApproach && <p>A new review cycle requires a changed approach. Preserve the previous failed routes and explain what mathematical strategy changes.</p>}
      <fieldset disabled={busy || !!attempt}><legend>Working knowledge</legend>
        {(["claim_ids", "reference_ids", "entry_ids"] as const).flatMap(field => draft[field].map(id => <p key={id}>{id}
          <button type="button" aria-label={`Unlink ${id}`} onClick={() => setDraft(value => ({ ...value, [field]: value[field].filter(item => item !== id) }))}>Unlink</button></p>))}
        <label>Search project knowledge<input maxLength={200} value={query} onChange={event => setQuery(event.target.value)} /></label>
        <button type="button" disabled={!query.trim() || !online} onClick={() => {
          setError(""); void request<Candidate[]>(root + "/research-account/knowledge?query=" + encodeURIComponent(query)).then(setCandidates).catch(fail);
        }}>Find knowledge to link</button>
        {candidates.map(item => <p key={item.id}>{item.kind} · {item.label}
          <button type="button" onClick={() => { const field = linkField(item);
            setDraft(value => ({ ...value, [field]: [...new Set([...value[field], item.id])] }));
            setExpectedLinks(value => ({ ...value, [item.id]: item.revision_id ?? item.revision! }));
          }}>Pin this version</button></p>)}
      </fieldset>
      <button type="submit" disabled={busy || !online}>{busy ? "Saving…" : attempt ? "Retry exact save" : "Save account"}</button>
      <button type="button" disabled={busy || !!attempt} onClick={() => setEditing(false)}>Cancel edit</button>
      {attempt && <p>Retry uses the exact same update until its receipt is known.</p>}
    </form>}
  </section>;
}
