import {
  PointerEvent as ReactPointerEvent,
  ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { PlusIcon } from "./ComposerIcons";
import {
  ClaimDetail,
  LedgerOverview,
  fetchArtifacts,
  fetchClaim,
  fetchJournal,
  fetchOverview,
  fetchResolve,
  fetchVersion,
  renameArtifact,
  uploadArtifact,
  uploadReferenceFile,
} from "./api";
import { RevealArtifactButton } from "./ArtifactActions";
import { GraphView } from "./Graph";
import { TeX, Markdown, openArtifact, openLedgerEntry } from "./Markdown";
import { ProgressiveList } from "./progressive";
import { IconButton } from "./IconButton";
import type { ArtifactInfo, ReferenceSummary } from "./api";
import { OrganizationLedgerBinding, OrganizationLedgerPanel } from "./OrganizationLedgerPanel";

/** author · title (year), with each supplied identifier as an outbound
 *  link. `locator` (per-claim) shows where in the source. */
function ReferenceCite({
  reference,
  locator,
}: {
  reference: ReferenceSummary;
  locator?: string | null;
}) {
  const links: { label: string; href: string }[] = [];
  if (reference.arxiv)
    links.push({ label: "arXiv", href: `https://arxiv.org/abs/${reference.arxiv}` });
  if (reference.doi)
    links.push({ label: "doi", href: `https://doi.org/${reference.doi}` });
  if (reference.url) {
    try {
      const url = new URL(reference.url);
      if (["https:", "http:"].includes(url.protocol)) links.push({ label: "url", href: url.href });
    } catch { /* An invalid source identifier is displayed as data, never an executable link. */ }
  }
  if (reference.isbn)
    links.push({
      label: "isbn",
      href: `https://openlibrary.org/isbn/${reference.isbn.replace(/[^0-9Xx]/g, "")}`,
    });
  return (
    <div className="claim-citation">
      {reference.author}, <em>{reference.title}</em>
      {reference.year ? ` (${reference.year})` : ""}
      {locator ? `, ${locator}` : ""}
      {links.map((link) => (
        <a
          key={link.label}
          className="ref-link"
          href={link.href}
          target="_blank"
          rel="noreferrer"
          onClick={(e) => e.stopPropagation()}
        >
          {link.label}
        </a>
      ))}
      {reference.notes ? <span className="ref-notes"> — {reference.notes}</span> : null}
    </div>
  );
}

/** A verification log entry; when its evidence LaTeX was autorepaired
 *  for display, a caption toggles to the author's original. */
function VerificationLog({ v }: { v: import("./api").VerificationInfo }) {
  const [showOriginal, setShowOriginal] = useState(false);
  const repaired = v.original != null && v.original !== v.text;
  const shown = showOriginal ? v.original : v.text;
  return (
    <div className="verification-row">
      <div className="proof-meta">
        <span
          className={`status-chip status-${
            v.verdict === "verified" || v.verdict === "established"
              ? "established"
              : v.verdict === "flawed" || v.verdict === "refuted"
                ? "refuted"
                : "conjectured"
          }`}
        >
          {/* a failed review is not a refutation — the claim merely did
              not pass, it was not proved false */}
          {v.verdict === "refuted" && v.authority === "review"
            ? "failed"
            : v.verdict}
          {v.authority ? ` · ${v.authority}` : ""}
        </span>
        {v.kind && <span className="verification-kind">{v.kind}</span>}
      </div>
      {shown && <Markdown text={shown} />}
      {repaired && (
        <button
          className="latex-repair-note"
          onClick={() => setShowOriginal((s) => !s)}
        >
          {showOriginal
            ? "view LaTeX autorepair"
            : "LaTeX autorepaired — view original"}
        </button>
      )}
    </div>
  );
}

/** One reference, opened like a claim: full bibliographic record, the
 *  cached source file with its upload control, and the claims citing
 *  it. The model links a `ref_…` slug in chat to send the user here —
 *  e.g. to ask for a paywalled paper to be uploaded. */
export function RefDetail({
  projectId,
  reference,
  citing,
  onBack,
  onOpenClaim,
  onChanged,
  sourceEnabled = true,
}: {
  projectId: string;
  reference: ReferenceSummary;
  citing: LedgerOverview["claims"];
  onBack: () => void;
  onOpenClaim: (id: string) => void;
  onChanged: () => void;
  sourceEnabled?: boolean;
}) {
  return (
    <div>
      <button className="icon-btn detail-back" onClick={onBack}>
        ← all refs
      </button>
      <div className="claim-head-row">
        <span className="claim-id">{reference.reference_id}</span>
      </div>
      <ReferenceCite reference={reference} />
      {reference.notes && <div className="ref-notes">{reference.notes}</div>}
      {sourceEnabled && <><div className="detail-section">Source file</div><ReferenceSource
        projectId={projectId}
        reference={reference}
        onChanged={onChanged}
      /></>}
      {citing.length > 0 && (
        <>
          <div className="detail-section">Cited by</div>
          {citing.map((claim) => (
            <div key={claim.claim_id} className="premise-row">
              <StatusChip status={claim.status} authority={claim.authority} />
              <span
                className="claim-id ledger-link"
                onClick={() => onOpenClaim(claim.claim_id)}
              >
                {claim.claim_id.slice(0, 12)}
              </span>
              <span className="dep-statement">
                <TeX text={claim.statement_text} />
              </span>
            </div>
          ))}
        </>
      )}
    </div>
  );
}

/** A reference's cached source copy: link to it when present, and an
 *  upload control either way — this is how the user supplies a
 *  paywalled paper so citation reviews can actually read the source. */
function ReferenceSource({
  projectId,
  reference,
  onChanged,
}: {
  projectId: string;
  reference: ReferenceSummary;
  onChanged: () => void;
}) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const upload = (file: File) => {
    setBusy(true);
    setFailed(false);
    uploadReferenceFile(projectId, reference.reference_id, file)
      .then(onChanged)
      .catch(() => setFailed(true))
      .finally(() => setBusy(false));
  };
  return (
    <div className="ref-source-row">
      {reference.file && (
        <a
          className="ref-source-link"
          href={`/api/projects/${projectId}/sources/${reference.file}`}
          target="_blank"
          rel="noreferrer"
        >
          {reference.file.split("/").pop()}
        </a>
      )}
      <button
        className="icon-btn upload-labelled"
        disabled={busy}
        onClick={() => inputRef.current?.click()}
      >
        <PlusIcon size={13} />
        {busy
          ? "uploading…"
          : reference.file
            ? "replace source file"
            : "upload source file"}
      </button>
      {failed && <span className="ref-source-error">upload failed</span>}
      <input
        ref={inputRef}
        type="file"
        style={{ display: "none" }}
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) upload(file);
          e.target.value = "";
        }}
      />
    </div>
  );
}

