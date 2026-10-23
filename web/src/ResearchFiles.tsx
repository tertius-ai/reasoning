import React, { useEffect, useState } from "react";
import { ApiError, aborted, errorMessage, request, topicPath } from "./organization-api";
import { ResearchMarkdown } from "./MathematicsBrowser";
import { AttachmentButton, MediaScope, type MediaFile } from "./TopicMedia";
type File = { id: string; path: string; revision?: number; head_revision: number; sha256: string; text?: string; truncated?: boolean;
  url?: string; download_url?: string };
export function ResearchFiles({ organizationId, topicId, canWrite, onAuthLost }: {
  organizationId: string; topicId: string; canWrite: boolean; onAuthLost: () => void;
}) {
  const [files, setFiles] = useState<File[]>([]), [selected, setSelected] = useState<File | null>(null);
  const [media, setMedia] = useState<MediaFile[]>([]), [selectedMedia, setSelectedMedia] = useState<MediaFile | null>(null);
  const [name, setName] = useState(""), [text, setText] = useState(""), [editing, setEditing] = useState(false);
  const [error, setError] = useState(""), [busy, setBusy] = useState(false), [refresh, setRefresh] = useState(0);
  const root = topicPath(organizationId, topicId) + "/research-files";
  const fail = (cause: unknown) => { if (cause instanceof ApiError && cause.status === 401) onAuthLost(); else setError(errorMessage(cause)); };
  useEffect(() => { const controller = new AbortController(); void request<File[]>(root, { signal: controller.signal }).then(setFiles)
    .catch((cause) => { if (!aborted(cause)) fail(cause); }); return () => controller.abort(); }, [root, refresh]);
  useEffect(() => { const controller = new AbortController(); void request<MediaFile[]>(topicPath(organizationId, topicId) + "/media", { signal: controller.signal }).then(setMedia)
    .catch((cause) => { if (!aborted(cause)) fail(cause); }); return () => controller.abort(); }, [organizationId, topicId, refresh]);
  return <MediaScope.Provider value={{ organizationId, topicId, canWrite }}><section className="org-mathematics" aria-label="Research files"><header><h3>Project files</h3>{canWrite && <AttachmentButton
    organizationId={organizationId} topicId={topicId} onError={fail} onInsert={() => setRefresh(value => value + 1)} />}{canWrite && <button onClick={() => {
    setSelectedMedia(null); setSelected(null); setName(""); setText(""); setEditing(true); setError("");
  }}>New file</button>}</header>{error && <p role="alert" className="org-error">{error}</p>}
    <div className="org-mathematics-layout"><div className="org-mathematics-list">{files.map((file) => <button key={file.id} onClick={() => {
      setSelectedMedia(null); void request<File>(root + "/read?path=" + encodeURIComponent(file.path)).then((value) => { setSelected(value); setText(value.text ?? ""); setName(value.path); setEditing(false); }).catch(fail);
    }}>{file.path}<small> · revision {file.head_revision}</small></button>)}{media.map(file => <button key={file.id} onClick={() => {
      setSelectedMedia(file); setSelected(null); setEditing(false);
    }}>{file.name}<small> · {file.mime === "application/pdf" ? "PDF" : "Picture"}</small></button>)}{!files.length && !media.length && <p className="org-help">Notes, proofs, pictures and PDFs appear here.</p>}</div>
      <article>{editing ? <form onSubmit={(event) => { event.preventDefault(); setBusy(true); setError("");
        void request<File>(root, { method: "POST", body: { path: name, text, expected_sha256: selected?.sha256 ?? null } })
          .then((file) => { setSelected({ ...file, text }); setEditing(false); setRefresh(refresh + 1); }).catch(fail).finally(() => setBusy(false));
      }}><label>Filename<input required value={name} disabled={busy || !!selected} maxLength={200} onChange={(event) => setName(event.target.value)} /></label>
        <label>Content<textarea rows={16} value={text} maxLength={64000} onChange={(event) => setText(event.target.value)} /></label>
        <button disabled={busy}>{busy ? "Saving…" : "Save revision"}</button><button type="button" disabled={busy} onClick={() => setEditing(false)}>Cancel</button>
      </form> : selectedMedia ? <><ResearchMarkdown>{selectedMedia.markdown}</ResearchMarkdown>
        <a href={selectedMedia.url} target="_blank" rel="noreferrer">Open original file</a> <a href={selectedMedia.url} download={selectedMedia.name}>Download {selectedMedia.name}</a>
        <small>SHA-256 {selectedMedia.sha256.slice(0, 12)} · {Math.ceil(selectedMedia.size_bytes / 1024)} KiB</small></> : selected ? <><header><strong>{selected.path}</strong>{canWrite && !selected.truncated && <button onClick={() => setEditing(true)}>Edit</button>}</header>
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
          : <ResearchMarkdown>{selected.text ?? ""}</ResearchMarkdown>}<small>Revision {selected.revision ?? selected.head_revision} · {selected.sha256.slice(0, 12)}</small></>
        : <p className="org-help">Select a file to read or edit it. Every saved revision is retained.</p>}</article></div>
  </section></MediaScope.Provider>;
}
