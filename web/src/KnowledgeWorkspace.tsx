import React, { useState } from "react";
import { Hierarchy, Organization, Topic } from "./organization-api";
import { MathematicsBrowser } from "./MathematicsBrowser";
import { ResearchBibliography } from "./ResearchBibliography";
import { KnowledgeBrowser } from "./KnowledgeBrowser";
import { ResearchBlog } from "./ResearchBlog";
import { KnowledgeLinks, KnowledgeView, knowledgeTitle, knowledgeURL } from "./KnowledgeLinks";
export { KnowledgeLinks, knowledgeTitle, knowledgeURL } from "./KnowledgeLinks";
export type { KnowledgeView } from "./KnowledgeLinks";

export function KnowledgeWorkspace({ organization, hierarchy, view, initialTopicId, initialProjectId, onAuthLost, embedded = false }: {
  organization: Organization; hierarchy: Hierarchy; view: KnowledgeView; initialTopicId?: string; initialProjectId?: string; onAuthLost: () => void;
  embedded?: boolean;
}) {
  const [scope, setScope] = useState(initialTopicId ?? (initialProjectId ? "project:" + initialProjectId : ""));
  const topicId = scope.startsWith("project:") ? "" : scope;
  const topic: Topic | undefined = hierarchy.topics.find(item => item.id === topicId);
  const channel = hierarchy.channels.find(item => item.id === topic?.channel_id);
  const project = hierarchy.projects.find(item => item.id === (scope.startsWith("project:") ? scope.slice(8) : channel?.project_id));
  const canWrite = organization.role !== "viewer" || project?.project_role === "owner";
  return <div className={embedded ? "org-knowledge-embedded" : "org-app org-knowledge-page"}>{!embedded && <header className="org-appbar">
    <a className="org-wordmark" href={"/organization.html?organization=" + encodeURIComponent(organization.id)}>{organization.name}</a>
    <h1>{knowledgeTitle(view)}</h1><KnowledgeLinks organizationId={organization.id} topicId={topicId || undefined} />
    <a href={"/organization.html?organization=" + encodeURIComponent(organization.id)}>Chat</a>
  </header>}<section className="org-knowledge-workspace" aria-label={knowledgeTitle(view)}>
    {view === "ledger" ? <>
      <label className="org-workspace-scope">Scope<select value={scope} onChange={event => {
        const next = event.target.value; setScope(next); window.history.replaceState(null, "", knowledgeURL(organization.id, view,
          next.startsWith("project:") ? undefined : next || undefined, next.startsWith("project:") ? next.slice(8) : undefined));
      }}>
        <option value="">All accessible mathematics</option>{hierarchy.projects.map(item => <option key={item.id} value={"project:" + item.id}>{item.name}</option>)}
        {hierarchy.topics.map(item => <option key={item.id} value={item.id}>
          {hierarchy.projects.find(p => p.id === hierarchy.channels.find(c => c.id === item.channel_id)?.project_id)?.name} / {item.name}
        </option>)}
      </select></label>
      {topicId && !topic || scope.startsWith("project:") && !project ? <p role="alert">This research scope is unavailable.</p> : <MathematicsBrowser key={scope} organizationId={organization.id}
        topicId={topic?.kind !== "alpha" ? topicId || undefined : undefined} projectId={project?.id} canWrite={canWrite} onAuthLost={onAuthLost} />}
      <details className="org-workspace-notes"><summary>Shared notes and source records</summary><KnowledgeBrowser organizationId={organization.id}
        hierarchy={hierarchy} canWrite={organization.role !== "viewer"} initialKind="ledger" notesOnly onAuthLost={onAuthLost} /></details>
    </> : view === "bibliography" ? <ResearchBibliography organizationId={organization.id} hierarchy={hierarchy} canWrite={canWrite} onAuthLost={onAuthLost} />
      : <ResearchBlog organizationId={organization.id} hierarchy={hierarchy} canWrite={organization.role !== "viewer"}
        initialProjectId={project?.id} onAuthLost={onAuthLost} />}
  </section></div>;
}
