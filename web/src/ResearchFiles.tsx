import React, { useEffect, useState } from "react";
import { ApiError, aborted, errorMessage, request, topicPath } from "./organization-api";
import { ResearchMarkdown } from "./MathematicsBrowser";
import { AttachmentButton, MediaScope, type MediaFile } from "./TopicMedia";
type File = { id: string; path: string; revision?: number; head_revision: number; sha256: string; text?: string; truncated?: boolean;
  url?: string; download_url?: string };
type Kind = "all" | "text" | "pdfs" | "pictures";
const isPicture = (file: MediaFile) => file.mime.startsWith("image/");
const extension = (path: string) => (path.match(/\.([a-z0-9]+)$/i)?.[1] ?? "txt").slice(0, 3).toUpperCase();
const kibibytes = (bytes: number) => `${Math.ceil(bytes / 1024).toLocaleString()} KiB`;

/** A topic's saved text files, PDFs and pictures: filtered by kind and name, and each opened in place. */
export function ResearchFiles({ organizationId, topicId, canWrite, onAuthLost }: {
  organizationId: string; topicId: string; canWrite: boolean; onAuthLost: () => void;
}) {
  const [files, setFiles] = useState<File[]>([]), [selected, setSelected] = useState<File | null>(null);
  const [media, setMedia] = useState<MediaFile[]>([]), [selectedMedia, setSelectedMedia] = useState<MediaFile | null>(null);
  const [name, setName] = useState(""), [text, setText] = useState(""), [editing, setEditing] = useState(false);
  const [error, setError] = useState(""), [busy, setBusy] = useState(false), [refresh, setRefresh] = useState(0);
  const [kind, setKind] = useState<Kind>("all"), [query, setQuery] = useState("");
  const root = topicPath(organizationId, topicId) + "/research-files";
  const fail = (cause: unknown) => { if (cause instanceof ApiError && cause.status === 401) onAuthLost(); else setError(errorMessage(cause)); };
  useEffect(() => { const controller = new AbortController(); void request<File[]>(root, { signal: controller.signal }).then(setFiles)
    .catch((cause) => { if (!aborted(cause)) fail(cause); }); return () => controller.abort(); }, [root, refresh]);
  useEffect(() => { const controller = new AbortController(); void request<MediaFile[]>(topicPath(organizationId, topicId) + "/media", { signal: controller.signal }).then(setMedia)
    .catch((cause) => { if (!aborted(cause)) fail(cause); }); return () => controller.abort(); }, [organizationId, topicId, refresh]);
  const open = (file: File) => {
    setSelectedMedia(null); setError("");
    void request<File>(root + "/read?path=" + encodeURIComponent(file.path)).then((value) => { setSelected(value); setText(value.text ?? ""); setName(value.path); setEditing(false); }).catch(fail);
  };
  const back = () => { setSelected(null); setSelectedMedia(null); setEditing(false); setError(""); };
  const pictures = media.filter(isPicture), pdfs = media.filter(file => !isPicture(file)), needle = query.trim().toLocaleLowerCase();
  const shownText = kind === "all" || kind === "text" ? files.filter(file => file.path.toLocaleLowerCase().includes(needle)) : [];
  const shownMedia = (kind === "all" ? media : kind === "pdfs" ? pdfs : kind === "pictures" ? pictures : [])
    .filter(file => file.name.toLocaleLowerCase().includes(needle));
  const viewing = editing || !!selected || !!selectedMedia;
  return <MediaScope.Provider value={{ organizationId, topicId, canWrite }}><section className="org-mathematics org-project-files" aria-label="Research files"><header><h3>Project files</h3>
    {viewing ? <button type="button" onClick={back}>‹ All files</button> : <span className="org-topic-actions">{canWrite && <AttachmentButton
      organizationId={organizationId} topicId={topicId} onError={fail} onInsert={() => setRefresh(value => value + 1)} />}{canWrite && <button onClick={() => {
      setSelectedMedia(null); setSelected(null); setName(""); setText(""); setEditing(true); setError("");
    }}>New file</button>}</span>}</header>{error && <p role="alert" className="org-error">{error}</p>}
    {!viewing && <>
      <nav className="org-project-files-kinds" aria-label="Kind of file">
        {([["all", "All", files.length + media.length], ["text", "Text", files.length], ["pdfs", "PDFs", pdfs.length], ["pictures", "Pictures", pictures.length]] as const)
          .map(([value, label, count]) => <button key={value} type="button" aria-pressed={kind === value} onClick={() => setKind(value)}>{label} <span>{count}</span></button>)}
      </nav>
      <input type="search" aria-label="Search file names" placeholder="Search file names…" value={query} onChange={event => setQuery(event.target.value)} />
      {shownText.length || shownMedia.length ? <ul className="org-project-files-list">
        {shownText.map(file => <li key={file.id}><span className="org-project-file-icon" aria-hidden="true">{extension(file.path)}</span>
          <span className="org-project-files-text"><strong>{file.path}</strong><small>Text · revision {file.head_revision}</small></span>
          <button type="button" aria-label={`Open ${file.path}`} onClick={() => open(file)}>Open</button></li>)}
        {shownMedia.map(file => <li key={file.id}>{isPicture(file) ? <img src={file.url} alt="" loading="lazy" referrerPolicy="no-referrer" />
          : <span className="org-project-file-icon" data-kind="pdf" aria-hidden="true">PDF</span>}
          <span className="org-project-files-text"><strong>{file.name}</strong><small>{isPicture(file) ? "Picture" : "PDF"} · {kibibytes(file.size_bytes)}</small></span>
          <button type="button" aria-label={`Open ${file.name}`} onClick={() => { setSelectedMedia(file); setSelected(null); setEditing(false); setError(""); }}>Open</button></li>)}
      </ul> : <p className="org-help">{files.length || media.length ? "No files match." : "Notes, proofs, pictures and PDFs appear here."}</p>}
    </>}
    {viewing && <article>{editing ? <form onSubmit={(event) => { event.preventDefault(); setBusy(true); setError("");
        void request<File>(root, { method: "POST", body: { path: name, text, expected_sha256: selected?.sha256 ?? null } })
          .then((file) => { setSelected({ ...file, text }); setEditing(false); setRefresh(refresh + 1); }).catch(fail).finally(() => setBusy(false));
      }}><label>Filename<input required value={name} disabled={busy || !!selected} maxLength={200} onChange={(event) => setName(event.target.value)} /></label>
        <label>Content<textarea rows={16} value={text} maxLength={64000} onChange={(event) => setText(event.target.value)} /></label>
        <button disabled={busy}>{busy ? "Saving…" : "Save revision"}</button><button type="button" disabled={busy} onClick={() => selected ? setEditing(false) : back()}>Cancel</button>
      </form> : selectedMedia ? <><header><strong>{selectedMedia.name}</strong></header><ResearchMarkdown>{selectedMedia.markdown}</ResearchMarkdown>
        <a href={selectedMedia.url} target="_blank" rel="noreferrer">Open original file</a> <a href={selectedMedia.url} download={selectedMedia.name}>Download {selectedMedia.name}</a>
        <small>SHA-256 {selectedMedia.sha256.slice(0, 12)} · {kibibytes(selectedMedia.size_bytes)}</small></> : selected && <><header><strong>{selected.path}</strong>{canWrite && !selected.truncated && <button onClick={() => setEditing(true)}>Edit</button>}</header>
        {selected.url && <div className="org-topic-actions"><a href={selected.url} target="_blank" rel="noreferrer">Open source</a>
          <a href={selected.download_url ?? selected.url} download={selected.path.split("/").at(-1)}>Download complete file</a>
          <button disabled={busy} onClick={() => { setBusy(true); setError("");
            void fetch(selected.url!, { credentials: "same-origin" }).then(async response => {
              if (!response.ok) throw new Error("The saved revision is unavailable; refresh your access and try again.");
              await navigator.clipboard.writeText(await response.text());
            }).catch(fail).finally(() => setBusy(false));
          }}>Copy complete source</button></div>}
        {selected.truncated && <p className="org-help">Showing a bounded preview. Open, copy, or download retrieves the complete saved revision.</p>}
        {/\.(tex|bib|txt|py|sage|lean)$/i.test(selected.path) ? <pre aria-label="File source">{selected.text ?? ""}</pre>
          : <ResearchMarkdown>{selected.text ?? ""}</ResearchMarkdown>}<small>Revision {selected.revision ?? selected.head_revision} · {selected.sha256.slice(0, 12)}</small></>}</article>}
  </section></MediaScope.Provider>;
}
