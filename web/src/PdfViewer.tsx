import { useCallback, useEffect, useRef, useState, type RefObject } from "react";
import {
  getDocument,
  GlobalWorkerOptions,
  type PDFDocumentProxy,
  type RenderTask,
} from "pdfjs-dist";
import pdfWorkerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import jbig2WasmUrl from "pdfjs-dist/wasm/jbig2.wasm?url&no-inline";
import openjpegWasmUrl from "pdfjs-dist/wasm/openjpeg.wasm?url&no-inline";
import foxitDingbatsUrl from "pdfjs-dist/standard_fonts/FoxitDingbats.pfb?url&no-inline";
import foxitSymbolUrl from "pdfjs-dist/standard_fonts/FoxitSymbol.pfb?url&no-inline";
import { nearestPdfPage } from "./pdfNavigation";

GlobalWorkerOptions.workerSrc = pdfWorkerUrl;

// pdf.js asks for its decoders and fonts by fixed file name. The worker decodes JBIG2 and CCITT (scanned pages) and
// JPX images only with jbig2.wasm and openjpeg.wasm; without them it drops the image and the page renders blank.
// With system fonts on, the only standard fonts it fetches are Symbol and ZapfDingbats. Each file is a content-hashed
// build asset, so it caches like the rest of the release and survives upgrades for pages that are already open.
const BUNDLED_BINARY_DATA: Record<string, Record<string, string>> = {
  wasmUrl: { "jbig2.wasm": jbig2WasmUrl, "openjpeg.wasm": openjpegWasmUrl },
  standardFontDataUrl: { "FoxitDingbats.pfb": foxitDingbatsUrl, "FoxitSymbol.pfb": foxitSymbolUrl },
};

// Passed as getDocument's BinaryDataFactory, which makes the worker request these files by name through this page.
class BundledBinaryData {
  async fetch({ kind, filename }: { kind: string; filename: string }): Promise<Uint8Array> {
    const url = BUNDLED_BINARY_DATA[kind]?.[filename];
    if (!url) throw new Error(`PDF support file ${filename} is not part of this build`);
    const response = await fetch(url);
    if (!response.ok) throw new Error(`PDF support file ${filename} could not be loaded (${response.status})`);
    return new Uint8Array(await response.arrayBuffer());
  }
}

const MIN_ZOOM = 0.5;
const MAX_ZOOM = 2;
const ZOOM_STEP = 0.25;

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "PDF could not be displayed";
}

function PdfPage({
  document,
  pageNumber,
  name,
  scrollRoot,
  setPageRef,
}: {
  document: PDFDocumentProxy;
  pageNumber: number;
  name: string;
  scrollRoot: RefObject<HTMLDivElement>;
  setPageRef: (pageNumber: number, element: HTMLDivElement | null) => void;
}) {
  const hostRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [width, setWidth] = useState(0);
  const [aspectRatio, setAspectRatio] = useState(8.5 / 11);
  const [nearViewport, setNearViewport] = useState(false);
  const [rendered, setRendered] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setPageRef(pageNumber, hostRef.current);
    return () => setPageRef(pageNumber, null);
  }, [pageNumber, setPageRef]);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const update = () => setWidth(host.clientWidth);
    update();
    const observer = new ResizeObserver(update);
    observer.observe(host);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const host = hostRef.current;
    const root = scrollRoot.current;
    if (!host || !root || typeof IntersectionObserver === "undefined") {
      setNearViewport(true);
      return;
    }
    const observer = new IntersectionObserver(
      ([entry]) => {
        setNearViewport(entry.isIntersecting);
        if (!entry.isIntersecting) setRendered(false);
      },
      { root, rootMargin: "150% 0px" },
    );
    observer.observe(host);
    return () => observer.disconnect();
  }, [scrollRoot]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!nearViewport || !canvas || width <= 0) return;
    let disposed = false;
    let renderTask: RenderTask | null = null;
    setError(null);
    setRendered(false);

    void document.getPage(pageNumber).then((page) => {
      if (disposed) return;
      const base = page.getViewport({ scale: 1 });
      setAspectRatio(base.width / base.height);
      if (![base.width, base.height].every(value => Number.isFinite(value) && value > 0)) throw new Error("PDF page dimensions are invalid");
      const cssScale = Math.min(2, Math.max(1, width - 2) / base.width, 1600 / base.height, 1600 / base.width);
      const pixelRatio = Math.min(2, window.devicePixelRatio || 1);
      const viewport = page.getViewport({ scale: cssScale * pixelRatio });
      canvas.width = Math.floor(viewport.width);
      canvas.height = Math.floor(viewport.height);
      canvas.style.width = `${Math.floor(viewport.width / pixelRatio)}px`;
      canvas.style.height = `${Math.floor(viewport.height / pixelRatio)}px`;
      renderTask = page.render({ canvas, viewport, background: "#ffffff" });
      return renderTask.promise;
    }).then(() => {
      if (!disposed) setRendered(true);
    }).catch((reason: unknown) => {
      if (!disposed && !(reason instanceof Error && reason.name === "RenderingCancelledException")) {
        setError(errorMessage(reason));
      }
    });

    return () => {
      disposed = true;
      renderTask?.cancel();
    };
  }, [document, nearViewport, pageNumber, width]);

  return (
    <div
      className="artifact-pdf-page"
      data-page-number={pageNumber}
      ref={hostRef}
      role="group"
      aria-label={`Page ${pageNumber}`}
      aria-busy={nearViewport && !rendered && !error}
      style={{ aspectRatio }}
    >
      {error ? (
        <div className="empty" role="alert">{error}</div>
      ) : nearViewport ? (
        <>
          {!rendered && (
            <div className="artifact-pdf-page-status" role="status">
              loading page {pageNumber}…
            </div>
          )}
          <canvas
            className={rendered ? "" : "loading"}
            ref={canvasRef}
            role="img"
            aria-label={`${name}, page ${pageNumber}`}
          />
        </>
      ) : null}
      <span className="artifact-pdf-page-number" aria-hidden="true">
        {pageNumber}
      </span>
    </div>
  );
}

