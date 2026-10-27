// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import React, { lazy, Suspense, useEffect, useRef, useState } from "react";
import { request } from "./organization-api";
import { PlusIcon } from "./ComposerIcons";
import { ATTACHMENT_ACCEPT, MARKDOWN_TYPE, MarkdownPreview, attachmentMime, uploadAttachment } from "./TopicMedia";
import "./topic-media.css";

const PdfViewer = lazy(() => import("./PdfViewer").then(module => ({ default: module.PdfViewer })));
export const MAX_DIRECT_ATTACHMENTS = 10;
export type DirectFile = { id: string; name: string; mime: string; sha256: string; size_bytes: number; url: string; markdown: string };
export type DirectReference = { name: string; url: string };
/** A file chosen in the composer: uploading until it has `markdown`, or failed with `error`. */
export type PendingFile = { key: string; name: string; mime: string; preview?: string; markdown?: string; error?: string };

const LINE = /^!\[([^\]\n]*)\]\((\/api\/organizations\/[a-f0-9]{32}\/direct-conversations\/[a-f0-9]{32}\/media\/[a-f0-9]{32}\/[a-f0-9]{64}\/content)\)$/;

/** A message's files (lines that are exactly a reference to this conversation's media) and the caption around them. */
export function splitAttachments(content: string, root: string): { files: DirectReference[]; caption: string } {
  const files: DirectReference[] = [], rest: string[] = [];
  for (const line of content.split("\n")) {
    const match = LINE.exec(line.trim());
    if (match && match[2].startsWith("/api" + root + "/media/")) files.push({ name: match[1], url: match[2] });
    else rest.push(line);
  }
  return { files, caption: files.length ? rest.join("\n").trim() : content };
}

export const joinAttachments = (references: string[], caption: string) =>
  [references.join("\n"), caption.trim()].filter(Boolean).join("\n\n");

export const uploadDirectFile = (file: File, root: string) => uploadAttachment<DirectFile>(file, root + "/media");

const metadata = new Map<string, Promise<DirectFile>>();
function useDirectFile(url: string) {
  const [file, setFile] = useState<DirectFile | null>(null), [failed, setFailed] = useState(false);
  useEffect(() => {
    let current = true;
    if (!metadata.has(url)) {
      const pending = request<DirectFile>(url.replace(/^\/api/, "").replace(/\/content$/, ""));
      pending.catch(() => metadata.delete(url));
      metadata.set(url, pending);
    }
    void metadata.get(url)!.then(value => { if (current) setFile(value); }).catch(() => { if (current) setFailed(true); });
    return () => { current = false; };
  }, [url]);
  return { file, failed };
}

export const fileSize = (bytes: number) => bytes < 1024 * 1024 ? Math.max(1, Math.round(bytes / 1024)) + " KB" : (bytes / 1024 / 1024).toFixed(1) + " MB";
const isImage = (mime: string) => mime.startsWith("image/");

function DocumentRow({ reference }: { reference: DirectReference }) {
  const { file, failed } = useDirectFile(reference.url), [open, setOpen] = useState(false);
  const markdown = file?.mime === MARKDOWN_TYPE;
  return <div className="direct-file">
    <div className="direct-file-row"><span className="direct-file-icon" aria-hidden="true">{markdown ? "MD" : "PDF"}</span>
      <span className="direct-file-text"><strong>{reference.name}</strong>
        <small>{failed ? "Unavailable" : file ? fileSize(file.size_bytes) : "…"}
          {!failed && <> · <button type="button" className="direct-file-link" aria-expanded={open} onClick={() => setOpen(!open)}>{open ? "Close" : "Open"}</button>
            {" · "}<a className="direct-file-link" href={reference.url + "?download=true"} download={reference.name}>Download</a></>}</small></span></div>
    {open && <div className="topic-pdf direct-file-viewer">{markdown ? <MarkdownPreview url={reference.url} />
      : <Suspense fallback={<span role="status">Loading PDF…</span>}><PdfViewer url={reference.url} name={reference.name} /></Suspense>}</div>}
  </div>;
}

function Lightbox({ images, index, onIndex, onClose }: { images: DirectReference[]; index: number; onIndex: (index: number) => void; onClose: () => void }) {
  const dialog = useRef<HTMLDivElement>(null), image = images[index];
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    dialog.current?.focus();
    return () => previous?.focus();
  }, []);
  const step = (delta: number) => onIndex((index + delta + images.length) % images.length);
  return <div className="direct-lightbox" role="dialog" aria-modal="true" aria-label={image.name} tabIndex={-1} ref={dialog}
    onClick={event => { if (event.target === event.currentTarget) onClose(); }}
    onKeyDown={event => {
      if (event.key === "Escape") { event.stopPropagation(); onClose(); }
      else if (event.key === "ArrowRight" && images.length > 1) step(1);
      else if (event.key === "ArrowLeft" && images.length > 1) step(-1);
    }}>
    <header><span>{image.name}{images.length > 1 && <small> · {index + 1} of {images.length}</small>}</span>
      <span><a href={image.url + "?download=true"} download={image.name}>Download</a>
        <button type="button" aria-label="Close" onClick={onClose}>×</button></span></header>
    <img src={image.url} alt={image.name} referrerPolicy="no-referrer" />
    {images.length > 1 && <><button type="button" className="direct-lightbox-previous" aria-label="Previous picture" onClick={() => step(-1)}>‹</button>
      <button type="button" className="direct-lightbox-next" aria-label="Next picture" onClick={() => step(1)}>›</button></>}
  </div>;
}

