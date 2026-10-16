// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import React, { createContext, lazy, Suspense, useContext, useRef, useState } from "react";
import { request, topicPath } from "./organization-api";
import { collectPdfText } from "./pdfText";
import "./topic-media.css";

export const MediaScope = createContext<{ organizationId: string; topicId?: string } | null>(null);
export type MediaFile = { id: string; name: string; mime: string; sha256: string; size_bytes: number; url: string; markdown: string };
const PdfViewer = lazy(() => import("./PdfViewer").then(module => ({ default: module.PdfViewer })));
const IMAGE_TYPES = new Set(["image/png", "image/jpeg", "image/webp"]);
const REF = /^\/api\/organizations\/([a-f0-9]{32})\/topics\/([a-f0-9]{32})\/media\/([a-f0-9]{32})\/([a-f0-9]{64})\/content$/;
export function mediaReference(url: string | undefined, organizationId?: string) {
  const match = REF.exec(url ?? "");
  return match && (!organizationId || match[1] === organizationId) ? match : null;
}

function EmbeddedMedia({ src, alt }: { src: string; alt: string }) {
  const [mime, setMime] = useState("");
  const [failed, setFailed] = useState(false);
  const [expanded, setExpanded] = useState(false);
  React.useEffect(() => {
    const controller = new AbortController();
    void request<{ mime: string }>(src.replace(/^\/api/, "").replace(/\/content$/, "/read"), { signal: controller.signal })
      .then(value => setMime(value.mime)).catch(cause => { if (cause?.name !== "AbortError") setFailed(true); });
    return () => controller.abort();
  }, [src]);
  if (failed) return <span className="org-help">Attachment unavailable</span>;
  if (IMAGE_TYPES.has(mime)) return <img className="topic-image" src={src} alt={alt} loading="lazy" referrerPolicy="no-referrer" />;
  if (mime === "application/pdf") return <details className="topic-pdf" onToggle={event => setExpanded(event.currentTarget.open)}><summary>{alt || "PDF"}</summary>
    {expanded && <><Suspense fallback={<span role="status">Loading PDF…</span>}><PdfViewer url={src} name={alt || "PDF"} /></Suspense>
    <a href={src} download>Download PDF</a></>}</details>;
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

async function pdfText(bytes: Uint8Array): Promise<string> {
  const { getDocument, GlobalWorkerOptions } = await import("pdfjs-dist");
  const worker = await import("pdfjs-dist/build/pdf.worker.min.mjs?url");
  GlobalWorkerOptions.workerSrc = worker.default;
  const task = getDocument({ data: bytes.slice(), useSystemFonts: true, disableFontFace: true });
  const timer = setTimeout(() => void task.destroy(), 15000);
  try {
    return await collectPdfText(await task.promise);
  } finally { clearTimeout(timer); await task.destroy(); }
}

export async function uploadMedia(file: File, organizationId: string, topicId: string): Promise<MediaFile> {
  const mime = file.type || (file.name.toLowerCase().endsWith(".pdf") ? "application/pdf" : "");
  if ((!IMAGE_TYPES.has(mime) && mime !== "application/pdf") || file.size < 1 || file.size > 8 * 1024 * 1024) {
    throw new Error("Choose a PNG, JPEG, WebP or PDF of at most 8 MiB.");
  }
  const bytes = new Uint8Array(await file.arrayBuffer());
  const sha256 = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)), byte => byte.toString(16).padStart(2, "0")).join("");
  const root = topicPath(organizationId, topicId) + "/media";
  const upload = await request<{ id: string; chunk_bytes: number }>(root, { method: "POST", body: {
    name: file.name, mime, size_bytes: bytes.length, sha256,
  } });
  for (let offset = 0, index = 0; offset < bytes.length; offset += upload.chunk_bytes, index++) {
    await request(root + `/${upload.id}/chunks/${index}`, { method: "PUT", body: { data_base64: base64(bytes.slice(offset, offset + upload.chunk_bytes)) } });
  }
  const preview_text = mime === "application/pdf" ? await pdfText(bytes).catch(() => "") : "";
  const vision_base64 = IMAGE_TYPES.has(mime) ? await imagePreview(file).catch(() => "") : "";
  return request<MediaFile>(root + `/${upload.id}/finish`, { method: "POST", body: { preview_text, vision_base64 } });
}

export function AttachmentButton({ organizationId, topicId, onInsert, onError, disabled = false }: {
  organizationId: string; topicId: string; onInsert: (markdown: string) => void; onError?: (error: unknown) => void; disabled?: boolean;
}) {
  const input = useRef<HTMLInputElement>(null), [busy, setBusy] = useState(false), [error, setError] = useState("");
  return <span className="topic-attachment-control"><button type="button" className="org-upload-play" disabled={disabled || busy} aria-label="Attach picture or PDF"
    title="Attach picture or PDF" onClick={() => input.current?.click()}><span aria-hidden="true">{busy ? "…" : "▶"}</span></button>
    <input hidden ref={input} type="file" accept="image/png,image/jpeg,image/webp,application/pdf" onChange={event => {
      const file = event.target.files?.[0]; event.target.value = ""; if (!file) return;
      setBusy(true); setError(""); void uploadMedia(file, organizationId, topicId).then(value => onInsert(value.markdown))
        .catch(cause => { setError(cause instanceof Error ? cause.message : "Attachment upload failed"); onError?.(cause); }).finally(() => setBusy(false));
    }} />{error && <span role="alert" className="org-error">{error}</span>}</span>;
}