function pageAtReadingPosition(
  root: HTMLDivElement,
  pages: Array<HTMLDivElement | null>,
): number {
  const rootRect = root.getBoundingClientRect();
  const marker = rootRect.top + Math.min(rootRect.height * 0.3, 240);
  return nearestPdfPage(
    pages.flatMap((page, index) => {
      if (!page) return [];
      const rect = page.getBoundingClientRect();
      return [{ pageNumber: index + 1, top: rect.top, bottom: rect.bottom }];
    }),
    marker,
  );
}

export function PdfViewer({ url, name }: { url: string; name: string }) {
  const [document, setDocument] = useState<PDFDocumentProxy | null>(null);
  const [pageNumber, setPageNumber] = useState(1);
  const [zoom, setZoom] = useState(1);
  const [error, setError] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const pagesRef = useRef<Array<HTMLDivElement | null>>([]);
  const pagesHostRef = useRef<HTMLDivElement>(null);

  const setPageRef = useCallback((number: number, element: HTMLDivElement | null) => {
    pagesRef.current[number - 1] = element;
  }, []);

  const scrollToPage = useCallback((number: number) => {
    const root = scrollRef.current;
    const page = pagesRef.current[number - 1];
    if (!root || !page) return;
    const rootRect = root.getBoundingClientRect();
    const pageRect = page.getBoundingClientRect();
    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    root.scrollTo({
      top: root.scrollTop + pageRect.top - rootRect.top - 16,
      behavior: reduceMotion ? "auto" : "smooth",
    });
    setPageNumber(number);
  }, []);

  useEffect(() => {
    const root = scrollRef.current;
    const pagesHost = pagesHostRef.current;
    if (!document || !root || !pagesHost) return;
    let frame = 0;
    const update = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        setPageNumber((current) => {
          const next = pageAtReadingPosition(root, pagesRef.current);
          return current === next ? current : next;
        });
      });
    };
    const resizeObserver = new ResizeObserver(update);
    resizeObserver.observe(root);
    resizeObserver.observe(pagesHost);
    root.addEventListener("scroll", update, { passive: true });
    update();
    return () => {
      cancelAnimationFrame(frame);
      resizeObserver.disconnect();
      root.removeEventListener("scroll", update);
    };
  }, [document]);

  useEffect(() => {
    let active = true;
    pagesRef.current = [];
    setDocument(null);
    setPageNumber(1);
    setZoom(1);
    setError(null);
    const task = getDocument({ url, useSystemFonts: true, disableFontFace: true, BinaryDataFactory: BundledBinaryData });
    void task.promise.then((loaded) => {
      if (loaded.numPages > 200) { void task.destroy(); throw new Error("PDF preview is limited to 200 pages; download the document to inspect all pages."); }
      if (active) setDocument(loaded);
    }).catch((reason: unknown) => {
      if (active) setError(errorMessage(reason));
    });
    return () => {
      active = false;
      void task.destroy();
    };
  }, [url]);

  if (error) return <div className="empty" role="alert">{error}</div>;
  if (!document) return <div className="empty" role="status">loading PDF…</div>;

  return (
    <div className="artifact-pdf">
      <div className="artifact-pdf-toolbar" aria-label="PDF page controls">
        <div className="artifact-pdf-toolbar-group">
          <button
            className="icon-btn"
            disabled={pageNumber <= 1}
            aria-label="Previous page"
            onClick={() => scrollToPage(Math.max(1, pageNumber - 1))}
          >
            ←
          </button>
          <span aria-live="polite">page {pageNumber} of {document.numPages}</span>
          <button
            className="icon-btn"
            disabled={pageNumber >= document.numPages}
            aria-label="Next page"
            onClick={() => scrollToPage(Math.min(document.numPages, pageNumber + 1))}
          >
            →
          </button>
        </div>
        <div className="artifact-pdf-toolbar-divider" aria-hidden="true" />
        <div className="artifact-pdf-toolbar-group">
          <button
            className="icon-btn"
            disabled={zoom <= MIN_ZOOM}
            aria-label="Zoom out"
            onClick={() => setZoom((current) => Math.max(MIN_ZOOM, current - ZOOM_STEP))}
          >
            −
          </button>
          <span>{Math.round(zoom * 100)}%</span>
          <button
            className="icon-btn"
            disabled={zoom >= MAX_ZOOM}
            aria-label="Zoom in"
            onClick={() => setZoom((current) => Math.min(MAX_ZOOM, current + ZOOM_STEP))}
          >
            +
          </button>
        </div>
      </div>
      <div
        className="artifact-pdf-scroll"
        ref={scrollRef}
        tabIndex={0}
        aria-label={`${name}, scrollable PDF document`}
      >
        <div
          className="artifact-pdf-pages"
          ref={pagesHostRef}
          style={{ width: `${zoom * 100}%` }}
        >
          {Array.from({ length: document.numPages }, (_, index) => (
            <PdfPage
              key={index + 1}
              document={document}
              pageNumber={index + 1}
              name={name}
              scrollRoot={scrollRef}
              setPageRef={setPageRef}
            />
          ))}
        </div>
      </div>
    </div>
  );
}
