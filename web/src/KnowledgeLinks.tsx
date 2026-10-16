import React from "react";

export type KnowledgeView = "ledger" | "bibliography" | "journal";
export const knowledgeTitle = (view: KnowledgeView) => view === "journal" ? "Research blog" : view === "ledger" ? "Ledger" : "Bibliography";
export function knowledgeURL(organizationId: string, view: KnowledgeView, topicId?: string, projectId?: string) {
  const parameters = new URLSearchParams({ organization: organizationId, view });
  if (topicId) parameters.set("topic", topicId);
  if (projectId) parameters.set("project", projectId);
  return "/organization.html?" + parameters;
}

export function KnowledgeLinks({ organizationId, topicId, onOpen, onIntent }: { organizationId: string; topicId?: string;
  onOpen?: (view: KnowledgeView) => void; onIntent?: () => void }) {
  return <nav className="org-knowledge-links" aria-label="Research workspaces" onPointerEnter={onIntent} onFocus={onIntent}>{(["ledger", "bibliography", "journal"] as KnowledgeView[]).map(view =>
    <a key={view} className="org-icon-button" href={view === "journal" ? `/blog/${encodeURIComponent(organizationId)}` : knowledgeURL(organizationId, view, topicId)}
      onClick={event => { if (view !== "journal" && onOpen && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey && event.button === 0) { event.preventDefault(); onOpen(view); } }}
      aria-label={`Open ${knowledgeTitle(view).toLowerCase()}`} title={knowledgeTitle(view)}>
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
        {view === "ledger" ? <><circle cx="5" cy="12" r="2" /><circle cx="18" cy="5" r="2" /><circle cx="18" cy="19" r="2" /><path d="M7 11l9-5M7 13l9 5" /></> : view === "bibliography"
          ? <><path d="M3 4h7l2 2 2-2h7v15h-7l-2 2-2-2H3zM12 6v15" /></> : <><path d="M4 4h16v16H4zM8 8h8M8 12h8M8 16h5" /></>}
      </svg>
    </a>)}</nav>;
}