/** Display name for a derived status: a claim that failed review is
 *  "flawed" — matching the review's own verdict word — not "refuted"
 *  (nothing proved it false). */
const statusLabel = (status: string) =>
  status === "refuted" ? "flawed" : status;

function StatusChip({ status, authority }: { status: string; authority?: string | null }) {
  return (
    <span className={`status-chip status-${status}`}>
      {statusLabel(status)}
      {authority ? ` · ${authority}` : ""}
    </span>
  );
}

function ClaimCard({
  claim,
  onOpen,
}: {
  claim: LedgerOverview["claims"][number];
  onOpen: (id: string) => void;
}) {
  return (
    <article className="claim-card">
      <button className="claim-card-open" onClick={() => onOpen(claim.claim_id)}>
        <div className="claim-head-row">
          <StatusChip status={claim.status} authority={claim.authority} />
          <span className="claim-id" title={claim.claim_id}>{claim.claim_id.slice(0, 12)}</span>
        </div>
        <div className="claim-statement">
          <TeX text={claim.statement_text} />
        </div>
      </button>
      {claim.reference && (
        <ReferenceCite reference={claim.reference} locator={claim.locator} />
      )}
    </article>
  );
}

function CopyId({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);
  return <button className="copy-id" title={value} onClick={() => {
    void navigator.clipboard.writeText(value).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1400);
    });
  }}>{copied ? "copied" : "copy id"}</button>;
}

const JOURNAL_LABELS: Record<string, string> = {
  "ledger-init": "Started the ledger",
  "reference-created": "Added a reference",
  "reference-refined": "Updated a reference",
  "claim-created": "Recorded a claim",
  "claim-refined": "Refined a claim",
  "claim-withdrawn": "Withdrew a claim",
  "claim-restored": "Restored a claim",
  "proof-added": "Added a proof",
  "proof-revised": "Revised a proof",
  "proof-abandoned": "Abandoned a proof",
  "proof-resumed": "Resumed a proof",
  "verification-added": "Added a verification",
  "evidence-repaired": "Repaired evidence display",
};

