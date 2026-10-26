// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import React, { useCallback, useEffect, useRef, useState } from "react";
import { FailedRequest, PageError, recentFailedRequests, recentPageErrors } from "./diagnostics";
import { ApiError, Organization, aborted, errorMessage, organizationPath, request } from "./organization-api";
import { pageRelease } from "./release";
import "./feedback.css";

type Kind = "bug" | "idea";
/** Diagnostics a reporter chose to attach; see diagnostics.ts for what each list holds and leaves out. */
export type Diagnostics = { view: string; release: string; browser: string; viewport: string; requests: FailedRequest[]; errors: PageError[] };
export type FeedbackReport = {
  id: string; kind: Kind; body: string; context: Partial<Diagnostics> & { server_release?: string }; created_at: number;
  resolved_at: number | null; resolved_by: string | null; reporter: { id: string; name: string };
};

export function BugIcon() {
  return <svg viewBox="0 0 16 16" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <rect x="5" y="5.5" width="6" height="8" rx="3" /><path d="M6.4 3.2 7.2 5.4M9.6 3.2 8.8 5.4M5 8H2.8M13.2 8H11M5 10.8 3 12M11 10.8l2 1.2M5 7 3.2 5.6M11 7l1.8-1.4" />
  </svg>;
}

const clock = (at: number) => new Date(at * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const plural = (count: number, word: string) => `${count} ${word}${count === 1 ? "" : "s"}`;

const currentDiagnostics = (view: string): Diagnostics => ({
  view: view.slice(0, 300),
  release: pageRelease()?.slice(0, 12) ?? "development",
  browser: navigator.userAgent.slice(0, 300),
  viewport: `${window.innerWidth}×${window.innerHeight}`,
  requests: recentFailedRequests(),
  errors: recentPageErrors(),
});

/** The diagnostics exactly as a report carries them; the preview and Administration both show them this way. */
function DiagnosticsList({ diagnostics, serverRelease }: { diagnostics: Partial<Diagnostics>; serverRelease?: string }) {
  const requests = diagnostics.requests ?? [], errors = diagnostics.errors ?? [];
  return <div className="org-feedback-diagnostics">
    <h4>Current view</h4>
    <dl>
      {diagnostics.view && <><dt>View</dt><dd>{diagnostics.view}</dd></>}
      {diagnostics.release && <><dt>Release</dt><dd>{diagnostics.release}{serverRelease && ` (server ${serverRelease.slice(0, 12)})`}</dd></>}
      {diagnostics.browser && <><dt>Browser</dt><dd>{diagnostics.browser}</dd></>}
      {diagnostics.viewport && <><dt>Window</dt><dd>{diagnostics.viewport}</dd></>}
    </dl>
    <h4>Failed requests ({requests.length})</h4>
    {requests.length ? <ul>{requests.map((item, index) => <li key={index}>{clock(item.at)} {item.method} {item.path} → <span className="org-feedback-status">{item.status || "no response"}</span> {item.detail}</li>)}</ul>
      : <p>None in this tab.</p>}
    <h4>Page errors ({errors.length})</h4>
    {errors.length ? <ul>{errors.map((item, index) => <li key={index}><span className="org-feedback-error">{item.message}</span>{item.stack && <pre>{item.stack}</pre>}</li>)}</ul>
      : <p>None in this tab.</p>}
  </div>;
}

/** Everything a report will carry, before it is sent. */
function ReportPreview({ kind, text, diagnostics, organization, busy, error, onSend, onClose }: {
  kind: Kind; text: string; diagnostics: Diagnostics | null; organization: Organization; busy: boolean; error: string; onSend: () => void; onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => { dialog.current?.showModal(); }, []);
  return <dialog ref={dialog} className="org-dialog org-feedback-preview" aria-label="Report preview" onCancel={event => { event.preventDefault(); onClose(); }}>
    <header><h2>Report preview</h2><button type="button" aria-label="Close preview" onClick={onClose}>×</button></header>
    <p className="org-help">This is everything the administrators of {organization.name} will receive.</p>
    <h4>Message · {kind === "bug" ? "Bug" : "Idea"}</h4>
    <p className="org-feedback-message">{text.trim() ? text : <em>No message yet.</em>}</p>
    {diagnostics ? <DiagnosticsList diagnostics={diagnostics} /> : <p className="org-help">No diagnostics will be attached.</p>}
    {error && <p role="alert" className="org-error">{error}</p>}
    <div className="org-feedback-preview-actions"><button type="button" onClick={onClose}>Close</button>
      <button type="button" className="org-primary" disabled={busy || !text.trim()} onClick={onSend}>{busy ? "Sending…" : "Send"}</button></div>
  </dialog>;
}

/** The bug button at the right of the account footer. Its form opens beside the sidebar and sends a report to the organization's administrators. */
export function FeedbackButton({ organization, describeView, onAuthLost }: { organization: Organization; describeView: () => string; onAuthLost: () => void }) {
  const [open, setOpen] = useState(false), [kind, setKind] = useState<Kind>("bug"), [text, setText] = useState("");
  const [include, setInclude] = useState(true), [busy, setBusy] = useState(false), [error, setError] = useState(""), [sent, setSent] = useState(false);
  // While the preview is open, exactly the diagnostics it shows are the ones sent.
  const [preview, setPreview] = useState<Diagnostics | null | undefined>(undefined);
  const attempt = useRef<string>(crypto.randomUUID());
  const root = useRef<HTMLDivElement>(null), field = useRef<HTMLTextAreaElement>(null), button = useRef<HTMLButtonElement>(null);
  const previewing = preview !== undefined;
  const close = useCallback((focus = false) => { setOpen(false); setSent(false); setError(""); setPreview(undefined); if (focus) button.current?.focus(); }, []);
  useEffect(() => {
    if (!open || previewing) return;
    const outside = (event: PointerEvent) => { if (!root.current?.contains(event.target as Node)) close(); };
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") close(true); };
    document.addEventListener("pointerdown", outside); document.addEventListener("keydown", escape);
    return () => { document.removeEventListener("pointerdown", outside); document.removeEventListener("keydown", escape); };
  }, [open, previewing, close]);
  useEffect(() => { if (open && !sent && !previewing) field.current?.focus(); }, [open, sent, previewing]);
  useEffect(() => {
    if (!sent) return;
    const timer = setTimeout(() => close(true), 2500);
    return () => clearTimeout(timer);
  }, [sent, close]);
  // A different report is a different attempt, so a retry never sends changed text under an earlier identifier.
  const change = (update: () => void) => { attempt.current = crypto.randomUUID(); setError(""); update(); };
  const counts = open ? [recentFailedRequests().length, recentPageErrors().length] : [0, 0];
  const send = async () => {
    if (busy || !text.trim()) return;
    const diagnostics = previewing ? preview : include ? currentDiagnostics(describeView()) : null;
    setBusy(true); setError("");
    try {
      await request(organizationPath(organization.id) + "/feedback-reports", { method: "POST", body: {
        kind, body: text, client_id: attempt.current, ...(diagnostics ? { context: diagnostics } : {}),
      } });
      setText(""); setKind("bug"); attempt.current = crypto.randomUUID(); setPreview(undefined); setSent(true);
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 401) onAuthLost(); else setError(errorMessage(cause));
    } finally { setBusy(false); }
  };
  return <div className="org-feedback" ref={root}>
    <button type="button" ref={button} className="org-feedback-launcher" aria-label="Report a bug or send feedback" title="Report a bug or send feedback"
      aria-expanded={open} onClick={() => open ? close() : setOpen(true)}><BugIcon /></button>
    {open && <div className="org-feedback-popover" role="dialog" aria-label="Send feedback">
      {sent ? <p role="status" className="org-feedback-sent">Thanks. Your report went to the administrators of {organization.name}.</p> : <form onSubmit={event => { event.preventDefault(); void send(); }}>
        <h3><BugIcon />Send feedback</h3>
        <div className="org-feedback-kind" role="radiogroup" aria-label="Kind of feedback">
          {(["bug", "idea"] as const).map(value => <button key={value} type="button" role="radio" aria-checked={kind === value}
            onClick={() => change(() => setKind(value))}>{value === "bug" ? "Bug" : "Idea"}</button>)}
        </div>
        <label className="org-visually-hidden" htmlFor="org-feedback-text">{kind === "bug" ? "What happened" : "Your idea"}</label>
        <textarea id="org-feedback-text" ref={field} rows={3} maxLength={4000} value={text} disabled={busy}
          placeholder={kind === "bug" ? "What happened, and what did you expect?" : "What would make Reasoning better for you?"}
          onChange={event => change(() => setText(event.target.value))}
          onKeyDown={event => { if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) { event.preventDefault(); void send(); } }} />
        <label className="org-feedback-context">
          <input type="checkbox" checked={include} onChange={event => change(() => setInclude(event.target.checked))} />
          <span>Include diagnostics <small>(view, browser, {plural(counts[0], "failed request")}, {plural(counts[1], "page error")})</small></span>
        </label>
        {error && !previewing && <p role="alert" className="org-error">{error}</p>}
        <div className="org-feedback-actions"><button type="button" onClick={() => { setError(""); setPreview(include ? currentDiagnostics(describeView()) : null); }}>Preview</button>
          <span><button type="button" onClick={() => close(true)}>Cancel</button>
            <button className="org-primary" disabled={busy || !text.trim()}>{busy ? "Sending…" : "Send"}</button></span></div>
      </form>}
      {previewing && <ReportPreview kind={kind} text={text} diagnostics={preview} organization={organization} busy={busy} error={error}
        onSend={() => void send()} onClose={() => { setPreview(undefined); setError(""); }} />}
    </div>}
  </div>;
}

