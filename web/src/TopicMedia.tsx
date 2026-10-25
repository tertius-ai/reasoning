// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import React, { createContext, lazy, Suspense, useContext, useRef, useState } from "react";
import { PlusIcon } from "./ComposerIcons";
import { request, topicPath } from "./organization-api";
import "./topic-media.css";

export const MediaScope = createContext<{ organizationId: string; topicId?: string; canWrite?: boolean } | null>(null);
export type Extraction = { status: string; method?: string; error?: string | null; truncated?: boolean; pages_read?: number; total_pages?: number };
export type MediaFile = { id: string; name: string; mime: string; sha256: string; size_bytes: number; url: string; markdown: string; extraction?: Extraction };
const PdfViewer = lazy(() => import("./PdfViewer").then(module => ({ default: module.PdfViewer })));
const IMAGE_TYPES = new Set(["image/png", "image/jpeg", "image/webp"]);
const REF = /^\/api\/organizations\/([a-f0-9]{32})\/topics\/([a-f0-9]{32})\/media\/([a-f0-9]{32})\/([a-f0-9]{64})\/content$/;
export function mediaReference(url: string | undefined, organizationId?: string) {
  const match = REF.exec(url ?? "");
  return match && (!organizationId || match[1] === organizationId) ? match : null;
}

function EmbeddedMedia({ src, alt }: { src: string; alt: string }) {
  const scope = useContext(MediaScope);
  const [mime, setMime] = useState("");
  const [extraction, setExtraction] = useState<Extraction | null>(null), [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const [expanded, setExpanded] = useState(false);
  React.useEffect(() => {
    const controller = new AbortController();
    void request<{ mime: string; extraction?: Extraction }>(src.replace(/^\/api/, "").replace(/\/content$/, "/read"), { signal: controller.signal })
      .then(value => { setMime(value.mime); setExtraction(value.extraction ?? null); }).catch(cause => { if (cause?.name !== "AbortError") setFailed(true); });
    return () => controller.abort();
  }, [src]);
  if (failed) return <span className="org-help">Attachment unavailable</span>;
  if (IMAGE_TYPES.has(mime)) return <img className="topic-image" src={src} alt={alt} loading="lazy" referrerPolicy="no-referrer" />;
  if (mime === "application/pdf") return <div className="topic-pdf"><p role="status">{extractionLabel(extraction)}
    {scope?.canWrite && extraction?.status !== "ready" && <button type="button" disabled={busy} onClick={() => {
      setBusy(true); void request<MediaFile>(src.replace(/^\/api/, "").replace(/\/content$/, "/extract"), { method: "POST", body: {} })
        .then(value => setExtraction(value.extraction ?? null)).catch(() => setExtraction({ status: "failed", error: "retry_unavailable" }))
        .finally(() => setBusy(false));
    }}>{busy ? "Extracting text…" : "Retry text extraction"}</button>}</p>
    <details onToggle={event => setExpanded(event.currentTarget.open)}><summary>{alt || "PDF"}</summary>
    {expanded && <><Suspense fallback={<span role="status">Loading PDF…</span>}><PdfViewer url={src} name={alt || "PDF"} /></Suspense>
    <a href={src} download>Download PDF</a></>}</details></div>;
  return <span role="status">Loading attachment…</span>;
}

export function MarkdownMedia({ src, alt }: { src?: string; alt?: string }) {
  const scope = useContext(MediaScope);
  if (!scope || !mediaReference(src, scope.organizationId)) return null;
  return <EmbeddedMedia key={src} src={src!} alt={alt ?? ""} />;
}

function base64(bytes: Uint8Array) {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

async function imagePreview(file: File): Promise<string> {
  const bitmap = await createImageBitmap(file);
  try {
    const canvas = document.createElement("canvas"), scale = Math.min(1, 512 / Math.max(bitmap.width, bitmap.height));
    canvas.width = Math.max(1, Math.round(bitmap.width * scale)); canvas.height = Math.max(1, Math.round(bitmap.height * scale));
    const context = canvas.getContext("2d");
    if (!context) return "";
    context.fillStyle = "white"; context.fillRect(0, 0, canvas.width, canvas.height); context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    for (const quality of [0.7, 0.5, 0.3]) {
      const encoded = canvas.toDataURL("image/jpeg", quality).split(",")[1];
      if (encoded.length <= 43688) return encoded;
    }
    return "";
  } finally { bitmap.close(); }
}

export function extractionLabel(value?: Extraction | null): string {
  if (value?.status === "ready") return `Text available for research${value.total_pages ? ` · ${value.pages_read}/${value.total_pages} pages` : ""}. Formulas and figures may need direct inspection.`;
  if (value?.status === "partial") return "Only part of this PDF is readable as text. Check coverage or provide its TeX/text source.";
  if (value?.error === "password_required") return "This PDF needs a password. Upload an unlocked copy or its TeX/text source.";
  if (value?.error === "extractor_busy") return "PDF uploaded. Text extraction is busy; retry in a moment.";
  if (value?.error === "no_text") return "No text was found in this PDF. Scanned pages need OCR or a TeX/text source.";
  if (["extraction_timeout", "resource_limit"].includes(value?.error ?? "")) return "PDF uploaded. Text extraction reached its resource limit; retry or provide a smaller document or text source.";
  return "PDF uploaded, but readable text is unavailable. Retry extraction or provide OCR, TeX, or text source.";
}

export const attachmentMime = (file: File) => file.type || (file.name.toLowerCase().endsWith(".pdf") ? "application/pdf" : "");

/** Chunked upload to a media collection (`root` ends in `/media`); `finish` supplies the body that completes it. */
export async function uploadAttachment<T>(file: File, root: string, finish: (mime: string) => Promise<object> = async () => ({})): Promise<T> {
  const mime = attachmentMime(file);
  if ((!IMAGE_TYPES.has(mime) && mime !== "application/pdf") || file.size < 1 || file.size > 8 * 1024 * 1024) {
    throw new Error("Choose a PNG, JPEG, WebP or PDF of at most 8 MiB.");
  }
  const bytes = new Uint8Array(await file.arrayBuffer());
  const sha256 = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)), byte => byte.toString(16).padStart(2, "0")).join("");
  const upload = await request<{ id: string; chunk_bytes: number }>(root, { method: "POST", body: {
    name: file.name, mime, size_bytes: bytes.length, sha256,
  } });
  for (let offset = 0, index = 0; offset < bytes.length; offset += upload.chunk_bytes, index++) {
    await request(root + `/${upload.id}/chunks/${index}`, { method: "PUT", body: { data_base64: base64(bytes.slice(offset, offset + upload.chunk_bytes)) } });
  }
  return request<T>(root + `/${upload.id}/finish`, { method: "POST", body: await finish(mime) });
}