function humanTime(value: unknown): string {
  const date = new Date(String(value ?? ""));
  return Number.isNaN(date.valueOf())
    ? ""
    : date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export function Detail({
  projectId,
  id,
  version,
  onBack,
  loadClaim,
  onOpen = openLedgerEntry,
  renderHumanCheck,
}: {
  projectId: string;
  id: string;
  version: string;
  onBack: () => void;
  loadClaim?: (id: string) => Promise<ClaimDetail>;
  onOpen?: (id: string) => void;
  renderHumanCheck?: (objectId: string, revisionId: string) => ReactNode;
}) {
  const [detail, setDetail] = useState<ClaimDetail | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    setDetail(null); setError("");
    (loadClaim ? loadClaim(id) : fetchClaim(projectId, id))
      .then(value => { if (active) setDetail(value); })
      .catch(() => { if (active) setError("This mathematical record is unavailable. Check your access and retry."); });
    return () => { active = false; };
  }, [projectId, id, version, loadClaim]);
  if (error) return <p role="alert">{error}</p>;
  if (!detail) return <div className="empty">loading…</div>;
  const proofs = Object.entries(detail.proofs);
  return (
    <div>
      <button className="icon-btn detail-back" onClick={onBack}>
        ← all claims
      </button>
      <div className="claim-head-row">
        <StatusChip status={detail.status} authority={detail.authority} />
        <span className="claim-id" title={detail.claim_id}>{detail.claim_id}</span>
        <CopyId value={detail.claim_id} />
      </div>
      <div className="claim-statement" style={{ fontSize: "0.95rem" }}>
        <TeX text={detail.statement_text} />
      </div>
      {renderHumanCheck?.(detail.claim_id, detail.current_revision_id)}
      {detail.reference && (
        <ReferenceCite reference={detail.reference} locator={detail.locator} />
      )}

      {detail.dependencies && detail.dependencies.length > 0 && (
        <>
          <div className="detail-section">Depends on</div>
          {detail.dependencies.map((dep) => (
            <div key={dep.claim_id} className="premise-row">
              <StatusChip status={dep.status} authority={dep.authority} />
              <span
                className="claim-id ledger-link"
                onClick={() => onOpen(dep.claim_id)}
              >
                {dep.claim_id.slice(0, 12)}
              </span>
              <span className="dep-statement">
                <TeX text={dep.statement_text} />
              </span>
            </div>
          ))}
        </>
      )}

      <div className="detail-section" role="heading" aria-level={4}>Proof attempts</div>
      {proofs.length === 0 && <p className="muted">No proof attempts recorded.</p>}
      {proofs.map(([proofId, proof]) => (
        <div key={proofId} className="proof-card">
          <div className="proof-meta">
            <span className="claim-id">{proofId.slice(0, 12)}</span>
            <span className={`status-chip status-${proof.argument === "verified" ? "established" : proof.argument === "flawed" ? "refuted" : "conjectured"}`}>
              {proof.argument}
              {proof.argument_authority ? ` · ${proof.argument_authority}` : ""}
            </span>
            {proof.stands && <span className="status-chip status-established">stands</span>}
            {!proof.target_fresh && (
              <span className="status-chip status-pending">stale target</span>
            )}
            {proof.abandoned && (
              <span className="status-chip status-withdrawn">abandoned</span>
            )}
          </div>
          {proof.body && <Markdown text={proof.body.text} />}
          {renderHumanCheck?.(proofId, proof.revision_id)}
          {proof.premises.length > 0 && (
            <>
              <div className="detail-section">Premises</div>
              {proof.premises.map((premise, i) => (
                <div key={i} className="premise-row">
                  <span className={`premise-state premise-${premise.state}`}>
                    {premise.state}
                  </span>
                  {premise.unresolved ? (
                    <TeX text={premise.unresolved} />
                  ) : (
                    <span
                      className="claim-id ledger-link"
                      onClick={() =>
                        premise.claim_id && onOpen(premise.claim_id)
                      }
                    >
                      {premise.claim_id}
                    </span>
                  )}
                </div>
              ))}
            </>
          )}
        </div>
      ))}

      {detail.verifications && detail.verifications.length > 0 && (
        <>
          <div className="detail-section">Verification logs</div>
          {detail.verifications.map((v) => (
            <VerificationLog key={v.verification_id} v={v} />
          ))}
        </>
      )}

      <div className="detail-section">Revisions</div>
      {detail.revisions.map((rev) => (
        <div key={rev.revision_id} className="rev-row">
          <div className="claim-head-row">
            <span className="claim-id">{rev.revision_id.slice(0, 14)}</span>
            {rev.established && <span className="status-chip status-established">established</span>}
            {rev.refuted && <span className="status-chip status-refuted">flawed</span>}
            {rev.revision_id === detail.current_revision_id && (
              <span className="status-chip status-definition">current</span>
            )}
          </div>
          <TeX text={rev.statement_text} />
        </div>
      ))}
    </div>
  );
}