const utc = (at: number) => new Date(at * 1000).toISOString().slice(0, 16).replace("T", " ") + " UTC";
/** A fenced block that no backtick run inside the text can close early. */
const fenced = (lines: string[]) => {
  const fence = "`".repeat(Math.max(3, ...lines.map(line => Math.max(0, ...(line.match(/`+/g) ?? []).map(run => run.length + 1)))));
  return [fence, ...lines, fence];
};

/** A report as GitHub-flavoured Markdown, with its diagnostics folded into a <details> block; times are UTC. */
export function reportMarkdown(report: FeedbackReport): string {
  const context = report.context, requests = context.requests ?? [], errors = context.errors ?? [];
  const lines = [`### ${report.kind === "bug" ? "Bug report" : "Idea"} from ${report.reporter.name} · ${utc(report.created_at)}`, "", report.body];
  if (report.resolved_at) lines.push("", `Resolved ${utc(report.resolved_at)}.`);
  if (!Object.keys(context).length) return lines.join("\n");
  const release = context.release && `${context.release}${context.server_release ? ` (server ${context.server_release.slice(0, 12)})` : ""}`;
  const facts = [["View", context.view], ["Release", release], ["Browser", context.browser], ["Window", context.viewport]].filter(([, value]) => value);
  lines.push("", "<details><summary>Diagnostics</summary>", "", ...facts.map(([label, value]) => `- ${label}: ${value}`),
    "", `**Failed requests (${requests.length})**`,
    ...(requests.length ? fenced(requests.map(item => `${utc(item.at).slice(11, 16)} ${item.method} ${item.path} → ${item.status || "no response"} ${item.detail}`.trimEnd())) : ["None."]),
    "", `**Page errors (${errors.length})**`,
    ...(errors.length ? fenced(errors.flatMap(item => [item.message, ...(item.stack ? item.stack.split("\n").map(line => "  " + line) : [])])) : ["None."]),
    "", "</details>");
  return lines.join("\n");
}

function Chevron() {
  return <svg className="org-feedback-chevron" viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M6 3.5 10.5 8 6 12.5" /></svg>;
}

type Filter = "all" | Kind;
type Group = "open" | "resolved";
const when = (at: number) => new Date(at * 1000);

/** Reports grouped into Open and Resolved; every report and both groups collapse. Resolved starts collapsed. */
export function FeedbackReports({ reports, busy, onResolve, onCopyFailed }: {
  reports: FeedbackReport[]; busy: string; onResolve: (report: FeedbackReport) => void; onCopyFailed: () => void;
}) {
  const [filter, setFilter] = useState<Filter>("all");
  const [groups, setGroups] = useState<ReadonlySet<Group>>(() => new Set(["open"]));
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(() => new Set());
  const [copied, setCopied] = useState("");
  useEffect(() => {
    if (!copied) return;
    const timer = setTimeout(() => setCopied(""), 2000);
    return () => clearTimeout(timer);
  }, [copied]);
  const flip = <T,>(set: ReadonlySet<T>, value: T) => { const next = new Set(set); if (!next.delete(value)) next.add(value); return next; };
  const copy = (report: FeedbackReport) => {
    if (!navigator.clipboard) { onCopyFailed(); return; }
    void navigator.clipboard.writeText(reportMarkdown(report)).then(() => setCopied(report.id), onCopyFailed);
  };
  const shown = reports.filter(report => filter === "all" || report.kind === filter);
  const open = reports.filter(report => !report.resolved_at).length;
  const group = (key: Group, label: string, items: FeedbackReport[]) => {
    const visible = groups.has(key), list = `org-feedback-group-${key}`;
    return <div className="org-feedback-group" key={key}>
      <button type="button" className="org-feedback-group-toggle" aria-expanded={visible} aria-controls={list} onClick={() => setGroups(flip(groups, key))}>
        <Chevron />{label}<span className="org-feedback-count">{items.length}</span></button>
      {visible && <ul id={list} aria-label={`${label} reports`}>{items.length ? items.map(report => {
        const isOpen = expanded.has(report.id), panel = `org-feedback-report-${report.id}`;
        const requests = report.context.requests?.length ?? 0, errors = report.context.errors?.length ?? 0, diagnostics = Object.keys(report.context).length > 0;
        const created = when(report.created_at);
        return <li key={report.id} className={report.resolved_at ? "org-feedback-resolved" : undefined}>
          <div className="org-feedback-row">
            <button type="button" className="org-feedback-toggle" aria-expanded={isOpen} aria-controls={panel} onClick={() => setExpanded(flip(expanded, report.id))}>
              <Chevron /><span className={"org-feedback-badge org-feedback-" + report.kind}>{report.kind === "bug" ? "Bug" : "Idea"}</span>
              <span className="org-feedback-summary">{report.body.split("\n", 1)[0]}</span></button>
            {diagnostics && <span className="org-feedback-signals" title={`${plural(requests, "failed request")}, ${plural(errors, "page error")}`}>
              <span className={requests ? "org-feedback-status" : undefined} aria-hidden="true">⇄{requests}</span>
              <span className={errors ? "org-feedback-error" : undefined} aria-hidden="true">!{errors}</span>
              <span className="org-visually-hidden">{plural(requests, "failed request")}, {plural(errors, "page error")}</span></span>}
            <time dateTime={created.toISOString()} title={created.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}>
              {created.toLocaleDateString(undefined, { month: "short", day: "numeric" })}</time>
          </div>
          {isOpen && <div className="org-feedback-panel" id={panel}>
            <p className="org-help">{report.reporter.name} · {created.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}
              {report.resolved_at && ` · resolved ${when(report.resolved_at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}`}</p>
            <p className="org-feedback-body">{report.body}</p>
            {diagnostics ? <DiagnosticsList diagnostics={report.context} serverRelease={report.context.server_release} /> : <p className="org-help">No diagnostics attached.</p>}
            <div className="org-feedback-report-actions">
              <button type="button" disabled={busy === report.id} onClick={() => onResolve(report)}>{report.resolved_at ? "Reopen" : "Resolve"}</button>
              <button type="button" className={copied === report.id ? "org-feedback-copied" : undefined} onClick={() => copy(report)}>
                {copied === report.id ? "Copied" : "Copy report"}</button>
            </div>
          </div>}
        </li>;
      }) : <li className="org-help">No {label.toLowerCase()} reports{filter === "all" ? "" : ` of this kind`}.</li>}</ul>}
    </div>;
  };
  return <>
    <div className="org-feedback-toolbar">
      <div className="org-feedback-filter" role="group" aria-label="Kind of report">
        {(["all", "bug", "idea"] as const).map(value => <button key={value} type="button" aria-pressed={filter === value} onClick={() => setFilter(value)}>
          {value === "all" ? "All" : value === "bug" ? "Bugs" : "Ideas"}</button>)}
      </div>
      <p className="org-help">{open} open · {reports.length - open} resolved</p>
    </div>
    <span className="org-visually-hidden" role="status">{copied ? "Report copied as Markdown." : ""}</span>
    {group("open", "Open", shown.filter(report => !report.resolved_at))}
    {group("resolved", "Resolved", shown.filter(report => report.resolved_at))}
  </>;
}

/** Administration › Feedback: every report in the organization, newest first, grouped by status and resolvable. */
export function FeedbackAdministration({ organizationId, onAuthLost }: { organizationId: string; onAuthLost: () => void }) {
  const [reports, setReports] = useState<FeedbackReport[] | null>(null), [error, setError] = useState(""), [busy, setBusy] = useState("");
  const root = organizationPath(organizationId) + "/feedback-reports";
  const fail = useCallback((cause: unknown) => {
    if (aborted(cause)) return;
    if (cause instanceof ApiError && cause.status === 401) onAuthLost(); else setError(errorMessage(cause));
  }, [onAuthLost]);
  useEffect(() => {
    const controller = new AbortController();
    void request<FeedbackReport[]>(root, { signal: controller.signal }).then(setReports).catch(fail);
    return () => controller.abort();
  }, [root, fail]);
  const resolve = (report: FeedbackReport) => {
    setBusy(report.id); setError("");
    void request<FeedbackReport>(root + "/" + encodeURIComponent(report.id) + "/resolution", { method: "PUT", body: { resolved: !report.resolved_at } })
      .then(updated => setReports(current => current?.map(item => item.id === updated.id ? updated : item) ?? null)).catch(fail).finally(() => setBusy(""));
  };
  return <section className="org-feedback-admin" aria-label="Feedback reports">
    {error && <p role="alert" className="org-error">{error}</p>}
    {!reports ? <p role="status" className="org-help">Loading reports…</p>
      : !reports.length ? <p className="org-help">No reports yet. Members send them with the bug button next to their name.</p>
        : <FeedbackReports reports={reports} busy={busy} onResolve={resolve}
          onCopyFailed={() => setError("Clipboard unavailable. Expand the report and copy its text manually.")} />}
  </section>;
}