function Tile({ reference, more, onOpen, onKind }: { reference: DirectReference; more: number; onOpen: () => void; onKind: (image: boolean) => void }) {
  const { file, failed } = useDirectFile(reference.url);
  useEffect(() => { if (file) onKind(isImage(file.mime)); }, [file, onKind]);
  if (failed) return <span className="direct-media-tile direct-media-missing">Unavailable</span>;
  return <button type="button" className="direct-media-tile" aria-label={"Open " + reference.name} onClick={onOpen}>
    {file && isImage(file.mime) && <img src={reference.url} alt={reference.name} loading="lazy" referrerPolicy="no-referrer" />}
    {more > 0 && <span className="direct-media-more">+{more}</span>}
  </button>;
}

/** Signal-style media: pictures in one grid (at most four tiles, the last showing how many more), then PDF and Markdown rows. */
export function DirectAttachments({ files }: { files: DirectReference[] }) {
  const [kinds, setKinds] = useState<Record<string, boolean>>({}), [viewing, setViewing] = useState<number | null>(null);
  const known = (url: string, image: boolean) => setKinds(current => current[url] === image ? current : { ...current, [url]: image });
  // Until its type is known a file is assumed to be a picture, so the grid does not jump for the common case.
  const images = files.filter(file => kinds[file.url] !== false), documents = files.filter(file => kinds[file.url] === false);
  const shown = images.slice(0, 4);
  return <div className="direct-attachments">
    {images.length > 0 && <div className={"direct-media-grid direct-media-" + Math.min(images.length, 4)}>
      {shown.map((file, index) => <Tile key={file.url} reference={file} more={index === 3 ? images.length - 4 : 0}
        onOpen={() => setViewing(index)} onKind={image => known(file.url, image)} />)}
      {images.slice(4).map(file => <HiddenKind key={file.url} reference={file} onKind={image => known(file.url, image)} />)}
    </div>}
    {documents.map(file => <DocumentRow key={file.url} reference={file} />)}
    {viewing !== null && images[viewing] && <Lightbox images={images} index={viewing} onIndex={setViewing} onClose={() => setViewing(null)} />}
  </div>;
}

function HiddenKind({ reference, onKind }: { reference: DirectReference; onKind: (image: boolean) => void }) {
  const { file } = useDirectFile(reference.url);
  useEffect(() => { if (file) onKind(isImage(file.mime)); }, [file, onKind]);
  return null;
}

/** The files waiting in the composer, each removable; a picture shows its own thumbnail. */
export function PendingFiles({ files, onRemove }: { files: PendingFile[]; onRemove: (key: string) => void }) {
  if (!files.length) return null;
  return <ul className="direct-pending" aria-label="Attachments to send">{files.map(file => <li key={file.key}
    className={"direct-pending-file" + (file.error ? " direct-pending-failed" : file.markdown ? "" : " direct-pending-uploading")} title={file.error ?? file.name}>
    {file.preview ? <img src={file.preview} alt="" /> : <span className="direct-file-icon" aria-hidden="true">{isImage(file.mime) ? "IMG" : "PDF"}</span>}
    <span className="direct-pending-name">{file.name}</span>
    <small role={file.error ? "alert" : undefined}>{file.error ? "Upload failed" : file.markdown ? "" : "Uploading…"}</small>
    <button type="button" aria-label={"Remove " + file.name} onClick={() => onRemove(file.key)}>×</button>
  </li>)}</ul>;
}

/** The composer's attach control: several pictures, PDFs and Markdown files at once. */
export function AttachFilesButton({ disabled, onFiles }: { disabled: boolean; onFiles: (files: File[]) => void }) {
  const input = useRef<HTMLInputElement>(null);
  return <><button type="button" className="org-upload-plus" disabled={disabled} aria-label="Attach pictures, PDFs or Markdown"
    title="Attach pictures, PDFs or Markdown" onClick={() => input.current?.click()}><PlusIcon /></button>
    <input hidden ref={input} type="file" multiple accept={ATTACHMENT_ACCEPT} onChange={event => {
      const files = Array.from(event.target.files ?? []); event.target.value = ""; if (files.length) onFiles(files);
    }} /></>;
}

export const pendingFile = (file: File): PendingFile => {
  const mime = attachmentMime(file);
  return { key: crypto.randomUUID(), name: file.name, mime, preview: isImage(mime) ? URL.createObjectURL(file) : undefined };
};
