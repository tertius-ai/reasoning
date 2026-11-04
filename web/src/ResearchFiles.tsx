import React, { useEffect, useRef, useState } from "react";
import { ApiError, aborted, errorMessage, request, topicPath } from "./organization-api";
import { ResearchMarkdown } from "./MathematicsBrowser";
import { AttachmentButton, MediaScope, type MediaFile } from "./TopicMedia";
type File = { id: string; path: string; revision?: number; head_revision: number; sha256: string; text?: string; truncated?: boolean;
  total_characters?: number;
  url?: string; download_url?: string };
type Kind = "all" | "text" | "pdfs" | "pictures";
export type RetainedFile = { event_sequence: number; tool_index: number; path: string; recovery_path: string;
  sha256: string; size_bytes: number };
export type FileDraft = { draft_id: string; path: string; size_bytes: number };
const MAX_FILE_BYTES = 256 * 1024;
const isPicture = (file: MediaFile) => file.mime.startsWith("image/");
const extension = (path: string) => (path.match(/\.([a-z0-9]+)$/i)?.[1] ?? "txt").slice(0, 3).toUpperCase();
const kibibytes = (bytes: number) => `${Math.ceil(bytes / 1024).toLocaleString()} KiB`;
const sourceText = async (url: string, signal: AbortSignal) => {
  const response = await fetch(url, { credentials: "same-origin", signal });
  if (!response.ok) throw new ApiError(response.status, "The saved revision is unavailable; refresh your access and try again.");
  return response.text();
};

/** Unfinished source is distinct from a published revision and never silently replayed. */
export function FileRecovery({ retained, drafts, root, canWrite, busy, recover }: {
  retained: RetainedFile[]; drafts: FileDraft[]; root: string; canWrite: boolean; busy: boolean;
  recover: (file: RetainedFile) => void;
}) {
  return <>
    {!!retained.length && <section aria-label="Recoverable files"><h4>Recoverable files</h4>
      <p className="org-help">These failed runs retained file source. Recover a copy without another model call. The source may need review.</p>
      <ul className="org-project-files-list">{retained.map(file => <li key={`${file.event_sequence}-${file.tool_index}`}>
        <span className="org-project-files-text"><strong>{file.path}</strong><small>Retained source · {kibibytes(file.size_bytes)}</small></span>
        {canWrite && <button type="button" disabled={busy} onClick={() => recover(file)}>Recover a copy</button>}
      </li>)}</ul></section>}
    {!!drafts.length && <section aria-label="Unfinished file drafts"><h4>Unfinished drafts</h4>
      <p className="org-help">Saved sections survive interruption. An agent can resume the draft; this download contains only the sections saved so far.</p>
      <ul className="org-project-files-list">{drafts.map(file => <li key={file.draft_id}>
        <span className="org-project-files-text"><strong>{file.path}</strong><small>Unfinished · {kibibytes(file.size_bytes)}</small></span>
        <a href={`/api${root}/drafts/${encodeURIComponent(file.draft_id)}/content`} download>Download unfinished source</a>
      </li>)}</ul></section>}
  </>;
}

/** A topic's saved text files, PDFs and pictures: filtered by kind and name, and each opened in place. */
type ResearchFilesProps = {
  organizationId: string; topicId: string; canWrite: boolean; onAuthLost: () => void;
  /** A saved revision to show first, such as one a reply's file chip opened. */
  initialFile?: { path: string; revision: number } | null;
};
export function ResearchFiles(props: ResearchFilesProps) {
  return <ResearchFilesView key={props.organizationId + ":" + props.topicId} {...props} />;
}