const formatSize = (bytes: number): string =>
  bytes >= 1 << 20
    ? `${(bytes / (1 << 20)).toFixed(1)}M`
    : bytes >= 1024
      ? `${Math.round(bytes / 1024)}k`
      : `${bytes}B`;

/** The project's shared artifacts directory: browse (click a name to
 *  open it in the viewer), rename/move (✎ edits the full relative
 *  path), and upload. */
function ArtifactsTab({
  projectId,
  version,
}: {
  projectId: string;
  version: string;
}) {
  const [artifacts, setArtifacts] = useState<ArtifactInfo[] | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState("");
  const [error, setError] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);

  const refresh = useCallback(() => {
    fetchArtifacts(projectId)
      .then((payload) => setArtifacts(payload.artifacts))
      .catch(() => {});
  }, [projectId]);
  useEffect(refresh, [refresh, version]);

  const artifactGroups = useMemo(() => {
    const normalizedQuery = query.trim().toLocaleLowerCase();
    const visible = (artifacts ?? []).filter((artifact) =>
      !normalizedQuery || artifact.path.toLocaleLowerCase().includes(normalizedQuery));
    return Object.entries(visible.reduce<Record<string, ArtifactInfo[]>>((groups, artifact) => {
      const slash = artifact.path.lastIndexOf("/");
      const directory = slash >= 0 ? artifact.path.slice(0, slash) : "Project files";
      (groups[directory] ??= []).push(artifact);
      return groups;
    }, {})).sort(([left], [right]) => left.localeCompare(right));
  }, [artifacts, query]);

  const commitRename = (source: string) => {
    const target = draft.trim().replace(/^artifacts\//, "");
    setEditing(null);
    if (!target || target === source) return;
    renameArtifact(projectId, source, target)
      .then(() => {
        setError(null);
        refresh();
      })
      .catch((e: Error) => setError(`rename failed (${e.message})`));
  };

  const upload = (files: FileList | null) => {
    if (!files || files.length === 0) return;
    Promise.allSettled(
      Array.from(files).map((file) => uploadArtifact(projectId, file)),
    ).then(() => refresh());
  };

  return (
    <div className="artifacts-tab">
      <div className="artifacts-bar">
        <input
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Search artifacts…"
          aria-label="Search artifacts"
        />
        <button className="icon-btn upload-labelled" onClick={() => fileRef.current?.click()}>
          <PlusIcon size={13} />
          upload
        </button>
        <input
          ref={fileRef}
          type="file"
          multiple
          hidden
          onChange={(e) => {
            upload(e.target.files);
            e.target.value = "";
          }}
        />
        {error && <span className="artifacts-error">{error}</span>}
      </div>
      {!artifacts ? (
        <div className="empty">loading…</div>
      ) : artifacts.length === 0 ? (
        <div className="empty">No artifacts yet. Attach a file in chat or upload one here.</div>
      ) : artifactGroups.length === 0 ? (
        <div className="empty">No artifacts match “{query}”.</div>
      ) : (
        artifactGroups.map(([directory, items]) => (
          <section className="artifact-group" key={directory}>
            <div className="artifact-group-heading"><span>{directory}</span><small>{items.length}</small></div>
            {items.map((artifact) => (
          <div key={artifact.path} className="artifact-row">
            {editing === artifact.path ? (
              <input
                className="artifact-rename"
                value={draft}
                autoFocus
                onChange={(e) => setDraft(e.target.value)}
                onBlur={() => commitRename(artifact.path)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") commitRename(artifact.path);
                  else if (e.key === "Escape") setEditing(null);
                }}
              />
            ) : (
              <>
                <span
                  className="artifact-name ledger-link"
                  onClick={() => openArtifact(artifact.path)}
                  title={artifact.path}
                >
                  {artifact.path.slice(artifact.path.lastIndexOf("/") + 1)}
                </span>
                <IconButton
                  icon="edit"
                  className="artifact-edit"
                  aria-label={`Rename or move ${artifact.path}`}
                  title="rename / move"
                  onClick={() => {
                    setEditing(artifact.path);
                    setDraft(artifact.path);
                  }}
                />
                <RevealArtifactButton
                  projectId={projectId}
                  name={artifact.path}
                  compact
                />
                <span className="artifact-meta">
                  {formatSize(artifact.size)}
                </span>
                <span
                  className="artifact-meta"
                  title={artifact.modified_at}
                >
                  {new Date(artifact.modified_at).toLocaleDateString([], {
                    month: "short",
                    day: "numeric",
                  })}
                </span>
              </>
            )}
          </div>
            ))}
          </section>
        ))
      )}
    </div>
  );
}