export function uploadMedia(file: File, organizationId: string, topicId: string): Promise<MediaFile> {
  return uploadAttachment<MediaFile>(file, topicPath(organizationId, topicId) + "/media", async mime => ({
    preview_text: "", // The server extracts bounded text from the exact private PDF bytes.
    vision_base64: IMAGE_TYPES.has(mime) ? await imagePreview(file).catch(() => "") : "",
  }));
}

export function AttachmentButton({ organizationId, topicId, onInsert, onError, disabled = false }: {
  organizationId: string; topicId: string; onInsert: (markdown: string) => void; onError?: (error: unknown) => void; disabled?: boolean;
}) {
  const input = useRef<HTMLInputElement>(null), [busy, setBusy] = useState(false), [error, setError] = useState("");
  return <span className="topic-attachment-control"><button type="button" className="org-upload-plus" disabled={disabled || busy} aria-label="Attach picture or PDF"
    title="Attach picture or PDF" onClick={() => input.current?.click()}>{busy ? <span aria-hidden="true">…</span> : <PlusIcon />}</button>
    <input hidden ref={input} type="file" accept="image/png,image/jpeg,image/webp,application/pdf" onChange={event => {
      const file = event.target.files?.[0]; event.target.value = ""; if (!file) return;
      setBusy(true); setError(""); void uploadMedia(file, organizationId, topicId).then(value => {
        onInsert(value.markdown);
        if (value.mime === "application/pdf" && value.extraction?.status !== "ready") setError(extractionLabel(value.extraction));
      })
        .catch(cause => { setError(cause instanceof Error ? cause.message : "Attachment upload failed"); onError?.(cause); }).finally(() => setBusy(false));
    }} />{error && <span role="alert" className="org-error">{error}</span>}</span>;
}