function ResearchFilesView({ organizationId, topicId, canWrite, onAuthLost, initialFile }: ResearchFilesProps) {
  const [files, setFiles] = useState<File[]>([]), [selected, setSelected] = useState<File | null>(null);
  const [media, setMedia] = useState<MediaFile[]>([]), [selectedMedia, setSelectedMedia] = useState<MediaFile | null>(null);
  const [name, setName] = useState(""), [text, setText] = useState(""), [editing, setEditing] = useState(false);
  const [error, setError] = useState(""), [busy, setBusy] = useState(false), [refresh, setRefresh] = useState(0);
  const [kind, setKind] = useState<Kind>("all"), [query, setQuery] = useState("");
  const [retained, setRetained] = useState<RetainedFile[]>([]), [drafts, setDrafts] = useState<FileDraft[]>([]);
  const active = useRef(true), action = useRef({ generation: 0, controller: null as AbortController | null });
  const root = topicPath(organizationId, topicId) + "/research-files";
  const invalidate = () => { action.current.generation += 1; action.current.controller?.abort(); action.current.controller = null; };
  const leaveAction = () => { invalidate(); setBusy(false); };
  const beginAction = () => {
    invalidate();
    const generation = action.current.generation, controller = new AbortController();
    action.current.controller = controller; setBusy(true);
    const current = () => active.current && action.current.generation === generation;
    return { signal: controller.signal, current, finish: () => { if (current()) setBusy(false); } };
  };
  useEffect(() => { active.current = true; return () => { active.current = false; invalidate(); }; }, []);
  const fail = (cause: unknown) => { if (cause instanceof ApiError && cause.status === 401) onAuthLost(); else setError(errorMessage(cause)); };
  useEffect(() => { const controller = new AbortController(); void request<File[]>(root, { signal: controller.signal }).then(setFiles)
    .catch((cause) => { if (!aborted(cause)) fail(cause); }); return () => controller.abort(); }, [root, refresh]);
  useEffect(() => { const controller = new AbortController(); void Promise.all([
    request<RetainedFile[]>(root + "/recovery", { signal: controller.signal }),
    request<FileDraft[]>(root + "/drafts", { signal: controller.signal }),
  ]).then(([files, progress]) => { setRetained(files); setDrafts(progress); })
    .catch(cause => { if (!aborted(cause)) fail(cause); }); return () => controller.abort(); }, [root, refresh]);
  useEffect(() => { const controller = new AbortController(); void request<MediaFile[]>(topicPath(organizationId, topicId) + "/media", { signal: controller.signal }).then(setMedia)
    .catch((cause) => { if (!aborted(cause)) fail(cause); }); return () => controller.abort(); }, [organizationId, topicId, refresh]);
  const open = (file: { path: string; revision?: number }) => {
    const operation = beginAction();
    setSelectedMedia(null); setError("");
    void request<File>(root + "/read?path=" + encodeURIComponent(file.path) + (file.revision ? "&revision=" + file.revision : ""),
      { signal: operation.signal }).then((value) => {
        if (!operation.current()) return;
        setSelected(value); setText(value.text ?? ""); setName(value.path); setEditing(false);
      }).catch(cause => { if (operation.current() && !aborted(cause)) fail(cause); }).finally(operation.finish);
  };
  // Runs once: the dialog keys this component by the file it opens first.
  useEffect(() => { if (initialFile) open(initialFile); }, []);
  const back = () => { leaveAction(); setSelected(null); setSelectedMedia(null); setEditing(false); setError(""); };
  const pictures = media.filter(isPicture), pdfs = media.filter(file => !isPicture(file)), needle = query.trim().toLocaleLowerCase();
  const shownText = kind === "all" || kind === "text" ? files.filter(file => file.path.toLocaleLowerCase().includes(needle)) : [];
  const shownMedia = (kind === "all" ? media : kind === "pdfs" ? pdfs : kind === "pictures" ? pictures : [])
    .filter(file => file.name.toLocaleLowerCase().includes(needle));
  const viewing = editing || !!selected || !!selectedMedia;
  const textBytes = new TextEncoder().encode(text).length;
  return <MediaScope.Provider value={{ organizationId, topicId, canWrite }}><section className="org-mathematics org-project-files" aria-label="Research files"><header><h3>Project files</h3>
    {viewing ? <button type="button" onClick={back}>‹ All files</button> : <span className="org-topic-actions">{canWrite && <AttachmentButton
      organizationId={organizationId} topicId={topicId} onError={fail} onInsert={() => setRefresh(value => value + 1)} />}{canWrite && <button onClick={() => {
      leaveAction(); setSelectedMedia(null); setSelected(null); setName(""); setText(""); setEditing(true); setError("");
    }}>New file</button>}</span>}</header>{error && <p role="alert" className="org-error">{error}</p>}
    {!viewing && <>
      <nav className="org-project-files-kinds" aria-label="Kind of file">
        {([["all", "All", files.length + media.length], ["text", "Text", files.length], ["pdfs", "PDFs", pdfs.length], ["pictures", "Pictures", pictures.length]] as const)
          .map(([value, label, count]) => <button key={value} type="button" aria-pressed={kind === value} onClick={() => setKind(value)}>{label} <span>{count}</span></button>)}
      </nav>
      <input type="search" aria-label="Search file names" placeholder="Search file names…" value={query} onChange={event => setQuery(event.target.value)} />
      {(kind === "all" || kind === "text") && <FileRecovery retained={retained.filter(file => file.path.toLocaleLowerCase().includes(needle))}
        drafts={drafts.filter(file => file.path.toLocaleLowerCase().includes(needle))} root={root} canWrite={canWrite} busy={busy} recover={file => {
          const operation = beginAction(); setError("");
          void request<File>(root + "/recovery", { method: "POST", body: { event_sequence: file.event_sequence, tool_index: file.tool_index,
            sha256: file.sha256, path: file.recovery_path } }).then(saved => {
              if (!active.current) return;
              setRefresh(value => value + 1); if (operation.current()) open(saved);
            }).catch(cause => { if (operation.current()) fail(cause); }).finally(operation.finish);
        }} />}
      {shownText.length || shownMedia.length ? <ul className="org-project-files-list">
        {shownText.map(file => <li key={file.id}><span className="org-project-file-icon" aria-hidden="true">{extension(file.path)}</span>
          <span className="org-project-files-text"><strong>{file.path}</strong><small>Text · revision {file.head_revision}</small></span>
          <button type="button" aria-label={`Open ${file.path}`} onClick={() => open(file)}>Open</button></li>)}
        {shownMedia.map(file => <li key={file.id}>{isPicture(file) ? <img src={file.url} alt="" loading="lazy" referrerPolicy="no-referrer" />
          : <span className="org-project-file-icon" data-kind="pdf" aria-hidden="true">PDF</span>}
          <span className="org-project-files-text"><strong>{file.name}</strong><small>{isPicture(file) ? "Picture" : "PDF"} · {kibibytes(file.size_bytes)}</small></span>
          <button type="button" aria-label={`Open ${file.name}`} onClick={() => { leaveAction(); setSelectedMedia(file); setSelected(null); setEditing(false); setError(""); }}>Open</button></li>)}
      </ul> : <p className="org-help">{files.length || media.length ? "No files match." : "Notes, proofs, pictures and PDFs appear here."}</p>}
    </>}
    {viewing && <article>{editing ? <form onSubmit={(event) => { event.preventDefault(); if (busy || textBytes > MAX_FILE_BYTES) return;
        const operation = beginAction(), source = text; setError("");
        void request<File>(root, { method: "POST", body: { path: name, text: source, expected_sha256: selected?.sha256 ?? null } })
          .then((file) => {
            if (!active.current) return;
            setRefresh(value => value + 1);
            if (!operation.current()) return;
            setSelected({ ...file, head_revision: file.revision ?? file.head_revision, text: source });
            setName(file.path); setText(source); setEditing(false);
          }).catch(cause => { if (operation.current()) fail(cause); }).finally(operation.finish);
      }}><label>Filename<input required value={name} disabled={busy || !!selected} maxLength={200} onChange={(event) => setName(event.target.value)} /></label>
        <label>Content<textarea rows={16} value={text} disabled={busy} onChange={(event) => setText(event.target.value)} /></label>
        {textBytes > MAX_FILE_BYTES && <p role="alert">The editor accepts at most 256 KiB of UTF-8 text. Save larger documents in sections with the agent's file tools.</p>}
        <button disabled={busy || textBytes > MAX_FILE_BYTES}>{busy ? "Saving…" : "Save revision"}</button><button type="button" disabled={busy} onClick={() => {
          if (!selected) { back(); return; }
          leaveAction(); setText(selected.text ?? ""); setEditing(false);
        }}>Cancel</button>
      </form> : selectedMedia ? <><header><strong>{selectedMedia.name}</strong></header><ResearchMarkdown>{selectedMedia.markdown}</ResearchMarkdown>
        <a href={selectedMedia.url} target="_blank" rel="noreferrer">Open original file</a> <a href={selectedMedia.url} download={selectedMedia.name}>Download {selectedMedia.name}</a>
        <small>SHA-256 {selectedMedia.sha256.slice(0, 12)} · {kibibytes(selectedMedia.size_bytes)}</small></> : selected && <><header><strong>{selected.path}</strong>{canWrite && (selected.total_characters ?? text.length) <= MAX_FILE_BYTES && (selected.revision ?? selected.head_revision) === selected.head_revision
          && <button disabled={busy} onClick={() => {
            if (!selected.truncated) { leaveAction(); setText(selected.text ?? ""); setName(selected.path); setEditing(true); return; }
            if (!selected.url) return;
            const operation = beginAction(); setError("");
            void sourceText(selected.url, operation.signal).then(value => {
              if (!operation.current()) return;
              if (new TextEncoder().encode(value).length > MAX_FILE_BYTES) throw new Error("The editor accepts at most 256 KiB of UTF-8 text. Use the agent's file tools to revise a larger file.");
              setText(value); setEditing(true);
            }).catch(cause => { if (operation.current() && !aborted(cause)) fail(cause); }).finally(operation.finish);
          }}>Edit</button>}</header>
        {selected.url && <div className="org-topic-actions"><a href={selected.url} target="_blank" rel="noreferrer">Open source</a>
          <a href={selected.download_url ?? selected.url} download={selected.path.split("/").at(-1)}>Download complete file</a>
          <button disabled={busy} onClick={() => { const operation = beginAction(); setError("");
            void sourceText(selected.url!, operation.signal).then(value => {
              if (operation.current()) return navigator.clipboard.writeText(value);
            }).catch(cause => { if (operation.current() && !aborted(cause)) fail(cause); }).finally(operation.finish);
          }}>Copy complete source</button></div>}
        {selected.truncated && <p className="org-help">Showing a bounded preview. Open, copy, or download retrieves the complete saved revision.</p>}
        {/\.(tex|bib|txt|py|sage|lean)$/i.test(selected.path) ? <pre aria-label="File source">{selected.text ?? ""}</pre>
          : <ResearchMarkdown>{selected.text ?? ""}</ResearchMarkdown>}<small>Revision {selected.revision ?? selected.head_revision} · {selected.sha256.slice(0, 12)}</small></>}</article>}
  </section></MediaScope.Provider>;
}