export function LedgerPanel({
  projectId, refreshKey, onMissing, onClose, hidden,
}: {
  projectId: string; refreshKey: number; onMissing?: () => void; onClose?: () => void; hidden?: boolean;
}) {
  const [binding, setBinding] = useState<OrganizationLedgerBinding | false | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController(); setBinding(null); setError("");
    void fetch(`/api/projects/${encodeURIComponent(projectId)}/ledger/organization`, { signal: controller.signal })
      .then(async (response) => {
        if (response.status === 404) onMissing?.();
        if (!response.ok) throw new Error("Ledger connection unavailable.");
        return response.json() as Promise<OrganizationLedgerBinding | { linked: false }>;
      }).then((value) => { if (!controller.signal.aborted) setBinding(value.linked ? value : false); })
      .catch((cause) => { if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : "Ledger connection unavailable."); });
    return () => controller.abort();
  }, [projectId, refreshKey, onMissing]);
  if (binding) return <OrganizationLedgerPanel projectId={projectId} binding={binding} hidden={hidden} onClose={onClose} />;
  if (binding === false) return <LocalLedgerPanel projectId={projectId} refreshKey={refreshKey} onMissing={onMissing} onClose={onClose} hidden={hidden} />;
  return <section id="ledger-panel" data-pane="ledger" className={`ledger-col ${hidden ? "pane-hidden" : ""}`}>
    <div className="panel-header"><span className="name">Ledger</span>{onClose && <button onClick={onClose} aria-label="Close Ledger panel">×</button>}</div>
    <p role={error ? "alert" : "status"}>{error || "Loading ledger connection…"}</p>
  </section>;
}

