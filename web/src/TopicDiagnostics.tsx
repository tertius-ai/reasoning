import React, { useEffect, useRef, useState } from "react";
import { ApiError, aborted, errorMessage, request, topicPath } from "./organization-api";
import { ResearchInstructions } from "./ResearchControls";

export function serializeTopicJSON(data: unknown): string {
  if (!data || typeof data !== "object" || !("schema" in data) || data.schema !== "reasoning.topic-summary.v2") return JSON.stringify(data, null, 2);
  // One record per line avoids thousands of formatting-only lines while preserving every summary value.
  return "{\n" + Object.entries(data).map(([key, value]) => "  " + JSON.stringify(key) + ": " + (Array.isArray(value)
    ? "[" + (value.length ? "\n" + value.map(item => "    " + JSON.stringify(item)).join(",\n") + "\n  " : "") + "]"
    : JSON.stringify(value))).join(",\n") + "\n}";
}

export function TopicSystemPrompt({ organizationId, topicId, onAuthLost, onInspect, revision = 0, bare = false }: {
  organizationId: string; topicId: string; onAuthLost: () => void; onInspect?: () => void; revision?: number; bare?: boolean;
}) {
  const [instructions, setInstructions] = useState<{ system_prompt: string; prompt_version: string } | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    setInstructions(null); setLoading(true); setError("");
    void request<typeof instructions>(topicPath(organizationId, topicId) + "/research-instructions", { signal: controller.signal })
      .then((value) => { if (!controller.signal.aborted) setInstructions(value); })
      .catch((cause) => {
        if (aborted(cause)) return;
        if (cause instanceof ApiError && cause.status === 401) onAuthLost(); else setError(errorMessage(cause));
      }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [organizationId, topicId, onAuthLost, revision]);
  return <ResearchInstructions instructions={instructions} loading={loading} error={error} onInspect={onInspect} bare={bare} />;
}

export function CopyIcon({ copied = false }: { copied?: boolean }) {
  return <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
    {copied ? <path d="m5 12 4 4 10-10" /> : <><rect x="8" y="8" width="12" height="12" rx="2" /><path d="M16 8V4H4v12h4" /></>}
  </svg>;
}

export function TopicCopyButton({ organizationId, topicId, onAuthLost }: {
  organizationId: string; topicId: string; onAuthLost: () => void;
}) {
  const [busy, setBusy] = useState(false), [copied, setCopied] = useState(false);
  const [fallback, setFallback] = useState(""), [error, setError] = useState("");
  const dialog = useRef<HTMLDialogElement>(null);
  const controller = useRef<AbortController | null>(null);
  const reset = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => { controller.current?.abort(); clearTimeout(reset.current); }, []);
  useEffect(() => { if (fallback || error) dialog.current?.showModal(); }, [fallback, error]);
  const close = () => { dialog.current?.close(); setFallback(""); setError(""); };
  const copy = async (detail: "summary" | "full" = "summary") => {
    if (busy) return;
    const current = new AbortController(); controller.current = current;
    clearTimeout(reset.current); setBusy(true); setCopied(false); setError(""); setFallback("");
    try {
      const data = await request<unknown>(topicPath(organizationId, topicId) + `/export?detail=${detail}`, { signal: current.signal });
      if (current.signal.aborted) return;
      const text = serializeTopicJSON(data);
      try {
        await navigator.clipboard.writeText(text);
        if (!current.signal.aborted) { setCopied(true); reset.current = setTimeout(() => setCopied(false), 2000); }
      } catch { if (!current.signal.aborted) setFallback(text); }
    } catch (cause) {
      if (current.signal.aborted || aborted(cause)) return;
      if (cause instanceof ApiError && cause.status === 401) onAuthLost(); else setError(errorMessage(cause));
    } finally { if (!current.signal.aborted) setBusy(false); }
  };
  const download = () => {
    const url = URL.createObjectURL(new Blob([fallback], { type: "application/json" }));
    const link = document.createElement("a"); link.href = url; link.download = `reasoning-topic-${topicId}.json`; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  return <>
    <button type="button" className="org-icon-button org-copy-topic" aria-label="Copy topic JSON" title="Copy topic JSON · Shift-click for full trace"
      disabled={busy} aria-busy={busy} data-copied={copied} onClick={(event) => void copy(event.shiftKey ? "full" : "summary")}><CopyIcon copied={copied} /></button>
    <dialog className="org-dialog org-export-dialog" ref={dialog} aria-labelledby="org-export-title" onCancel={close}>
      <header><h2 id="org-export-title">Topic JSON</h2><button type="button" aria-label="Close JSON" onClick={close}>×</button></header>
      {error ? <p role="alert" className="org-error">{error}</p> : <>
        <textarea aria-label="Topic diagnostic JSON" readOnly value={fallback} onFocus={(event) => event.target.select()} />
        <button type="button" onClick={download}>Download JSON</button>
      </>}
    </dialog>
  </>;
}