function LocalLedgerPanel({
  projectId,
  refreshKey,
  onMissing,
  onClose,
  hidden,
}: {
  projectId: string;
  refreshKey: number;
  onMissing?: () => void;
  onClose?: () => void;
  /** Mobile: pane stays mounted (event listeners, polling) but invisible. */
  hidden?: boolean;
}) {
  const [width, setWidth] = useState<number | null>(() => {
    const stored = Number(window.localStorage.getItem("reasoning-ledger-width"));
    return Number.isFinite(stored) && stored >= 320 ? stored : null;
  });
  const [resizing, setResizing] = useState(false);
  const [overview, setOverview] = useState<LedgerOverview | null>(null);
  // What the current overview corresponds to, for delta fetches: the
  // server diffs against this version and sends only changed entries.
  const overviewRef = useRef<LedgerOverview | null>(null);
  const overviewVersionRef = useRef<string | null>(null);
  const [selected, setSelectedState] = useState<string | null>(() =>
    window.location.hash.startsWith("#clm_")
      ? window.location.hash.slice(1)
      : null,
  );
  const setSelected = useCallback((id: string | null) => {
    setSelectedState(id);
    window.history.replaceState(null, "", id ? `#${id}` : "#");
  }, []);
  const [tab, setTab] = useState<
    "claims" | "refs" | "artifacts" | "graph" | "journal"
  >(() => (window.location.hash === "#graph" ? "graph" : "claims"));
  const [selectedRef, setSelectedRef] = useState<string | null>(null);
  const [journal, setJournal] = useState<Record<string, unknown>[]>([]);
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [graphExpanded, setGraphExpanded] = useState(false);

  const [version, setVersion] = useState("");

  const refresh = useCallback(() => {
    const since = overviewRef.current
      ? (overviewVersionRef.current ?? undefined)
      : undefined;
    fetchOverview(projectId, since)
      .then((payload) => {
        overviewVersionRef.current = payload.version;
        if ("unchanged" in payload) return;
        let next: LedgerOverview;
        if ("delta_since" in payload && overviewRef.current) {
          // merge: replace changed summaries, drop removed, keep the
          // rest — id-sorted, matching the full payload's order
          const { claims_changed, claims_removed, references_changed,
                  references_removed, version, delta_since, ...shared } =
            payload;
          const claims = new Map(
            overviewRef.current.claims.map((c) => [c.claim_id, c]),
          );
          for (const claim of claims_changed) claims.set(claim.claim_id, claim);
          for (const id of claims_removed) claims.delete(id);
          const references = new Map(
            overviewRef.current.references.map((r) => [r.reference_id, r]),
          );
          for (const ref of references_changed)
            references.set(ref.reference_id, ref);
          for (const id of references_removed) references.delete(id);
          next = {
            ...shared,
            claims: [...claims.keys()].sort().map((id) => claims.get(id)!),
            references: [...references.keys()]
              .sort()
              .map((id) => references.get(id)!),
          };
        } else if ("claims" in payload) {
          next = payload;
        } else {
          return;
        }
        overviewRef.current = next;
        setOverview(next);
      })
      .catch(() => {});
  }, [projectId]);

  useEffect(refresh, [refresh, refreshKey, version]);

  // Project switch: the held overview and journal belong to the old
  // project; drop them so nothing merges across projects.
  useEffect(() => {
    return () => {
      overviewRef.current = null;
      overviewVersionRef.current = null;
      setOverview(null);
      setJournal([]);
    };
  }, [projectId]);

  // The journal (append-only git history) is fetched only on its tab,
  // and incrementally: just the tail past what this window already has.
  const journalLengthRef = useRef(0);
  journalLengthRef.current = journal.length;
  useEffect(() => {
    if (tab !== "journal") return;
    const load = (after: number): Promise<void> =>
      fetchJournal(projectId, after).then((payload) => {
        if (payload.total < after) return load(0); // out of sync: refetch
        if (after === 0) setJournal(payload.entries);
        else if (payload.entries.length)
          setJournal((prev) => [
            ...prev.slice(0, after),
            ...payload.entries,
          ]);
      });
    load(journalLengthRef.current).catch(() => {});
  }, [tab, projectId, refreshKey, version]);

  // The overview fetch only refires when the ledger head changes, so a
  // failure (server mid-restart, dropped forward) would otherwise leave
  // the panel stuck on empty forever. Retry until it lands.
  useEffect(() => {
    if (overview) return;
    const timer = setInterval(refresh, 3000);
    return () => clearInterval(timer);
  }, [overview, refresh]);

  // Slugs clicked anywhere (chat, dock, premise rows) raise "ledger-open";
  // resolve the slug (possibly a truncated prefix or a proof id) to its
  // claim and show it.
  useEffect(() => {
    const handler = (event: Event) => {
      const slug = (event as CustomEvent<string>).detail;
      if (!slug) return;
      fetchResolve(projectId, slug)
        .then((resolved) => {
          if (resolved.reference_id) {
            setTab("refs");
            setSelectedRef(resolved.reference_id);
          } else if (resolved.claim_id) {
            setTab("claims");
            setSelected(resolved.claim_id);
          }
        })
        .catch(() => {});
    };
    window.addEventListener("ledger-open", handler);
    return () => window.removeEventListener("ledger-open", handler);
  }, [projectId, setSelected]);

  // Poll the cheap change token so the column tracks ledger writes from
  // any source (chat sessions, CLI sessions, anything else). A 404 means
  // the project no longer exists behind this origin (e.g. the server was
  // restarted on a different workbench root): stop polling and ask the
  // app to re-resolve its selection instead of retrying forever.
  useEffect(() => {
    let cancelled = false;
    const timer = setInterval(() => tick(), 2500);
    const tick = () =>
      fetchVersion(projectId)
        .then((payload) => {
          if (!cancelled) setVersion(payload.version);
        })
        .catch((error: unknown) => {
          if (!cancelled && error instanceof Error && error.message === "404") {
            cancelled = true;
            clearInterval(timer);
            onMissing?.();
          }
        });
    tick();
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [projectId, onMissing]);

  const unresolvedCount =
    overview?.unresolved_premises.reduce((n, u) => n + u.needs.length, 0) ?? 0;
  const normalizedQuery = query.trim().toLowerCase();
  const visibleClaims = (overview?.claims ?? []).filter((claim) =>
    (statusFilter === "all" || claim.status === statusFilter) &&
    (!normalizedQuery || [claim.statement_text, claim.claim_id, claim.reference?.title, claim.reference?.author]
      .some((value) => String(value ?? "").toLowerCase().includes(normalizedQuery))),
  );
  const visibleReferences = (overview?.references ?? []).filter((reference) =>
    !normalizedQuery || [reference.title, reference.author, reference.year, reference.reference_id]
      .some((value) => String(value ?? "").toLowerCase().includes(normalizedQuery)),
  );
  const visibleJournal = [...journal].reverse().filter((entry) =>
    !normalizedQuery || Object.values(entry).some((value) =>
      String(value ?? "").toLowerCase().includes(normalizedQuery)),
  );

  const beginResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    event.preventDefault();
    const panel = event.currentTarget.parentElement;
    if (!panel) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    const startX = event.clientX;
    const startWidth = panel.getBoundingClientRect().width;
    setResizing(true);
    const move = (next: PointerEvent) => {
      const maximum = Math.max(320, window.innerWidth * 0.75);
      const nextWidth = Math.min(maximum, Math.max(320, startWidth + startX - next.clientX));
      setWidth(nextWidth);
    };
    const finish = (next: PointerEvent) => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", finish);
      setResizing(false);
      const maximum = Math.max(320, window.innerWidth * 0.75);
      const nextWidth = Math.min(maximum, Math.max(320, startWidth + startX - next.clientX));
      window.localStorage.setItem("reasoning-ledger-width", String(Math.round(nextWidth)));
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", finish, { once: true });
  };
  const resizeWithKeyboard = (direction: number) => {
    const panel = document.getElementById("ledger-panel");
    const current = width ?? panel?.getBoundingClientRect().width ?? 480;
    const maximum = Math.max(320, window.innerWidth * 0.75);
    const next = Math.min(maximum, Math.max(320, current + direction * 24));
    setWidth(next);
    window.localStorage.setItem("reasoning-ledger-width", String(Math.round(next)));
  };

  return (
    <div
      id="ledger-panel"
      data-pane="ledger"
      className={`ledger-col ${tab === "graph" && width === null ? "ledger-col-wide" : ""} ${
        resizing ? "ledger-col-resizing" : ""
      } ${hidden ? "pane-hidden" : ""} ${tab === "graph" && graphExpanded ? "ledger-col-fullscreen" : ""}`}
      style={width === null || (tab === "graph" && graphExpanded) ? undefined : { width }}
    >
      <div
        className="ledger-resizer"
        role="separator"
        aria-label="Resize ledger pane"
        aria-orientation="vertical"
        tabIndex={0}
        onPointerDown={beginResize}
        onKeyDown={(event) => {
          if (event.key === "ArrowLeft") { event.preventDefault(); resizeWithKeyboard(1); }
          if (event.key === "ArrowRight") { event.preventDefault(); resizeWithKeyboard(-1); }
          if (event.key === "Home") { event.preventDefault(); setWidth(null); window.localStorage.removeItem("reasoning-ledger-width"); }
        }}
        onDoubleClick={() => {
          setWidth(null);
          window.localStorage.removeItem("reasoning-ledger-width");
        }}
      />
      <div className="panel-header">
        <span className="name">Ledger</span>
        <div className="tabs" role="tablist" aria-label="Ledger views">
          <button
            role="tab"
            aria-selected={tab === "claims"}
            className={`tab ${tab === "claims" ? "active" : ""}`}
            onClick={() => { setTab("claims"); setGraphExpanded(false); }}
          >
            claims
          </button>
          <button
            role="tab"
            aria-selected={tab === "refs"}
            className={`tab ${tab === "refs" ? "active" : ""}`}
            onClick={() => {
              setTab("refs");
              setSelectedRef(null);
              setGraphExpanded(false);
            }}
          >
            refs
          </button>
          <button
            role="tab"
            aria-selected={tab === "artifacts"}
            className={`tab ${tab === "artifacts" ? "active" : ""}`}
            onClick={() => { setTab("artifacts"); setGraphExpanded(false); }}
          >
            artifacts
          </button>
          <button
            role="tab"
            aria-selected={tab === "graph"}
            className={`tab ${tab === "graph" ? "active" : ""}`}
            onClick={() => setTab("graph")}
          >
            graph
          </button>
          <button
            role="tab"
            aria-selected={tab === "journal"}
            className={`tab ${tab === "journal" ? "active" : ""}`}
            onClick={() => { setTab("journal"); setGraphExpanded(false); }}
          >
            journal
          </button>
        </div>
        <span className="spacer" />
        {tab === "graph" && <button className="icon-btn" onClick={() => setGraphExpanded((value) => !value)}>{graphExpanded ? "exit full screen" : "full screen"}</button>}
        <button className="icon-btn" onClick={refresh}>
          refresh
        </button>
        {onClose && (
          <IconButton icon="close" className="ledger-close" onClick={onClose} aria-label="Close ledger" />
        )}
      </div>
      <div className={`panel-body ${tab === "graph" ? "panel-body-graph" : ""}`}>
        {tab === "graph" ? (
          <GraphView
            projectId={projectId}
            version={version}
            onOpen={(claimId) => {
              setSelected(claimId);
              setTab("claims");
              setGraphExpanded(false);
            }}
          />
        ) : tab === "artifacts" ? (
          <ArtifactsTab projectId={projectId} version={version} />
        ) : tab === "refs" ? (
          !overview ? (
            <div className="empty">loading…</div>
          ) : selectedRef ? (
            (() => {
              const reference = overview.references.find(
                (r) => r.reference_id === selectedRef,
              );
              return reference ? (
                <RefDetail
                  projectId={projectId}
                  reference={reference}
                  citing={overview.claims.filter(
                    (c) => c.reference_id === selectedRef,
                  )}
                  onBack={() => setSelectedRef(null)}
                  onOpenClaim={(id) => {
                    setTab("claims");
                    setSelected(id);
                  }}
                  onChanged={refresh}
                />
              ) : (
                <div className="empty">unknown reference</div>
              );
            })()
          ) : overview.references.length === 0 ? (
            <div className="empty">No references yet. Add a source in Library or cite one in chat.</div>
          ) : (
            <>
              <div className="ledger-filter-row"><input type="search" aria-label="Search references" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search title, author, year…" /></div>
              {visibleReferences.length ? (
                <ProgressiveList items={visibleReferences}>
                  {(reference) => (
                    <article
                      key={reference.reference_id}
                      className="claim-card reference-card"
                      onClick={(event) => {
                        if ((event.target as HTMLElement).closest("a, button")) return;
                        setSelectedRef(reference.reference_id);
                      }}
                    >
                      <div className="claim-head-row">
                        <span className="claim-id" title={reference.reference_id}>{reference.reference_id.slice(0, 12)}</span>
                        {reference.file && (
                          <span className="ref-has-file" title="source file cached">
                            📄
                          </span>
                        )}
                        <CopyId value={reference.reference_id} />
                      </div>
                      <ReferenceCite reference={reference} />
                    </article>
                  )}
                </ProgressiveList>
              ) : <div className="empty">no references match this search</div>}
            </>
          )
        ) : tab === "journal" ? (
          journal.length === 0 ? (
            <div className="empty">No journal entries yet. Research activity will be recorded here.</div>
          ) : (
            <>
              <div className="ledger-filter-row"><input type="search" aria-label="Search journal" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search ledger activity…" /></div>
              {visibleJournal.length ? (
                <ol className="journal-list">
                  <ProgressiveList items={visibleJournal} batch={300}>
                    {(entry, i) => {
                      const event = String(entry.event ?? "unknown");
                      const claimId = typeof entry.claim_id === "string" ? entry.claim_id : null;
                      const subject = claimId || (typeof entry.reference_id === "string" ? entry.reference_id : typeof entry.proof_id === "string" ? entry.proof_id : null);
                      return <li key={`${String(entry.at)}-${i}`} className="journal-row"><time>{humanTime(entry.at)}</time><div><strong>{JOURNAL_LABELS[event] ?? event.replaceAll("-", " ")}</strong>{subject && (claimId ? <button className="journal-subject" title={subject} onClick={() => { setSelected(claimId); setTab("claims"); }}>{subject.slice(0, 16)}</button> : <span className="journal-subject-static" title={subject}>{subject.slice(0, 16)}</span>)}</div></li>;
                    }}
                  </ProgressiveList>
                </ol>
              ) : <div className="empty">no activity matches this search</div>}
            </>
          )
        ) : selected ? (
          <Detail
            projectId={projectId}
            id={selected}
            version={version}
            onBack={() => setSelected(null)}
          />
        ) : !overview ? (
          <div className="empty">loading…</div>
        ) : (
          <>
            <div className="ledger-filter-row">
              <input type="search" aria-label="Search claims" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search claims or sources…" />
              <select aria-label="Filter claims by status" value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}><option value="all">all statuses</option>{Object.keys(overview.counts).sort().map((status) => <option key={status} value={status}>{status}</option>)}</select>
            </div>
            <div className="counts-row">
              {Object.entries(overview.counts).map(([status, count]) => (
                <span key={status} className={`status-chip status-${status}`}>
                  {count} {statusLabel(status)}
                </span>
              ))}
            </div>
            <div className="alerts">
              {overview.contradictions.length > 0 && (
                <div className="alert bad">
                  {overview.contradictions.length} contradiction(s)
                </div>
              )}
              {overview.cycles.length > 0 && (
                <div className="alert bad">{overview.cycles.length} cycle(s)</div>
              )}
              {unresolvedCount > 0 && (
                <div className="alert warn">
                  {unresolvedCount} unresolved premise(s) awaiting backfill
                </div>
              )}
              {overview.stale_proofs.length > 0 && (
                <div className="alert warn">
                  {overview.stale_proofs.length} stale proof(s)
                </div>
              )}
              {overview.broken_proofs.length > 0 && (
                <div className="alert bad">
                  {overview.broken_proofs.length} broken proof(s)
                </div>
              )}
            </div>
            {overview.claims.length === 0 ? (
              <div className="empty">No claims yet. Send a research question to begin building the ledger.</div>
            ) : visibleClaims.length === 0 ? (
              <div className="empty">no claims match these filters</div>
            ) : (
              <ProgressiveList items={visibleClaims}>
                {(claim) => (
                  <ClaimCard
                    key={claim.claim_id}
                    claim={claim}
                    onOpen={setSelected}
                  />
                )}
              </ProgressiveList>
            )}
          </>
        )}
      </div>
    </div>
  );
}
