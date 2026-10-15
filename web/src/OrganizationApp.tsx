import { schedulePoll } from "./polling";
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown, { Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import { BetaConversation } from "./BetaConversation";
import { ForkOrigin, MessageActions } from "./ChatFeedback";
import { BotAdministration } from "./BotAdministration";
import { BotApprovalDialog, useBotApprovals } from "./BotApprovals";
import { BotConversation, submitModelAttempt } from "./BotConversation";
import { withBotModels } from "./botModels";
import { availableEfforts } from "./ReasoningEffort";
import { editMentions, encodeMentions, MentionBinding } from "./composerMentions";
import { ComputeAdministration } from "./ComputeAdministration";
import { ComputeConversation } from "./ComputeConversation";
import { KnowledgeBrowser } from "./KnowledgeBrowser";
import { KnowledgeLinks, KnowledgeView, KnowledgeWorkspace } from "./KnowledgeWorkspace";
import { WorkflowConversation } from "./WorkflowConversation";
import { TopicActivity, orderedActivity } from "./TopicActivity";
import { WorkflowAttempt } from "./workflow-api";
import { WorkflowExecutionAttempt } from "./workflow-run-api";
import { ComputeAttempt } from "./compute-api";
import { MessageUsageDetails, OrganizationModelBrowser, OrganizationUsagePanel } from "./OrganizationUsage";
import { normalizeMathDelimiters } from "./markdownMath";
import { modelOptionGroups } from "./modelRecommendations";
import { ModelPicker, compactTokens } from "./ModelBrowser";
import { ResearchControls, RESEARCH_TOOLS, ResearchTool, TOOL_LABELS, goalCommand } from "./ResearchControls";
import { TopicCopyButton, TopicSystemPrompt } from "./TopicDiagnostics";
import { NavigationUsage, readNavigation, saveNavigation, Spend } from "./navigationState";
import { ThemeToggle } from "./ThemeToggle";
import { ShortcutsHelp, SlashCommandPalette, TOPIC_COMMANDS, TopicCommand, dispatchTopicCommand, useTopicCommand } from "./TopicCommands";
import { AttachmentButton, MarkdownMedia, MediaScope } from "./TopicMedia";
import { TexRequestDialog, texRequest } from "./TexRequestDialog";
import { HelpPage } from "./HelpPage";
import { EmailVerification, VerifyEmailBanner } from "./EmailVerification";
import { BatchInvitations, InvitationDeliveryProgress } from "./BatchInvitations";
import { ProviderKeyUsage } from "./ProviderKeyUsage";
import {
  Account, ApiError, Channel, EMPTY_HIERARCHY, Hierarchy, InferenceConfiguration, Invitation, InvitationMail, KeyMetadata, KIND_LABEL, KIND_SYMBOL, KnowledgeTool,
  BotModel, Member, MentionQuery, Message, ModelAttempt, Organization, OrganizationPrices, Policy, Presence, Project, ProjectMember, SearchResult, Session, Topic, TopicKind,
  aborted, authorLabel, decimalBudget, errorMessage, invitationEmailLabel, mentionMarkdown, mentionQuery, mentionText, mergeMessages, organizationPath, request, topicPath,
} from "./organization-api";

type Draft = { text: string; clientId: string; attempted: boolean; mentions?: MentionBinding[] };
type Creation = "organization" | "project" | "channel" | "topic";
const newDraft = (): Draft => ({ text: "", clientId: crypto.randomUUID(), attempted: false });
// Stable renderer types preserve DOM selection across activity and message polling.
const messageMarkdownComponents: Components = {
  a: ({ children, href }) => <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>,
  p: ({ children, node }) => node?.children.some(child => child.type === "element" && child.tagName === "img")
    ? <div className="topic-media-paragraph">{children}</div> : <p>{children}</p>,
  img: ({ src, alt }) => <MarkdownMedia src={src} alt={alt} />,
};
export const prepareMessageAttempt = (draft: Draft): Draft => ({
  ...draft, clientId: draft.clientId || crypto.randomUUID(), attempted: true,
});
export const initializeDraft = (existing: Record<string, Draft>, key: string): Record<string, Draft> =>
  existing[key] ? existing : { ...existing, [key]: newDraft() };
export function agentCommand(text: string): "paused" | "active" | "cancelled" | null {
  const command = /^\/(pause|resume|stop)(?:\s+agents)?$/i.exec(text.trim());
  return command ? command[1].toLowerCase() === "pause" ? "paused" : command[1].toLowerCase() === "resume" ? "active" : "cancelled" : null;
}
export function resizeComposer(element: HTMLTextAreaElement): void {
  element.style.height = "auto";
  const maximum = Math.min(240, Math.max(80, window.innerHeight * .3));
  element.style.height = `${Math.min(element.scrollHeight, maximum)}px`;
  element.style.overflowY = element.scrollHeight > maximum ? "auto" : "hidden";
}
export function alphaMessageGroups(messages: Message[]): Message[][] {
  const groups: Message[][] = [];
  for (const message of messages) {
    const last = groups.at(-1)?.at(-1);
    const sameDay = last && new Date(last.created_at * 1000).toDateString() === new Date(message.created_at * 1000).toDateString();
    if (last && last.author_id === message.author_id && last.author_kind === message.author_kind
      && message.author_kind !== "assistant" && message.created_at >= last.created_at && message.created_at - last.created_at <= 300 && sameDay) groups.at(-1)!.push(message);
    else groups.push([message]);
  }
  return groups;
}

export function TopicBreadcrumb({ channel, topic, subscribed, onFollow, onSettings }: {
  channel?: Channel; topic: Topic; subscribed: boolean; onFollow: () => void; onSettings?: () => void;
}) {
  const menu = useRef<HTMLDetailsElement>(null);
  useEffect(() => {
    const close = (event: PointerEvent | KeyboardEvent) => {
      if (menu.current && ((event instanceof KeyboardEvent && event.key === "Escape")
        || (event instanceof PointerEvent && !menu.current.contains(event.target as Node)))) menu.current.open = false;
    };
    document.addEventListener("pointerdown", close); document.addEventListener("keydown", close);
    return () => { document.removeEventListener("pointerdown", close); document.removeEventListener("keydown", close); };
  }, []);
  return <nav className="org-current-topic" aria-label="Current topic">
    <span title={channel?.name}>#{channel?.name}</span><span aria-hidden="true">›</span>
    <details className="org-topic-menu" ref={menu}>
      <summary title={topic.name}><h1><span className="org-kind">{KIND_SYMBOL[topic.kind]}</span>-{topic.name}</h1></summary>
      <div className="org-topic-menu-options">
        <button type="button" onClick={() => { onFollow(); if (menu.current) menu.current.open = false; }}>
          {subscribed ? "Unfollow topic" : "Follow topic"}</button>
        {onSettings && <button type="button" onClick={() => { onSettings(); if (menu.current) menu.current.open = false; }}>Topic settings</button>}
      </div>
    </details>
  </nav>;
}
export const automaticModelAttempt = (model: string, through: number, _maximum: number, clientId = crypto.randomUUID()): ModelAttempt => ({
  client_id: clientId, models: [model], max_completion_tokens: null, context_message_limit: 50, through_sequence: through,
});

function NavigationSpend({ value, ready = true }: { value?: Spend; ready?: boolean }) {
  if (!ready) return <span className="org-tree-spend" aria-label="Loading confirmed spend">$…</span>;
  return <span className="org-tree-spend" title={`Confirmed provider spend. ${value?.unknown_calls ?? 0} unconfirmed calls; ${value?.external_runs ?? 0} bot runs with external accounting.`}>
    ${((value?.actual_microusd ?? 0) / 1000000).toFixed(2)}{Boolean(value?.unknown_calls || value?.external_runs) && <span aria-label="Additional unconfirmed or external charges">*</span>}</span>;
}

export function TopicNavigation({ hierarchy, organizationId = "", userId = "", usage, selected, canCreate, canManageOrganization, onSelect, onCreateChannel, onCreateTopic, onProjectSettings, onChannelSettings, onTopicSettings }: {
  organizationId?: string; userId?: string; usage?: NavigationUsage;
  hierarchy: Hierarchy; selected: string | null; canCreate: boolean; canManageOrganization: boolean; onSelect: (id: string) => void;
  onCreateChannel: (projectId: string) => void; onCreateTopic: (channelId: string) => void;
  onProjectSettings: (projectId: string) => void; onChannelSettings: (channelId: string) => void; onTopicSettings: (topicId: string) => void;
}) {
  const [collapsedChannels, setCollapsedChannels] = useState<Set<string>>(() => new Set(readNavigation(userId, organizationId).collapsedChannels));
  const [collapsedProjects, setCollapsedProjects] = useState<Set<string>>(() => new Set(readNavigation(userId, organizationId).collapsedProjects));
  type Scope = { kind: keyof NavigationUsage; id: string; name: string };
  const [pointerScope, setPointerScope] = useState<Scope | null>(null), [focusScope, setFocusScope] = useState<Scope | null>(null);
  const touchTimer = useRef<ReturnType<typeof setTimeout> | null>(null), held = useRef(false);
  const stopHold = () => { if (touchTimer.current) clearTimeout(touchTimer.current); touchTimer.current = null; };
  useEffect(() => () => { if (touchTimer.current) clearTimeout(touchTimer.current); }, []);
  const scope = pointerScope ?? focusScope;
  const reveal = (kind: Scope["kind"], id: string, name: string) => ({
    onPointerEnter: () => setPointerScope({ kind, id, name }), onPointerLeave: () => setPointerScope(null),
    onFocus: (event: React.FocusEvent<HTMLElement>) => {
      if (event.currentTarget.matches(":focus-visible") || window.matchMedia("(hover: none)").matches) setFocusScope({ kind, id, name });
    }, onBlur: () => setFocusScope(null),
    onPointerDown: (event: React.PointerEvent<HTMLElement>) => {
      stopHold(); held.current = false;
      if (event.pointerType === "touch") {
        const element = event.currentTarget;
        touchTimer.current = setTimeout(() => { held.current = true; element.focus({ preventScroll: true }); setFocusScope({ kind, id, name }); }, 450);
      }
    }, onPointerUp: stopHold, onPointerCancel: () => { stopHold(); held.current = false; },
    onClickCapture: (event: React.MouseEvent) => { if (held.current) { event.preventDefault(); event.stopPropagation(); held.current = false; } },
    onKeyDown: () => { held.current = false; },
    onContextMenu: (event: React.MouseEvent) => { if (held.current) event.preventDefault(); },
    "aria-describedby": "org-spend-" + id,
  });
  const spend = (kind: Scope["kind"], id: string) => <span id={"org-spend-" + id} className="org-inline-spend" data-visible={scope?.kind === kind && scope.id === id}>
    <NavigationSpend ready={Boolean(usage)} value={usage?.[kind][id]} /></span>;
  useEffect(() => { saveNavigation(userId, organizationId, { collapsedChannels: [...collapsedChannels], collapsedProjects: [...collapsedProjects] }); }, [userId, organizationId, collapsedChannels, collapsedProjects]);
  const states = new Map(hierarchy.topic_state.map((state) => [state.topic_id, state]));
  return <div className="org-navigation"><nav aria-label="Projects and channels" className="org-tree">
    {hierarchy.projects.map((project, projectIndex) => {
      const channels = hierarchy.channels.filter((channel) => channel.project_id === project.id);
      const canWriteProject = canCreate || project.project_role === "owner";
      const canManageProject = canManageOrganization || project.project_role === "owner";
      return <details key={project.id} open={!collapsedProjects.has(project.id)} onToggle={(event) => {
        const open = event.currentTarget.open;
        setCollapsedProjects(current => { if (current.has(project.id) === !open) return current; const next = new Set(current); if (open) next.delete(project.id); else next.add(project.id); return next; });
      }} className={`org-project org-project-tone-${projectIndex % 6}`}>
      <summary title={project.name} {...reveal("projects", project.id, project.name)}><span className="org-tree-label">{project.name}</span>{project.visibility === "private" && <span className="org-private-mark" aria-label="Private project" title="Private project">· private</span>}{spend("projects", project.id)}</summary>
      <span className="org-project-actions">{canWriteProject && <button type="button" className="org-tree-add" aria-label={`New channel in ${project.name}`}
        title={`New channel in ${project.name}`} onClick={() => onCreateChannel(project.id)}>+</button>}
        {canManageProject && <button type="button" className="org-tree-more" aria-label={`Settings for ${project.name}`} title={`Settings for ${project.name}`} onClick={() => onProjectSettings(project.id)}>⋯</button>}</span>
      {channels.map((channel, channelIndex) => {
        const topics = hierarchy.topics.filter((topic) => topic.channel_id === channel.id);
        const collapsed = collapsedChannels.has(channel.id);
        return <section key={channel.id} className={`org-channel org-tree-tone-${channelIndex % 4}`}>
        <div className="org-channel-heading"><button type="button" className="org-channel-name" title={`${collapsed ? "Expand" : "Collapse"} #${channel.name}`} {...reveal("channels", channel.id, "#" + channel.name)}
          aria-expanded={!collapsed} onClick={() => setCollapsedChannels((current) => {
            const next = new Set(current); if (next.has(channel.id)) next.delete(channel.id); else next.add(channel.id); return next;
          })}><span className="org-tree-label">#{channel.name}</span>{spend("channels", channel.id)}</button>
          <span className="org-channel-actions">{canWriteProject && <button type="button" className="org-tree-add" aria-label={`New topic in #${channel.name}`}
            title={`New topic in #${channel.name}`} onClick={() => onCreateTopic(channel.id)}>+</button>}
            {canManageProject && <button type="button" className="org-tree-more" aria-label={`Settings for #${channel.name}`} title={`Settings for #${channel.name}`} onClick={() => onChannelSettings(channel.id)}>⋯</button>}</span></div>
        {!collapsed && topics.map((topic, topicIndex) => {
          const state = states.get(topic.id);
          return <div key={topic.id} className="org-topic-row"><button type="button" className={`org-topic org-tree-tone-${topicIndex % 4} ${state?.subscribed === false ? "org-unfollowed" : ""}`}
            aria-current={selected === topic.id ? "page" : undefined} title={`${KIND_LABEL[topic.kind]}: ${topic.name}`} {...reveal("topics", topic.id, KIND_SYMBOL[topic.kind] + "-" + topic.name)}
            onClick={() => onSelect(topic.id)}>
            <span className="org-kind" aria-label={topic.kind + " topic"}>{KIND_SYMBOL[topic.kind]}</span><span className="org-topic-name">-{topic.name}</span>
            {!!state?.unread_mention_count && <span className="org-mention-count" aria-label={`${state.unread_mention_count} unread personal mentions`} title={`${state.unread_mention_count} unread personal mentions`}>@</span>}
            {!!state?.unread_count && state.subscribed && <span className="org-unread" aria-label={`${state.unread_count} unread messages`}>{state.unread_count}</span>}
            {spend("topics", topic.id)}
          </button>{canManageProject && <button type="button" className="org-tree-more" aria-label={`Settings for ${KIND_SYMBOL[topic.kind]}-${topic.name}`}
            title={`Settings for ${KIND_SYMBOL[topic.kind]}-${topic.name}`} onClick={() => onTopicSettings(topic.id)}>⋯</button>}</div>;
        })}
        {!collapsed && !topics.length && <p className="org-tree-empty">No topics</p>}
      </section>;
      })}
      {!channels.length && <p className="org-tree-empty">No channels</p>}
    </details>;
    })}
  </nav></div>;
}

function Dialog({ title, children, onClose }: { title: string; children: React.ReactNode; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => { dialog.current?.showModal(); }, []);
  return <dialog ref={dialog} className="org-dialog" aria-label={title} onCancel={onClose} onClose={onClose}>
    <header><h2>{title}</h2><button type="button" aria-label="Close dialog" onClick={onClose}>×</button></header>
    {children}
  </dialog>;
}

export function AccountMenu({ user, organization, organizations, canCreateOrganization, onSwitch, onAccount, onOrganization, onAdmin, onCreate, onKnowledge, onBots, onModels, onUsage, onSignOut, onShortcuts }: {
  user: Account; organization?: Organization; organizations: Organization[]; canCreateOrganization: boolean;
  onSwitch: (id: string) => void; onAccount: () => void; onOrganization: () => void; onAdmin: () => void; onCreate: () => void;
  onKnowledge: () => void; onModels: () => void; onUsage: () => void; onSignOut: () => void;
  onBots?: () => void;
  onShortcuts?: () => void;
}) {
  const [open, setOpen] = useState(false);
  const menu = useRef<HTMLDetailsElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (event: PointerEvent) => { if (!menu.current?.contains(event.target as Node)) setOpen(false); };
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") { setOpen(false); menu.current?.querySelector("summary")?.focus(); } };
    document.addEventListener("pointerdown", close); document.addEventListener("keydown", escape);
    return () => { document.removeEventListener("pointerdown", close); document.removeEventListener("keydown", escape); };
  }, [open]);
  const act = (action: () => void) => { setOpen(false); action(); };
  return <details className="org-account-menu" ref={menu} open={open}>
    <summary role="button" aria-label="Account menu" aria-expanded={open} onClick={(event) => { event.preventDefault(); setOpen(!open); }}>
      <span>{user.name}</span><span aria-hidden="true">⌃</span></summary>
    <div className="org-account-options">
      <header><strong>{user.name}</strong><small>{user.email}</small></header>
      <label className="org-org-selector"><span>Organization</span><select aria-label="Organization" value={organization?.id ?? ""}
        onChange={(event) => act(() => onSwitch(event.target.value))}>
        {!organization && <option value="">Choose organization</option>}
        {organizations.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
      </select></label>
      <section><small>Organization</small>
        {organization && <button onClick={() => { if (onBots) act(onBots); }}>My bots</button>}
        {organization && <button onClick={() => act(onOrganization)}>Information</button>}
        {organization && ["owner", "admin"].includes(organization.role) && <button onClick={() => act(onAdmin)}>Administration</button>}
        {canCreateOrganization && <button onClick={() => act(onCreate)}>New organization</button>}
      </section>
      <section><small>Models and usage</small>
        <button disabled={!organization} onClick={() => act(onModels)}>Model capabilities</button>
        <button disabled={!organization} onClick={() => act(onUsage)}>Usage and energy</button>
      </section>
      <section><small>Account</small><button onClick={() => act(onAccount)}>User information</button>
        {onShortcuts && <button onClick={() => act(onShortcuts)}>Keyboard shortcuts <kbd>?</kbd></button>}
        <button onClick={() => act(onSignOut)}>Sign out</button></section>
      <footer><a href="/licenses/Reasoning-NOTICE.txt">License notices</a></footer>
    </div>
  </details>;
}

function AuthForm({ mode, invitation, onMode, onAuthenticated }: {
  mode: "login" | "join" | "setup"; invitation: string; onMode: (mode: "login" | "join") => void;
  onAuthenticated: (session: Session) => Promise<void>;
}) {
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [token, setToken] = useState(invitation);
  const [organization, setOrganization] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const title = mode === "setup" ? "Create your organization" : mode === "join" ? "Join an organization" : "Sign in to Reasoning";
  const submit = async (event: React.FormEvent) => {
    event.preventDefault(); setBusy(true); setError("");
    try {
      const session = await request<Session>(mode === "login" ? "/auth/login" : mode === "join" ? "/invitations/accept" : "/setup", {
        method: "POST", body: mode === "login" ? { email, password } : mode === "join" ? { token, name, password }
          : { email, name, password, organization_name: organization, setup_token: token },
      });
      await onAuthenticated(session);
    } catch (cause) { setError(errorMessage(cause)); }
    finally { setPassword(""); setBusy(false); }
  };
  return <main className="org-auth"><div className="org-auth-card">
    <div className="org-wordmark"><img src="/reasoning.svg" alt="" />Reasoning</div>
    <h1>{title}</h1>
    <form onSubmit={submit}>
      {mode !== "login" && <label>{mode === "setup" ? "Setup credential" : "Invitation token"}<input name="token" type="password"
        required value={token} autoComplete="off" onChange={(event) => setToken(event.target.value)} /></label>}
      {mode !== "join" && <label>Email<input name="email" type="email" required autoComplete="username" value={email}
        onChange={(event) => setEmail(event.target.value)} autoFocus /></label>}
      {mode !== "login" && <label>Your name<input name="name" required maxLength={200} value={name} autoComplete="name"
        onChange={(event) => setName(event.target.value)} /></label>}
      <label>Password<input name="password" type="password" required minLength={mode === "login" ? undefined : 12} maxLength={1024}
        autoComplete={mode === "login" ? "current-password" : "new-password"} value={password}
        onChange={(event) => setPassword(event.target.value)} /></label>
      {mode === "setup" && <label>Organization name<input name="organization" required maxLength={200} value={organization}
        onChange={(event) => setOrganization(event.target.value)} /></label>}
      {error && <p role="alert" className="org-error">{error}</p>}
      <button className="org-primary" disabled={busy}>{busy ? "Working…" : mode === "login" ? "Sign in" : mode === "join" ? "Accept invitation" : "Create organization"}</button>
    </form>
    {mode === "join" && <p className="org-help">Your invitation determines your email address. Already registered? <button onClick={() => onMode("login")}>Sign in first</button>.</p>}
    {mode === "login" && <div className="org-help"><button onClick={() => onMode("join")}>Have an invitation?</button><p>For account recovery, contact your server administrator.</p></div>}
    {mode === "setup" && <p className="org-help">The host administrator can read the private setup-token file or complete setup with the server CLI.</p>}
    <footer>© Benjamin Antieau and Deven Manam · <a href="/licenses/Reasoning-Apache-2.0.txt">Apache-2.0</a></footer>
  </div></main>;
}

function CreationDialog({ kind, hierarchy, organizationId, parentId, members, userId, canCreatePrivate, onClose, onCreate }: {
  kind: Creation; hierarchy: Hierarchy; organizationId: string; parentId?: string; members: Member[]; userId: string; canCreatePrivate: boolean; onClose: () => void;
  onCreate: (body: Record<string, unknown>) => Promise<void>;
}) {
  const [name, setName] = useState("");
  const [parent, setParent] = useState(parentId ?? (kind === "channel" ? hierarchy.projects[0]?.id ?? "" : hierarchy.channels[0]?.id ?? ""));
  const [topicKind, setTopicKind] = useState<TopicKind>("alpha");
  const [allowedModels, setAllowedModels] = useState<string[]>([]);
  const [catalog, setCatalog] = useState<OrganizationPrices | null>(null);
  const [agentTools, setAgentTools] = useState<ResearchTool[]>([...RESEARCH_TOOLS]);
  const [defaultModel, setDefaultModel] = useState("");
  const [defaultEffort, setDefaultEffort] = useState("");
  const [creationBots, setCreationBots] = useState<BotModel[]>([]);
  const creationCatalog = useMemo(() => withBotModels(catalog, creationBots), [catalog, creationBots]);
  const creationModels = [...allowedModels, ...creationBots.map(bot => bot.id)];
  const [knowledgeTools, setKnowledgeTools] = useState<KnowledgeTool[]>(["ledger", "bibliography", "journal", "workflows"]);
  const [privateProject, setPrivateProject] = useState(false);
  const [projectMembers, setProjectMembers] = useState<string[]>([userId]);
  const [projectOwner, setProjectOwner] = useState(userId);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    if (kind !== "topic") return;
    const controller = new AbortController();
    void Promise.allSettled([
      request<InferenceConfiguration>(organizationPath(organizationId) + "/inference-configuration", { signal: controller.signal }).then(value => {
        if (!controller.signal.aborted) setAllowedModels(value.allowed_models);
        return value;
      }),
      request<OrganizationPrices>(organizationPath(organizationId) + "/model-prices", { signal: controller.signal }),
    ]).then(([configuration, prices]) => {
      if (controller.signal.aborted) return;
      if (configuration.status === "rejected") { if (!aborted(configuration.reason)) setError(errorMessage(configuration.reason)); return; }
      const modelCatalog = prices.status === "fulfilled" ? prices.value : null;
      setAllowedModels(configuration.value.allowed_models); setCatalog(modelCatalog);
      setDefaultModel(current => current || modelOptionGroups(configuration.value.allowed_models, modelCatalog).recommended[0]
        || configuration.value.allowed_models[0] || "");
    });
    return () => controller.abort();
  }, [kind, organizationId]);
  useEffect(() => {
    if (kind !== "topic" || !parent) return;
    const controller = new AbortController(); setCreationBots([]);
    void request<BotModel[]>(organizationPath(organizationId) + "/channels/" + encodeURIComponent(parent) + "/bot-models", {
      signal: controller.signal }).then(setCreationBots).catch(cause => { if (!aborted(cause)) setError(errorMessage(cause)); });
    return () => controller.abort();
  }, [kind, parent, organizationId]);
  useEffect(() => {
    if (!defaultModel || defaultModel.startsWith("bot/") && !creationBots.some(bot => bot.id === defaultModel)) {
      setDefaultModel(allowedModels[0] ?? creationBots[0]?.id ?? ""); setDefaultEffort("");
    }
  }, [creationBots, allowedModels, defaultModel]);
  const submit = async (event: React.FormEvent) => {
    event.preventDefault(); setBusy(true); setError("");
    try { await onCreate({ name, ...(kind === "project" && privateProject ? { visibility: "private", member_ids: projectMembers, owner_id: projectOwner }
      : kind === "channel" ? { project_id: parent } : kind === "topic"
      ? { channel_id: parent, kind: topicKind, default_model: topicKind === "beta" && defaultModel ? defaultModel : null,
        default_effort: topicKind === "beta" && availableEfforts(creationCatalog, defaultModel).includes(defaultEffort) ? defaultEffort : null,
        knowledge_tools: topicKind === "beta" ? knowledgeTools : null, agent_tools: topicKind === "beta" ? agentTools : null } : {}) }); onClose(); }
    catch (cause) { setError(errorMessage(cause)); }
    finally { setBusy(false); }
  };
  return <Dialog title={"New " + kind} onClose={onClose}><form onSubmit={submit}>
    <label>{kind[0].toUpperCase() + kind.slice(1)} name<input required autoFocus maxLength={200} value={name} onChange={(event) => setName(event.target.value)} /></label>
    {kind === "project" && canCreatePrivate && <label className="org-model-choice"><input type="checkbox" checked={privateProject} onChange={(event) => setPrivateProject(event.target.checked)} />Private project</label>}
    {kind === "project" && privateProject && <fieldset className="org-project-member-picker"><legend>Project members</legend>
      {members.map((member) => <label key={member.id}><input type="checkbox" checked={projectMembers.includes(member.id)} disabled={member.id === projectOwner}
        onChange={(event) => setProjectMembers((current) => event.target.checked ? [...current, member.id] : current.filter((id) => id !== member.id))} />{member.name}<small>{member.email}</small></label>)}
      <label>Project owner<select value={projectOwner} onChange={(event) => { setProjectOwner(event.target.value); setProjectMembers((current) => current.includes(event.target.value) ? current : [...current, event.target.value]); }}>
        {members.map((member) => <option key={member.id} value={member.id}>{member.name}</option>)}</select></label>
      <p className="org-help">Only selected members and organization administrators can see this project. Its owner can change this list later.</p>
    </fieldset>}
    {kind === "channel" && <label>Project<select value={parent} required onChange={(event) => setParent(event.target.value)}>
      {hierarchy.projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}
    </select></label>}
    {kind === "topic" && <>{!parentId && <label>Channel<select value={parent} required onChange={(event) => setParent(event.target.value)}>
      {hierarchy.channels.map((item) => <option key={item.id} value={item.id}>#{item.name}</option>)}
    </select></label>}<fieldset className="org-topic-kind"><legend>Topic type</legend>
      {(["alpha", "beta", "gamma"] as TopicKind[]).map((value) => <label key={value}>
        <input type="radio" name="org-topic-kind" value={value} checked={topicKind === value} onChange={() => {
          setTopicKind(value); if (value === "beta") setDefaultModel((current) => current || creationModels[0] || "");
        }} /><span aria-hidden="true">{KIND_SYMBOL[value]}</span><strong>{KIND_LABEL[value]}</strong>
      </label>)}</fieldset>{topicKind === "beta" && (creationModels.length ? <ModelPicker label="Model" models={creationModels} catalog={creationCatalog}
        value={defaultModel} onChange={setDefaultModel} effort={defaultEffort} onEffortChange={setDefaultEffort} disabled={busy} />
      : <p className="org-help">No models are enabled. An administrator can add one later.</p>)}
      {topicKind === "beta" && <details className="org-topic-tools"><summary><span>Tools</span><small>{agentTools.length === 6 ? "Standard" : `${agentTools.length} enabled`}</small></summary>
        {RESEARCH_TOOLS.map((tool) => <label className="org-model-choice" key={tool}>
          <input type="checkbox" checked={agentTools.includes(tool)} onChange={(event) => setAgentTools((current) => event.target.checked ? [...current, tool] : current.filter((item) => item !== tool))} />{TOOL_LABELS[tool]}
        </label>)}
        <details><summary>Knowledge sources</summary>
        {(["ledger", "bibliography", "journal", "workflows"] as KnowledgeTool[]).map((tool) => <label className="org-model-choice" key={tool}>
          <input type="checkbox" checked={knowledgeTools.includes(tool)} onChange={(event) => setKnowledgeTools((current) => event.target.checked ? [...current, tool] : current.filter((item) => item !== tool))} />
          {tool === "journal" ? "Research journal" : tool === "workflows" ? "Workflows" : tool[0].toUpperCase() + tool.slice(1)}
        </label>)}</details></details>}</>}
    {error && <p role="alert" className="org-error">{error}</p>}
    <button className="org-primary" disabled={busy || (kind === "topic" && topicKind === "beta" && allowedModels.length > 0 && !defaultModel)}>{busy ? "Creating…" : "Create " + kind}</button>
  </form></Dialog>;
}

type DeletionPlan = { name: string; counts: Record<string, number>; active: Record<string, number>; can_delete: boolean; plan_token: string;
  blockers?: { kind: string; id: string; topic_id: string; status: string; action: string }[] };

function DeletionControl({ organizationId, kind, entityId, onDeleted }: {
  organizationId: string; kind: "project" | "channel" | "topic"; entityId: string; onDeleted: () => void;
}) {
  const [plan, setPlan] = useState<DeletionPlan | null>(null), [confirmation, setConfirmation] = useState("");
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const root = organizationPath(organizationId) + "/" + kind + "s/" + encodeURIComponent(entityId);
  const inspect = async () => {
    setBusy(true); setError(""); setConfirmation("");
    try { setPlan(await request<DeletionPlan>(root + "/deletion")); }
    catch (cause) { setError(errorMessage(cause)); } finally { setBusy(false); }
  };
  return <section className="org-delete-control">
    {!plan ? <button type="button" className="org-danger" disabled={busy} onClick={() => void inspect()}>{busy ? "Checking activity…" : "Delete " + kind}</button>
      : <form onSubmit={(event) => { event.preventDefault(); if (confirmation !== plan.name) return;
        setBusy(true); setError("");
        void request(root, { method: "DELETE", body: { confirmation, plan_token: plan.plan_token, stop_active: true } })
          .then(onDeleted).catch((cause) => { setError(errorMessage(cause)); }).finally(() => setBusy(false));
      }}><h3>Delete {plan.name}?</h3>
        <p>This removes {Object.entries(plan.counts).filter(([, count]) => count > 0).map(([label, count]) => `${count} ${count === 1 ? label.replace(/s$/, "") : label.replace("_", " ")}`).join(", ")}
          {Object.values(plan.counts).some((count) => count > 0) ? "." : "this " + kind + "."} Bot access is revoked. Billing and audit records are retained.</p>
        {!plan.can_delete && <p role="status">Active work will be stopped and pending approvals revoked. Unknown charges and execution receipts remain retained; external work may take time to stop.</p>}
        <label><span>Type <strong>{plan.name}</strong> to confirm</span><input aria-label="Deletion confirmation" autoComplete="off" value={confirmation} onChange={(event) => setConfirmation(event.target.value)} /></label>
        <div className="org-delete-actions"><button type="submit" className="org-danger" disabled={busy || confirmation !== plan.name}>{busy ? "Deleting…" : "Delete " + kind}</button>
          <button type="button" disabled={busy} onClick={() => void inspect()}>Refresh counts</button><button type="button" disabled={busy} onClick={() => { setPlan(null); setError(""); }}>Cancel</button></div>
      </form>}
    {error && <p role="alert" className="org-error">{error}</p>}
  </section>;
}

function ProjectSettingsDialog({ organizationId, project, members, canDelete, onClose, onChanged }: {
  organizationId: string; project: Project; members: Member[]; canDelete: boolean; onClose: () => void; onChanged: () => void;
}) {
  const [name, setName] = useState(project.name), [projectMembers, setProjectMembers] = useState<ProjectMember[]>([]);
  const [candidate, setCandidate] = useState(""), [candidateRole, setCandidateRole] = useState<"owner" | "member">("member");
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const root = organizationPath(organizationId) + "/projects/" + encodeURIComponent(project.id);
  const reload = useCallback(async () => {
    if (project.visibility !== "private") return;
    setProjectMembers(await request<ProjectMember[]>(root + "/members"));
  }, [project.visibility, root]);
  useEffect(() => { void reload().catch((cause) => setError(errorMessage(cause))); }, [reload]);
  const action = async (work: () => Promise<unknown>) => {
    setBusy(true); setError("");
    try { await work(); onChanged(); await reload(); } catch (cause) {
      if (cause instanceof ApiError && cause.status === 403) { onChanged(); onClose(); } else setError(errorMessage(cause));
    } finally { setBusy(false); }
  };
  const available = members.filter((member) => !projectMembers.some((item) => item.id === member.id));
  return <Dialog title="Project settings" onClose={onClose}>
    {error && <p role="alert" className="org-error">{error}</p>}
    <form onSubmit={(event) => { event.preventDefault(); void action(() => request(root, { method: "PATCH", body: { name } })); }}>
      <label>Project name<input required maxLength={200} value={name} onChange={(event) => setName(event.target.value)} /></label>
      <button disabled={busy || name === project.name}>Save name</button>
    </form>
    <p className="org-help">{project.visibility === "private" ? "Private · visible only to this roster and organization administrators." : "Organization project · visible to every organization member."}</p>
    {project.visibility === "private" && <section className="org-project-members"><h3>Members</h3>
      {!!available.length && <form onSubmit={(event) => { event.preventDefault(); if (!candidate) return; void action(async () => {
        await request(root + "/members/" + encodeURIComponent(candidate), { method: "PUT", body: { role: candidateRole } }); setCandidate("");
      }); }}><label>Add member<select required value={candidate} onChange={(event) => setCandidate(event.target.value)}><option value="">Choose a person</option>
        {available.map((member) => <option key={member.id} value={member.id}>{member.name} · {member.email}</option>)}</select></label>
        <label>Project role<select value={candidateRole} onChange={(event) => setCandidateRole(event.target.value as "owner" | "member")}><option value="member">Member</option><option value="owner">Owner</option></select></label>
        <button disabled={busy || !candidate}>Add</button></form>}
      <ul className="org-admin-list">{projectMembers.map((member) => <li key={member.id}><div><strong>{member.name}</strong><small>{member.email}</small></div>
        <select aria-label={`Project role for ${member.name}`} value={member.role} disabled={busy} onChange={(event) => void action(() => request(root + "/members/" + member.id, { method: "PUT", body: { role: event.target.value } }))}>
          <option value="owner">Owner</option><option value="member">Member</option></select>
        <button disabled={busy} onClick={() => { if (window.confirm(`Remove ${member.name} from ${project.name}?`)) void action(() => request(root + "/members/" + member.id, { method: "DELETE" })); }}>Remove</button>
      </li>)}</ul>
    </section>}
    {canDelete && <DeletionControl organizationId={organizationId} kind="project" entityId={project.id} onDeleted={() => { onChanged(); onClose(); }} />}
  </Dialog>;
}

function ChannelSettingsDialog({ organizationId, channel, onClose, onChanged }: {
  organizationId: string; channel: Channel; onClose: () => void; onChanged: () => void;
}) {
  const [name, setName] = useState(channel.name), [busy, setBusy] = useState(false), [error, setError] = useState("");
  return <Dialog title="Channel settings" onClose={onClose}><form onSubmit={(event) => { event.preventDefault(); setBusy(true); setError("");
    void request(organizationPath(organizationId) + "/channels/" + encodeURIComponent(channel.id), { method: "PATCH", body: { name } })
      .then(() => { onChanged(); onClose(); }).catch((cause) => setError(errorMessage(cause))).finally(() => setBusy(false));
  }}><label>Channel name<input required autoFocus maxLength={200} value={name} onChange={(event) => setName(event.target.value)} /></label>
    {error && <p role="alert" className="org-error">{error}</p>}<button disabled={busy || name === channel.name}>Save name</button></form>
    <DeletionControl organizationId={organizationId} kind="channel" entityId={channel.id} onDeleted={() => { onChanged(); onClose(); }} /></Dialog>;
}

function TopicRenameControl({ organizationId, topic, onChanged }: { organizationId: string; topic: Topic; onChanged: () => void }) {
  const [name, setName] = useState(topic.name), [busy, setBusy] = useState(false), [error, setError] = useState("");
  return <form onSubmit={(event) => { event.preventDefault(); setBusy(true); setError("");
    void request(topicPath(organizationId, topic.id), { method: "PATCH", body: { name } })
      .then(onChanged).catch(cause => setError(errorMessage(cause))).finally(() => setBusy(false));
  }}><label>Topic name<input required maxLength={200} value={name} onChange={event => setName(event.target.value)} /></label>
    {error && <p role="alert" className="org-error">{error}</p>}<button disabled={busy || name === topic.name}>Save name</button></form>;
}

type CredentialSource = { organization: Organization; key: KeyMetadata; policy: Policy };

function OrganizationCreationDialog({ organizations, onClose, onCreate }: {
  organizations: Organization[]; onClose: () => void; onCreate: (body: Record<string, unknown>) => Promise<void>;
}) {
  const administrators = organizations.filter((item) => ["owner", "admin"].includes(item.role));
  const [name, setName] = useState("");
  const [mode, setMode] = useState<"new" | "reuse" | "none">("new");
  const [label, setLabel] = useState("Primary");
  const [key, setKey] = useState("");
  const [budget, setBudget] = useState("");
  const [sources, setSources] = useState<CredentialSource[]>([]);
  const [source, setSource] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    let alive = true;
    void Promise.all(administrators.map(async (organization) => {
      const root = organizationPath(organization.id);
      const [keys, policy] = await Promise.all([request<KeyMetadata[]>(root + "/provider-keys"), request<Policy>(root + "/policy")]);
      return keys.filter((item) => item.disabled_at === null).map((item) => ({ organization, key: item, policy }));
    })).then((groups) => {
      if (!alive) return;
      const available = groups.flat(); setSources(available);
      if (available[0]) {
        setSource(available[0].organization.id + ":" + available[0].key.id);
        setBudget((available[0].policy.monthly_budget_microusd / 1_000_000).toFixed(6).replace(/\.?0+$/, ""));
      }
    }).catch((cause) => { if (alive) setError(errorMessage(cause)); });
    return () => { alive = false; };
  }, []);
  const submit = async (event: React.FormEvent) => {
    event.preventDefault(); setBusy(true); setError("");
    try {
      let body: Record<string, unknown> = { name, credential_mode: mode };
      if (mode === "new") body = { ...body, key_label: label, key,
        monthly_budget_microusd: decimalBudget(budget) };
      if (mode === "reuse") {
        const [source_organization_id, source_key_id] = source.split(":");
        body = { ...body, source_organization_id, source_key_id };
      }
      await onCreate(body); onClose();
    } catch (cause) { setError(errorMessage(cause)); }
    finally { setBusy(false); }
  };
  return <Dialog title="New organization" onClose={onClose}><form onSubmit={submit}>
    <label>Organization name<input required autoFocus maxLength={200} value={name} onChange={(event) => setName(event.target.value)} /></label>
    <label>AI access<select value={mode} onChange={(event) => setMode(event.target.value as typeof mode)}>
      <option value="new">Use a fresh OpenRouter key (recommended)</option>
      <option value="reuse" disabled={!sources.length}>Explicitly reuse an organization key</option>
      <option value="none">Set up AI later</option>
    </select></label>
    {mode === "new" && <div className="org-credential-choice">
      <p className="org-help">Create a separate upstream key for clear billing and revocation boundaries.</p>
      <label>Key label<input required maxLength={200} value={label} onChange={(event) => setLabel(event.target.value)} /></label>
      <label>OpenRouter API key<input required type="password" minLength={8} maxLength={4096} autoComplete="off" value={key} onChange={(event) => setKey(event.target.value)} /></label>
      <p className="org-help">This key can use any available OpenRouter model or router, subject to its upstream restrictions and your organization budget.</p>
      <label>Monthly LLM budget (USD)<input required inputMode="decimal" value={budget} onChange={(event) => setBudget(event.target.value)} /></label>
    </div>}
    {mode === "reuse" && <div className="org-credential-choice"><p className="org-help">This copies the selected credential and budget into a separate encrypted organization record.</p>
      <label>Existing key<select required value={source} onChange={(event) => setSource(event.target.value)}>
        {sources.map((item) => <option key={item.organization.id + item.key.id} value={item.organization.id + ":" + item.key.id}>
          {item.organization.name} · {item.key.label} · {item.key.fingerprint}
        </option>)}
      </select></label></div>}
    {mode === "none" && <p className="org-help">The organization starts without provider credentials or an LLM budget.</p>}
    {error && <p role="alert" className="org-error">{error}</p>}
    <button className="org-primary" disabled={busy || (mode === "reuse" && !source)}>{busy ? "Creating…" : "Create organization"}</button>
  </form></Dialog>;
}

export function BetaModelMenu({ configuration, catalog, selected, tools, disabled, error, onSelect, effort = "", onEffortChange }: {
  configuration: Pick<InferenceConfiguration, "allowed_models"> | null; catalog: OrganizationPrices | null; selected: string; disabled: boolean;
  tools: KnowledgeTool[]; error: string; onSelect: (model: string) => void;
  effort?: string; onEffortChange?: (effort: string) => void;
}) {
  const metadata = catalog?.models.find((model) => model.id === selected);
  const maximum = metadata?.max_completion_tokens ?? 8192;
  const sourceLabels = tools.map((tool) => tool === "journal" ? "journal" : tool === "workflows" ? "workflows" : tool);
  return <ModelPicker label="Choose reply model" models={configuration?.allowed_models ?? []} catalog={catalog} value={selected}
    onChange={onSelect} effort={effort} onEffortChange={onEffortChange} disabled={disabled}>
      {error && <p className="org-error">{error}</p>}
      {!configuration && !error && <p role="status">Loading models…</p>}
      {configuration && !configuration.allowed_models.length && <p>No models are enabled for this organization.</p>}
      <section className="org-context-summary"><strong>Context</strong>
        <p>Cited memory + token-budgeted recent conversation · {sourceLabels.length ? sourceLabels.join(", ") : "conversation only"}</p>
        {selected && <p>No application output-token cap · provider capacity {compactTokens(maximum)} tokens · research continues within authorized budgets</p>}
        <details><summary>Details</summary><p>The selected model receives this topic through your new message, research memory, and granted sources. Tools can recover earlier context. Inspect context in research settings for the exact input.</p></details>
      </section>
  </ModelPicker>;
}

export function peopleWithBots(presence: Presence | null) {
  return (presence?.humans ?? []).map(person => ({ ...person,
    bots: (presence?.bots ?? []).filter(bot => bot.owner_user_id === person.id),
  })).filter(person => person.online || person.bots.length > 0);
}

function PresenceSidebar({ organizationId, open, onClose, onAuthLost }: {
  organizationId: string; open: boolean; onClose: () => void; onAuthLost: () => void;
}) {
  const [presence, setPresence] = useState<Presence | null>(null);
  const [reviewBot, setReviewBot] = useState<Presence["bots"][number] | null>(null);
  const approvals = useBotApprovals(organizationId, open, onAuthLost);
  useEffect(() => { setReviewBot(null); }, [organizationId, open]);
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try { setPresence(await request<Presence>(organizationPath(organizationId) + "/presence", { signal: controller.signal })); }
      catch (cause) { if (!aborted(cause) && cause instanceof ApiError && cause.status === 401) onAuthLost(); }
      if (!controller.signal.aborted) timer = schedulePoll(poll, 3000, controller.signal);
    };
    void poll(); return () => { controller.abort(); clearTimeout(timer); };
  }, [organizationId, open, onAuthLost]);
  if (!open) return null;
  const people = peopleWithBots(presence);
  const connected = (presence?.humans.filter(person => person.online).length ?? 0) + (presence?.bots.filter(bot => bot.online).length ?? 0);
  return <aside className="org-presence" aria-label="Online people and bots"><header><div><strong>People</strong><small>{connected} connected</small></div>
    <button aria-label="Collapse online sidebar" onClick={onClose}>×</button></header>
    {!presence && <p role="status">Checking presence…</p>}
    {people.map(person => <section key={person.id} data-person-id={person.id}>
      <div className="org-presence-row"><span className="org-presence-avatar">{person.name.slice(0, 1).toUpperCase()}</span><strong title={person.role}>{person.name}</strong><i aria-label={person.online ? "online" : "offline"} /></div>
      {person.bots.map(bot => <div className="org-presence-row org-person-bot" key={bot.id} data-bot-id={bot.id}>
        <span className="org-presence-avatar org-bot-avatar" aria-label={bot.developer ? "Developer bot" : "Bot"}>{bot.developer ? "⬡" : "◆"}</span><strong title={[bot.engine === "claude" ? "Claude Code" : bot.engine === "codex" ? "Codex" : "Bot", ...(bot.models ?? []), bot.online ? "online" : "offline", bot.developer ? "developer tools" : ""].filter(Boolean).join(" · ")}>{approvals.rows.some(row => row.bot_id === bot.id)
          ? <button type="button" className="org-bot-review-name" onClick={() => setReviewBot(bot)}>{bot.name}</button> : bot.name}</strong>
        {approvals.rows.some(row => row.bot_id === bot.id) ? <button type="button" className="org-bot-approval-indicator"
          aria-label={`Review ${approvals.rows.filter(row => row.bot_id === bot.id).length} pending requests from ${bot.name}`}
          title="Owner approval needed" onClick={() => setReviewBot(bot)}>!</button> : <i className={bot.activity ? "org-bot-thinking" : ""}
          title={bot.activity?.replaceAll("_", " ")} aria-label={bot.activity?.replaceAll("_", " ") ?? (bot.online ? "online" : "offline")} />
        }
      </div>)}
    </section>)}
    {presence && !people.length && <p className="org-help">No active connections.</p>}
    {approvals.error && <p className="org-error" role="alert">Approvals: {approvals.error}</p>}
    {reviewBot && <BotApprovalDialog key={organizationId + reviewBot.id} organizationId={organizationId}
      bot={presence?.bots.find(bot => bot.id === reviewBot.id) ?? reviewBot} rows={approvals.rows.filter(row => row.bot_id === reviewBot.id)}
      onClose={() => setReviewBot(null)} onDecided={id => { approvals.remove(id); approvals.refresh(); }} />}
  </aside>;
}

function Conversation({ organizationId, topic, members, user, canWrite, canManage, draft, updateDraft, anchor, onPreference, onForked, onAuthLost, modelAttempt, onModelAttempt, computeAttempt, onComputeAttempt, workflowAttempt, onWorkflowAttempt, workflowExecutionAttempt, onWorkflowExecutionAttempt }: {
  organizationId: string; topic: Topic; members: Member[]; user: Account; canWrite: boolean; canManage: boolean;
  draft: Draft; updateDraft: (draft: Draft) => void; anchor: number | null;
  onPreference: () => void; onAuthLost: () => void;
  onForked: (topicId: string) => void;
  modelAttempt?: ModelAttempt; onModelAttempt: (attempt?: ModelAttempt) => void;
  computeAttempt?: ComputeAttempt; onComputeAttempt: (attempt?: ComputeAttempt) => void;
  workflowAttempt?: WorkflowAttempt; onWorkflowAttempt: (attempt?: WorkflowAttempt) => void;
  workflowExecutionAttempt?: WorkflowExecutionAttempt; onWorkflowExecutionAttempt: (attempt?: WorkflowExecutionAttempt) => void;
}) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [loading, setLoading] = useState(true);
  const [online, setOnline] = useState(true);
  const [error, setError] = useState("");
  const [sending, setSending] = useState(false);
  const [older, setOlder] = useState(true);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [atBottom, setAtBottom] = useState(anchor === null);
  const [mention, setMention] = useState<MentionQuery | null>(null);
  const [mentionIndex, setMentionIndex] = useState(0);
  const [topicBots, setTopicBots] = useState<Pick<Member, "id" | "name">[]>([]);
  const [inferenceConfiguration, setInferenceConfiguration] = useState<InferenceConfiguration | null>(null);
  const [modelCatalog, setModelCatalog] = useState<OrganizationPrices | null>(null);
  const [botModels, setBotModels] = useState<BotModel[]>([]);
  const [texOpen, setTexOpen] = useState(false);
  const attachmentControl = useRef<HTMLSpanElement>(null);
  useTopicCommand(topic.id, command => {
    if (command === "tex" && canWrite) setTexOpen(true);
    if (command === "attach" && canWrite) attachmentControl.current?.querySelector<HTMLButtonElement>("button")?.click();
  });
  const replyCatalog = useMemo(() => withBotModels(modelCatalog, botModels), [modelCatalog, botModels]);
  const replyConfiguration = useMemo(() => inferenceConfiguration || botModels.length ? {
    allowed_models: [...(inferenceConfiguration?.allowed_models ?? []), ...botModels.map(model => model.id)],
  } : null, [inferenceConfiguration, botModels]);
  const botNames = useMemo(() => Object.fromEntries(topicBots.map(bot => [bot.id, bot.name])), [topicBots]);
  const modelStorageKey = `reasoning-model:${user.id}:${organizationId}:${topic.id}`;
  const [selectedModel, setSelectedModel] = useState(() => {
    try { return localStorage.getItem(modelStorageKey) ?? topic.default_model ?? ""; }
    catch { return topic.default_model ?? ""; }
  });
  const effortKey = `reasoning-effort:${user.id}:${organizationId}:${topic.id}:${selectedModel}`;
  const [effort, setEffort] = useState("");
  useEffect(() => { const initial = selectedModel === topic.default_model ? topic.default_effort ?? "" : "";
    try { setEffort(localStorage.getItem(effortKey) ?? initial); } catch { setEffort(initial); } }, [effortKey, topic.default_effort, topic.default_model]);
  const chooseEffort = (value: string) => { setEffort(value); try { localStorage.setItem(effortKey, value); } catch { /* Optional storage. */ } };
  const selectedEffort = availableEfforts(replyCatalog, selectedModel).includes(effort) ? effort : null;
  const [promptRevision, setPromptRevision] = useState(0);
  useEffect(() => { try { if (selectedModel) localStorage.setItem(modelStorageKey, selectedModel); } catch { /* Storage is optional. */ } }, [modelStorageKey, selectedModel]);
  const [modelError, setModelError] = useState("");
  const [goalDraft, setGoalDraft] = useState<string | null>(null);
  const [composerTools, setComposerTools] = useState<HTMLDivElement | null>(null);
  const [workflowActivity, setWorkflowActivity] = useState<TopicActivity[]>([]), [computeActivity, setComputeActivity] = useState<TopicActivity[]>([]);
  const [betaActivity, setBetaActivity] = useState<TopicActivity[]>([]);
  const [botActivity, setBotActivity] = useState<TopicActivity[]>([]);
  const viewport = useRef<HTMLDivElement>(null);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const cursor = useRef(0);
  const editCursor = useRef(0);
  const acknowledged = useRef(0);
  const alive = useRef(true);
  const bottom = useRef(anchor === null);
  const currentDraft = useRef(draft);
  currentDraft.current = draft;
  const root = topicPath(organizationId, topic.id);
  useEffect(() => {
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    const refreshBots = async () => {
      try {
        const value = await request<Pick<Member, "id" | "name">[]>(root + "/bots", { signal: controller.signal });
        if (!controller.signal.aborted) setTopicBots(value);
      } catch { if (!controller.signal.aborted) setTopicBots([]); }
      if (!controller.signal.aborted) timer = schedulePoll(refreshBots, 10000, controller.signal);
    };
    void refreshBots();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [root]);
  useEffect(() => {
    if (!viewport.current) return;
    let frame = 0;
    const observer = new MutationObserver(() => {
      cancelAnimationFrame(frame);
      if (bottom.current && !document.querySelector(".chat-action-menu") && !window.getSelection()?.toString())
        frame = requestAnimationFrame(() => {
          if (viewport.current && !document.querySelector(".chat-action-menu") && !window.getSelection()?.toString())
            viewport.current.scrollTop = viewport.current.scrollHeight;
        });
    });
    observer.observe(viewport.current, { childList: true, characterData: true, subtree: true });
    return () => { observer.disconnect(); cancelAnimationFrame(frame); };
  }, [topic.kind]);
  useEffect(() => {
    const resize = () => { if (textarea.current) resizeComposer(textarea.current); };
    resize(); window.addEventListener("resize", resize);
    return () => window.removeEventListener("resize", resize);
  }, [draft.text]);
  const botMentionNamesKey = topicBots.map(bot => `${bot.id}:${bot.name}`).sort().join("\0");
  useEffect(() => {
    const input = textarea.current;
    if (!input || document.activeElement !== input) return;
    // A user can type a spaced bot name before its asynchronous catalog arrives.
    // Re-evaluate the current caret on actual identity/name changes, not routine polling.
    setMention(mentionQuery(input.value, input.selectionStart, topicBots.map(bot => bot.name)));
    setMentionIndex(0);
  }, [botMentionNamesKey]);
  const mentionPeople = [...members, ...topicBots];
  const boundMention = mention && draft.mentions?.find(binding => binding.start === mention.start);
  const modelMentionBots = mention ? topicBots.filter(bot => mention.query.toLocaleLowerCase().startsWith(bot.name.toLocaleLowerCase() + "-")
    && (!boundMention || boundMention.id === bot.id)).sort((a, b) => b.name.length - a.name.length) : [];
  const longestName = modelMentionBots[0]?.name.length;
  const mentionMembers: (Pick<Member, "id" | "name"> & { model?: string; effort?: string; optionId?: string; owner?: string })[] = !mention ? [] : modelMentionBots.length
    ? botModels.flatMap(model => {
      const bot = modelMentionBots.find(bot => bot.id === model.bot_id && bot.name.length === longestName);
      if (!bot) return [];
      const id = model.id.split("/").slice(2).join("/");
      const [queryModel, queryEffort] = mention.query.slice(bot.name.length + 1).toLocaleLowerCase().split("!");
      if (!id.toLocaleLowerCase().includes(queryModel)) return [];
      return (queryEffort === undefined ? [""] : (model.efforts ?? []).filter(value => value.startsWith(queryEffort)))
        .map(effort => ({ id: bot.id, name: bot.name, model: id, effort, optionId: model.id + "!" + effort,
          owner: modelMentionBots.length > 1 ? members.find(member => member.id === model.owner_user_id)?.name ?? "" : "" }));
    })
    : mentionPeople.filter(member => member.name.toLocaleLowerCase().includes(mention.query.toLocaleLowerCase())).slice(0, 20);
  const chooseMention = (member: Pick<Member, "id" | "name"> & { model?: string; effort?: string }) => {
    if (!mention) return;
    const label = `@${member.name}${member.model ? "-" + member.model : ""}${member.effort ? "!" + member.effort : ""}`, token = label + " ";
    const text = draft.text.slice(0, mention.start) + token + draft.text.slice(mention.end);
    updateDraft({ text, clientId: crypto.randomUUID(), attempted: false,
      mentions: [...editMentions(draft.text, text, draft.mentions), { start: mention.start, end: mention.start + label.length,
        id: member.id, label, model: member.model, effort: member.effort }] });
    const position = mention.start + token.length;
    setMention(null);
    requestAnimationFrame(() => { textarea.current?.focus(); textarea.current?.setSelectionRange(position, position); });
  };

  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; };
  }, []);

  useEffect(() => {
    if (topic.kind !== "beta") return;
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>; setModelError("");
    const refreshConfiguration = async () => {
      try {
        const configuration = await request<InferenceConfiguration>(organizationPath(organizationId) + "/inference-configuration", {
          signal: controller.signal });
        if (controller.signal.aborted) return;
        setInferenceConfiguration(configuration);
        setSelectedModel((current) => configuration.allowed_models.includes(current) || current.startsWith("bot/") ? current
          : topic.default_model && configuration.allowed_models.includes(topic.default_model) ? topic.default_model
            : configuration.allowed_models[0] ?? "");
      } catch (cause) {
        if (aborted(cause)) return;
        if (cause instanceof ApiError && cause.status === 401) onAuthLost(); else setModelError(errorMessage(cause));
      }
      if (!controller.signal.aborted) timer = schedulePoll(refreshConfiguration, 3000, controller.signal);
    };
    void refreshConfiguration();
    void request<OrganizationPrices>(organizationPath(organizationId) + "/model-prices", { signal: controller.signal })
      .then((catalog) => { if (!controller.signal.aborted) setModelCatalog(catalog); })
      .catch((cause) => {
        if (aborted(cause)) return;
        if (cause instanceof ApiError && cause.status === 401) onAuthLost(); else setModelError(errorMessage(cause));
      });
    return () => { controller.abort(); clearTimeout(timer); };
  }, [organizationId, topic.kind, topic.default_model, onAuthLost]);
  useEffect(() => {
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      try {
        const value = await request<BotModel[]>(root + "/bot-models", { signal: controller.signal });
        if (!controller.signal.aborted) setBotModels(value);
      } catch { /* Keep the last known model choices during a transient refresh failure. */ }
      if (!controller.signal.aborted) timer = schedulePoll(refresh, topic.kind === "beta" ? 3000 : 10000, controller.signal);
    };
    void refresh();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [root, topic.kind]);

  useEffect(() => {
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const query = cursor.current ? `after=${cursor.current}&limit=200` : `before=${anchor === null ? Number.MAX_SAFE_INTEGER : anchor + 1}&limit=100`;
        const [incoming, updates] = await Promise.all([
          request<Message[]>(root + "/messages?" + query, { signal: controller.signal }).then(incoming => {
            if (!controller.signal.aborted && !cursor.current) {
              setMessages(existing => mergeMessages(existing, incoming)); setLoading(false);
            }
            return incoming;
          }),
          request<{ after: number; messages: Message[] }>(root + "/message-updates?after=" + editCursor.current, { signal: controller.signal }),
        ]);
        if (controller.signal.aborted) return;
        if (!updates || !Array.isArray(updates.messages) || !Number.isSafeInteger(updates.after) || updates.after < editCursor.current)
          throw new Error("Invalid message update response; reconnecting.");
        if (!cursor.current) setOlder(incoming.length === 100);
        cursor.current = incoming.at(-1)?.sequence ?? cursor.current;
        editCursor.current = updates.after;
        setMessages((existing) => mergeMessages(mergeMessages(existing, incoming), updates.messages.filter(message =>
          existing.some(prior => prior.id === message.id) || incoming.some(prior => prior.id === message.id))));
        setOnline(true); setLoading(false);
        if (incoming.some((message) => message.client_id === currentDraft.current.clientId && message.author_id === user.id)) {
          updateDraft(newDraft()); setError("");
        }
      } catch (cause) {
        if (aborted(cause)) return;
        if (cause instanceof ApiError && cause.status === 401) { onAuthLost(); return; }
        setOnline(false); setError(errorMessage(cause)); setLoading(false);
      }
      if (!controller.signal.aborted) timer = schedulePoll(poll, 2000, controller.signal);
    };
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [root, anchor, user.id, onAuthLost]);

  useEffect(() => {
    if (bottom.current && viewport.current && !document.querySelector(".chat-action-menu") && !window.getSelection()?.toString())
      viewport.current.scrollTop = viewport.current.scrollHeight;
  }, [messages]);

  useEffect(() => {
    const mark = () => {
      const sequence = messages.at(-1)?.sequence ?? 0;
      if (!bottom.current || document.visibilityState !== "visible" || !document.hasFocus() || sequence <= acknowledged.current) return;
      void request(root + "/read", { method: "PUT", body: { sequence } }).then(() => {
        acknowledged.current = Math.max(acknowledged.current, sequence); onPreference();
      }).catch((cause) => { if (cause instanceof ApiError && cause.status === 401) onAuthLost(); });
    };
    mark(); window.addEventListener("focus", mark); document.addEventListener("visibilitychange", mark);
    return () => { window.removeEventListener("focus", mark); document.removeEventListener("visibilitychange", mark); };
  }, [messages, atBottom, root, onAuthLost]);

  const chooseCommand = (command: TopicCommand) => {
    updateDraft(newDraft()); setMention(null);
    dispatchTopicCommand(topic.id, command);
    textarea.current?.focus();
  };
  const send = async (event?: React.FormEvent) => {
    event?.preventDefault();
    if (!canWrite || sending || !online || !draft.text.trim() || (topic.kind === "beta" && !!modelAttempt)) return;
    const command = TOPIC_COMMANDS.find(command => draft.text.trim().toLowerCase() === "/" + command.id
      && (command.kinds as readonly string[]).includes(topic.kind));
    if (command) { chooseCommand(command.id); return; }
    if (topic.kind === "beta") {
      const objective = goalCommand(draft.text);
      if (objective !== null) { setGoalDraft(objective); return; }
    }
    const attempt = prepareMessageAttempt(draft);
    updateDraft(attempt); setSending(true); setError("");
    try {
      const wireText = encodeMentions(attempt.text, attempt.mentions);
      const message = await request<Message>(root + "/messages", { method: "POST", body: { content: wireText, client_id: attempt.clientId } });
      if (!alive.current) return;
      setMessages((existing) => mergeMessages(existing, [message]));
      // The incremental cursor advances only from a contiguous poll page, not a
      // POST result: another member may have posted intervening messages.
      if (currentDraft.current.clientId === attempt.clientId) updateDraft(newDraft());
      bottom.current = true; setAtBottom(true);
      const control = topic.kind === "beta" ? agentCommand(attempt.text) : null;
      if (control) {
        await request(root + "/agents/control", { method: "POST", body: { status: control } });
      } else if (topic.kind === "beta" && selectedModel && !topicBots.some((bot) => wireText.includes(`@[${bot.id}]`) || wireText.includes(`@[${bot.id}:`))) {
        const metadata = modelCatalog?.models.find((model) => model.id === selectedModel);
        const modelRequest = automaticModelAttempt(selectedModel, message.sequence, metadata?.max_completion_tokens ?? 8192);
        if (selectedEffort) modelRequest.effort = selectedEffort;
        onModelAttempt(modelRequest);
        try {
          await submitModelAttempt(organizationId, topic.id, modelRequest);
          if (alive.current) onModelAttempt(undefined);
        } catch (cause) {
          if (!alive.current) return;
          if (cause instanceof ApiError && cause.status >= 400 && cause.status < 500 && cause.status !== 408) onModelAttempt(undefined);
          if (cause instanceof ApiError && cause.status === 401) onAuthLost();
          else setError(`Message sent, but ${errorMessage(cause).replace(/^./, (letter) => letter.toLowerCase())}`);
        }
      }
    } catch (cause) {
      if (!alive.current) return;
      if (cause instanceof ApiError && cause.status === 401) onAuthLost();
      else setError(errorMessage(cause));
    } finally { if (alive.current) setSending(false); }
  };

  const loadOlder = async () => {
    if (!messages.length || loadingOlder) return;
    setLoadingOlder(true);
    const height = viewport.current?.scrollHeight ?? 0;
    const scroll = viewport.current?.scrollTop ?? 0;
    try {
      const page = await request<Message[]>(root + `/messages?before=${messages[0].sequence}&limit=100`);
      if (!alive.current) return;
      bottom.current = false; setAtBottom(false); setOlder(page.length === 100);
      setMessages((existing) => mergeMessages(existing, page));
      requestAnimationFrame(() => { if (viewport.current) viewport.current.scrollTop = scroll + viewport.current.scrollHeight - height; });
    } catch (cause) { if (alive.current) setError(errorMessage(cause)); }
    finally { if (alive.current) setLoadingOlder(false); }
  };

  const visibleMessages = messages.filter(message => topic.kind !== "gamma" || !message.workflow_run_id
    || !workflowActivity.some(entry => entry.id === "workflow:" + message.workflow_run_id));
  const groups = topic.kind === "alpha" ? alphaMessageGroups(visibleMessages) : visibleMessages.map(message => [message]);
  const messageActivity: TopicActivity[] = groups.map((group, groupIndex) => ({
      id: "message:" + group[0].id, created_at: group[0].created_at, sequence: group[0].sequence, body: <>
      {topic.kind === "alpha" && (groupIndex === 0 || new Date(groups[groupIndex - 1][0].created_at * 1000).toDateString() !== new Date(group[0].created_at * 1000).toDateString())
        && <div className="org-message-day"><time dateTime={new Date(group[0].created_at * 1000).toISOString().slice(0, 10)}>{new Date(group[0].created_at * 1000).toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric", year: "numeric" })}</time></div>}
      <div className={topic.kind === "alpha" ? "org-message-group" : undefined}>{group.map((message, index) => <article className={"org-message " + (index ? "org-message-continuation " : "") + (message.author_kind !== "assistant" && message.author_id === user.id ? "org-message-own" : "")}
        key={message.id} id={"message-" + message.sequence} data-sequence={message.sequence}>
        <header>{!index && <strong>{authorLabel(message, members)}</strong>}
          <time dateTime={new Date(message.created_at * 1000).toISOString()} title={new Date(message.created_at * 1000).toLocaleString()}>{topic.kind === "alpha" ? new Date(message.created_at * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }) : new Date(message.created_at * 1000).toLocaleString()}</time></header>
        <ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[rehypeKatex]} skipHtml
          components={messageMarkdownComponents}>{normalizeMathDelimiters(mentionMarkdown(message.content, mentionPeople))}</ReactMarkdown>
        {message.author_kind === "assistant" && <MessageUsageDetails usage={message.usage} />}
        <MessageActions organizationId={organizationId} topicId={topic.id} messageId={message.id}
          people={mentionPeople}
          message={message} canEdit={canWrite && message.author_kind !== "assistant" && message.author_kind !== "bot" && message.author_id === user.id}
          onEdited={updated => setMessages(existing => mergeMessages(existing, [updated]))}
          onReplyAgain={topic.kind === "beta" && selectedModel && canWrite && online && !modelAttempt ? async () => {
            const metadata = modelCatalog?.models.find(model => model.id === selectedModel);
            const attempt = automaticModelAttempt(selectedModel, message.sequence, metadata?.max_completion_tokens ?? 8192);
            if (selectedEffort) attempt.effort = selectedEffort;
            onModelAttempt(attempt);
            try { await submitModelAttempt(organizationId, topic.id, attempt); onModelAttempt(undefined); }
            catch (cause) {
              if (cause instanceof ApiError && cause.status >= 400 && cause.status < 500 && cause.status !== 408) onModelAttempt(undefined);
              throw cause;
            }
          } : undefined}
          kind={topic.kind} canWrite={canWrite} onForked={(id) => {
            try { if (selectedModel) localStorage.setItem(`reasoning-model:${user.id}:${organizationId}:${id}`, selectedModel); }
            catch { /* Storage is optional. */ }
            onForked(id);
          }} onAuthLost={onAuthLost} />
      </article>)}</div></> }));
  return <MediaScope.Provider value={{ organizationId, topicId: topic.id }}><section className="org-conversation" aria-label="Topic conversation">
    {texOpen && <TexRequestDialog bots={botModels} names={botNames} onClose={() => setTexOpen(false)} onPrepare={(source, bot) => {
      const label = `@${botNames[bot.bot_id] ?? bot.name}`;
      updateDraft({ text: label + " " + texRequest(source), clientId: crypto.randomUUID(), attempted: false,
        mentions: [{ start: 0, end: label.length, id: bot.bot_id, label }] });
      setTexOpen(false); setMention(null); requestAnimationFrame(() => textarea.current?.focus());
    }} />}
    {topic.kind === "beta" && <ForkOrigin organizationId={organizationId} topicId={topic.id} />}
    <div className="org-stream">
    <div className="org-transcript" ref={viewport} tabIndex={0} aria-label="Message history" onScroll={() => {
      const view = viewport.current;
      if (view) { bottom.current = view.scrollHeight - view.scrollTop - view.clientHeight < 64; setAtBottom(bottom.current); }
    }}>
      {topic.kind === "beta" && <TopicSystemPrompt organizationId={organizationId} topicId={topic.id} onAuthLost={onAuthLost} revision={promptRevision}
        onInspect={() => { bottom.current = false; setAtBottom(false); }} />}
      {older && messages.length > 0 && <button className="org-older" disabled={loadingOlder} onClick={() => void loadOlder()}>{loadingOlder ? "Loading…" : "Load earlier messages"}</button>}
      {loading && <p role="status" className="org-empty">Loading conversation…</p>}
      {!loading && messages.length === 0 && topic.kind !== "gamma" && <div className="org-empty"><h2>Start the conversation</h2><p>{canWrite ? "Write the first message below." : "Messages will appear here."}</p></div>}
      {orderedActivity([...messageActivity, ...workflowActivity, ...computeActivity, ...betaActivity, ...botActivity])
        .map(item => <div key={item.id} data-activity-id={item.id}>{item.body}</div>)}
      {topic.kind === "beta" && <BetaConversation organizationId={organizationId} topicId={topic.id} userId={user.id}
        canWrite={canWrite} canManage={canManage} online={online} toolbarTarget={composerTools} hidePlay={Boolean(draft.text.trim())}
        attempt={modelAttempt} onAttempt={onModelAttempt} onAuthLost={onAuthLost} onActivity={setBetaActivity} />}
      <BotConversation organizationId={organizationId} topicId={topic.id} names={botNames} userId={user.id}
        canWrite={canWrite} canManage={canManage} onActivity={setBotActivity} onAuthLost={onAuthLost} />

    </div>
    {!atBottom && messages.length + workflowActivity.length + computeActivity.length + betaActivity.length > 0 && <button className="org-jump" aria-label="Jump to newest message" title="Newest message" onClick={() => {
      bottom.current = true; setAtBottom(true); if (viewport.current) viewport.current.scrollTop = viewport.current.scrollHeight;
    }}><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true"><path d="M12 4v16m-7-7 7 7 7-7" /></svg></button>}
    </div>
    {!online && <p role="status" className="org-reconnecting">Reconnecting…</p>}
    {error && <p role="alert" className="org-error org-conversation-error">{error}</p>}
    {topic.kind === "beta" && <ResearchControls organizationId={organizationId} topicId={topic.id} userId={user.id}
      toolbarTarget={composerTools} catalog={replyCatalog} allowedModels={replyConfiguration?.allowed_models ?? []}
      selectedEffort={selectedEffort}
      selectedModel={selectedModel} canWrite={canWrite} canManage={canManage} online={online} goalDraft={goalDraft}
      onGoalDraftDone={() => setGoalDraft(null)} onGoalStarted={(objective) => {
        if (goalCommand(currentDraft.current.text) === objective) updateDraft(newDraft()); onPreference();
      }} onSettingsChanged={() => setPromptRevision(value => value + 1)} onAuthLost={onAuthLost} />}
      {topic.kind === "gamma" && <WorkflowConversation organizationId={organizationId} topicId={topic.id} userId={user.id}
        canWrite={canWrite} canManage={canManage} attempt={workflowAttempt} onAttempt={onWorkflowAttempt}
        executionAttempt={workflowExecutionAttempt} onExecutionAttempt={onWorkflowExecutionAttempt} onAuthLost={onAuthLost} toolbarTarget={composerTools}
        hidePlay={Boolean(draft.text.trim())} onActivity={setWorkflowActivity} />}
      {topic.kind !== "alpha" && <ComputeConversation organizationId={organizationId} topicId={topic.id} userId={user.id}
        canWrite={canWrite} canManage={canManage} attempt={computeAttempt} onAttempt={onComputeAttempt} onAuthLost={onAuthLost} toolbarTarget={composerTools} onActivity={setComputeActivity} />}
    <form className="org-composer" onSubmit={(event) => void send(event)}>
      <SlashCommandPalette topicId={topic.id} kind={topic.kind} text={draft.text} onSelect={chooseCommand} />
      <label className="org-visually-hidden" htmlFor="org-message">Message</label>
      <textarea id="org-message" ref={textarea} placeholder={!canWrite ? "You have read-only access" : topic.kind === "beta" && modelAttempt
        ? "Resolve the pending model request before sending another message" : topic.kind === "beta" && selectedModel
      ? `Message ${replyCatalog?.models.find(model => model.id === selectedModel)?.execution_source === "bot"
        ? replyCatalog.models.find(model => model.id === selectedModel)?.name : selectedModel}…` : "Write a message…"} value={draft.text}
        disabled={!canWrite || sending || (topic.kind === "beta" && !!modelAttempt)} maxLength={16000} rows={1} onChange={(event) => {
          updateDraft({ text: event.target.value, clientId: draft.attempted || !draft.clientId ? crypto.randomUUID() : draft.clientId,
            attempted: false, mentions: editMentions(draft.text, event.target.value, draft.mentions) });
          setMention(mentionQuery(event.target.value, event.target.selectionStart, topicBots.map(bot => bot.name))); setMentionIndex(0);
        }} onSelect={(event) => setMention(mentionQuery(event.currentTarget.value, event.currentTarget.selectionStart, topicBots.map(bot => bot.name)))}
        aria-autocomplete="list" aria-controls={mentionMembers.length ? "org-mention-list" : undefined}
        aria-activedescendant={mentionMembers.length ? "org-mention-" + (mentionMembers[mentionIndex % mentionMembers.length].optionId ?? mentionMembers[mentionIndex % mentionMembers.length].id) : undefined}
        onKeyDown={(event) => {
          if (event.nativeEvent.isComposing) return;
          if (event.key === "Escape") { setMention(null); return; }
          if (event.key === "Tab" && mention && !event.shiftKey && !event.ctrlKey && !event.metaKey && !event.altKey) {
            event.preventDefault();
            if (mentionMembers.length) chooseMention(mentionMembers[mentionIndex % mentionMembers.length]);
            return;
          }
          if (event.key === "Tab") { setMention(null); return; }
          if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); setMention(null); void send(); return; }
          if (event.key === "Enter" && event.shiftKey) return;
          if (mentionMembers.length && ["ArrowDown", "ArrowUp", "Enter"].includes(event.key)) {
            event.preventDefault();
            if (event.key === "Enter") chooseMention(mentionMembers[mentionIndex % mentionMembers.length]);
            else setMentionIndex((index) => (index + (event.key === "ArrowDown" ? 1 : -1) + mentionMembers.length) % mentionMembers.length);
            return;
          }
          if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); setMention(null); void send(); }
        }} />
      <div className="org-composer-controls">
        <div className="org-composer-actions">
        {(topic.kind === "alpha" || draft.text.trim()) && <button className="org-primary org-send-play"
          aria-label={sending ? "Sending…" : draft.attempted ? "Retry send" : "Send"} title={draft.attempted ? "Retry send" : "Send message"}
          disabled={!canWrite || sending || !online || !draft.text.trim() || (topic.kind === "beta" && !!modelAttempt)}>
          <span aria-hidden="true">{sending ? "…" : "▶"}</span></button>}
        {topic.kind !== "alpha" && <div className="org-composer-tools" ref={setComposerTools} />}
        <span ref={attachmentControl}><AttachmentButton organizationId={organizationId} topicId={topic.id}
          disabled={!canWrite || sending || (topic.kind === "beta" && !!modelAttempt)} onError={cause => {
            if (cause instanceof ApiError && cause.status === 401) onAuthLost();
          }} onInsert={markdown => {
            const previous = currentDraft.current, text = previous.text + (previous.text ? "\n\n" : "") + markdown;
            updateDraft({ text, clientId: crypto.randomUUID(), attempted: false, mentions: previous.mentions });
            requestAnimationFrame(() => textarea.current?.focus());
          }} /></span>
        </div>
        <div className="org-composer-model">
        {topic.kind === "beta" && <BetaModelMenu configuration={replyConfiguration} catalog={replyCatalog} selected={selectedModel} tools={topic.knowledge_tools}
          disabled={sending || !!modelAttempt} error={selectedModel.startsWith("bot/") ? "" : modelError} onSelect={setSelectedModel}
          effort={effort} onEffortChange={chooseEffort} />}
        </div>
      </div>
      {mentionMembers.length > 0 && <div id="org-mention-list" role="listbox" className="org-mention-menu" aria-label="Mention a member">
        {mentionMembers.map((member, index) => <button type="button" role="option" tabIndex={-1} id={"org-mention-" + (member.optionId ?? member.id)}
          aria-selected={index === mentionIndex % mentionMembers.length} key={member.optionId ?? member.id}
          onMouseDown={(event) => event.preventDefault()} onClick={() => chooseMention(member)}>{member.name}{member.model ? "-" + member.model : ""}{member.effort ? "!" + member.effort : ""}{member.owner ? " · " + member.owner : ""}</button>)}</div>}
    </form>
  </section></MediaScope.Provider>;
}

function Administration({ organization, onClose, onRefresh, onAuthLost }: { organization: Organization; onClose: () => void; onRefresh: () => void; onAuthLost: () => void }) {
  const [tab, setTab] = useState<"members" | "invitations" | "ai" | "bots" | "compute">("members");
  const [members, setMembers] = useState<Member[]>([]);
  const [invitations, setInvitations] = useState<Invitation[]>([]);
  const [mail, setMail] = useState<InvitationMail>({ enabled: false, daily_limit: null, worker_running: false });
  const [sendEmail, setSendEmail] = useState(false);
  const inviteAttempt = useRef<{ email: string; role: string; client_id: string } | null>(null);
  const resendAttempts = useRef<Record<string, string>>({});
  const [keys, setKeys] = useState<KeyMetadata[]>([]);
  const [budget, setBudget] = useState("0");
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("member");
  const [keyValue, setKeyValue] = useState("");
  const [keyLabel, setKeyLabel] = useState("");
  const [keyFormOpen, setKeyFormOpen] = useState(false);
  const [link, setLink] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const root = organizationPath(organization.id);
  const reload = useCallback(async () => {
    const [people, invites, credentials, policy, mailConfiguration] = await Promise.all([
      request<Member[]>(root + "/members"), request<Invitation[]>(root + "/invitations"), request<KeyMetadata[]>(root + "/provider-keys"), request<Policy>(root + "/policy"),
      request<InvitationMail>(root + "/invitation-mail"),
    ]);
    setMembers(people); setInvitations(invites); setKeys(credentials);
    setMail(mailConfiguration);
    setBudget((policy.monthly_budget_microusd / 1_000_000).toFixed(6).replace(/\.?0+$/, ""));
  }, [root]);
  useEffect(() => { void reload().catch((cause) => setError(errorMessage(cause))); }, [reload]);
  useEffect(() => {
    if (tab !== "invitations") return;
    const controller = new AbortController();
    const timer = setInterval(() => {
      void request<Invitation[]>(root + "/invitations", { signal: controller.signal }).then(setInvitations).catch((cause) => {
        if (aborted(cause)) return;
        if (cause instanceof ApiError && cause.status === 401) onAuthLost();
        else setError(errorMessage(cause));
      });
    }, 3000);
    return () => { controller.abort(); clearInterval(timer); };
  }, [tab, root, onAuthLost]);
  const action = async (work: () => Promise<unknown>) => {
    setBusy(true); setError("");
    try { await work(); await reload(); onRefresh(); } catch (cause) { setError(errorMessage(cause)); }
    finally { setBusy(false); }
  };
  return <Dialog title={organization.name + " administration"} onClose={onClose}>
    <nav className="org-admin-tabs" aria-label="Administration">{(["members", "invitations", "ai", "bots", "compute"] as const).map((value) =>
      <button key={value} aria-pressed={tab === value} onClick={() => { setTab(value); setError(""); }}>{value === "ai" ? "Inference" : value[0].toUpperCase() + value.slice(1)}</button>)}</nav>
    {error && <p role="alert" className="org-error">{error}</p>}
    {tab === "bots" && <BotAdministration key={organization.id} organizationId={organization.id} />}
    {tab === "compute" && <ComputeAdministration key={organization.id} organizationId={organization.id} onAuthLost={onAuthLost} />}
    {tab === "members" && <ul className="org-admin-list">{members.map((member) => <li key={member.id}><div><strong>{member.name}</strong><small>{member.email}</small></div>
      {organization.role === "owner" || !["owner", "admin"].includes(member.role) ? <>
        <select aria-label={"Role for " + member.name} value={member.role} disabled={busy} onChange={(event) => void action(() => request(root + "/members/" + member.id, { method: "PATCH", body: { role: event.target.value } }))}>
          {(organization.role === "owner" ? ["owner", "admin", "member", "viewer"] : ["member", "viewer"]).map((value) => <option key={value}>{value}</option>)}
        </select><button disabled={busy} onClick={() => { if (window.confirm(`Remove ${member.name} from ${organization.name}? Their account and message history remain.`)) void action(() => request(root + "/members/" + member.id, { method: "DELETE" })); }}>Remove</button>
      </> : <span>{member.role}</span>}
    </li>)}</ul>}
    {tab === "invitations" && <><BatchInvitations organizationId={organization.id} owner={organization.role === "owner"}
      mailEnabled={mail.enabled} workerRunning={mail.worker_running} invitations={invitations}
      onChanged={() => { void reload(); onRefresh(); }} /><form onSubmit={(event) => { event.preventDefault(); void action(async () => {
      const useMail = sendEmail && mail.enabled;
      if (useMail && (!inviteAttempt.current || inviteAttempt.current.email !== email || inviteAttempt.current.role !== role)) {
        inviteAttempt.current = { email, role, client_id: crypto.randomUUID() };
      }
      const body = useMail ? { ...inviteAttempt.current!, send_email: true } : { email, role };
      const invite = await request<Invitation & { token: string }>(root + "/invitations", { method: "POST", body });
      setLink(window.location.origin + "/#invite=" + encodeURIComponent(invite.token)); setEmail("");
      inviteAttempt.current = null;
    }); }}><label>Invite email<input type="email" required value={email} onChange={(event) => setEmail(event.target.value)} /></label>
      <label>Invitation role<select value={role} onChange={(event) => setRole(event.target.value)}><option>member</option><option>viewer</option>{organization.role === "owner" && <option>admin</option>}</select></label>
      {mail.enabled ? <><label className="org-invitation-email-toggle"><input type="checkbox" checked={sendEmail} onChange={(event) => setSendEmail(event.target.checked)} />Send invitation by email</label>
        <small>Installation limit: {mail.daily_limit} email invitations per rolling day.{!mail.worker_running && " Email worker is not running; invitations will remain queued."}</small></>
        : <p className="org-help">Email is not configured. Create a link to share directly.</p>}
      <button className="org-primary" disabled={busy}>Create invitation</button></form>
      {link && <label className="org-invite-link">Invitation link<input value={link} readOnly onFocus={(event) => event.target.select()} />
        <button type="button" onClick={() => void navigator.clipboard.writeText(link).catch(() => setError("Could not copy; select the link and copy manually."))}>Copy link</button>
        <small>Share privately. It expires after seven days. A shared invitation does not verify inbox ownership.</small></label>}
      <InvitationDeliveryProgress invitations={invitations} />
      <ul className="org-admin-list">{invitations.map((invite) => <li key={invite.id}><div><strong>{invite.email}</strong><small>{invite.role} · {invite.accepted_at ? "Joined organization" : invite.revoked_at ? "Revoked" : invite.expires_at * 1000 <= Date.now() ? "Expired" : "Pending"}</small></div>
        <small>{invitationEmailLabel(invite)}</small>
        {!invite.accepted_at && !invite.revoked_at && mail.enabled && <button disabled={busy} onClick={() => void action(async () => {
          const client_id = resendAttempts.current[invite.id] ??= crypto.randomUUID();
          const next = await request<Invitation & { token: string }>(root + "/invitations/" + invite.id + "/resend", {
            method: "POST", body: { client_id },
          }); delete resendAttempts.current[invite.id]; setLink(window.location.origin + "/#invite=" + encodeURIComponent(next.token));
        })}>New link and email</button>}
        {!invite.accepted_at && !invite.revoked_at && <button disabled={busy} onClick={() => { if (window.confirm("Revoke this invitation? The link will no longer work.")) void action(() => request(root + "/invitations/" + invite.id, { method: "DELETE" })); }}>Revoke</button>}
      </li>)}</ul></>}
    {tab === "ai" && <div className="org-admin-ai"><section><header className="org-admin-section-heading"><div><h3>OpenRouter keys</h3>
      <p>Multiple keys can be stored. New requests use the newest active key; secret values are never returned.</p></div>
      {!keyFormOpen && <button disabled={busy} onClick={() => { setKeyLabel(""); setKeyValue(""); setKeyFormOpen(true); }}>+ Add key</button>}</header>
      {keyFormOpen && <form className="org-key-form" autoComplete="off" onSubmit={(event) => { event.preventDefault(); void action(async () => {
        await request(root + "/provider-keys", { method: "POST", body: {
          label: keyLabel, key: keyValue,
        } }); setKeyValue(""); setKeyLabel(""); setKeyFormOpen(false);
      }); }}><header><div><h4>Add an OpenRouter key</h4><small>This creates a new stored key; it does not replace one listed below.</small></div>
        <button type="button" disabled={busy} onClick={() => { setKeyLabel(""); setKeyValue(""); setKeyFormOpen(false); }}>Cancel</button></header>
        <label>Key label<input required disabled={busy} name="reasoning-key-label" placeholder="For example, Primary" maxLength={200} autoComplete="off"
          value={keyLabel} onChange={(event) => setKeyLabel(event.target.value)} /></label>
        <label>OpenRouter API key<input type="password" required disabled={busy} name="reasoning-new-openrouter-key" placeholder="Paste a new OpenRouter key" minLength={8} maxLength={4096}
          autoComplete="new-password" autoCapitalize="none" spellCheck={false} value={keyValue} onChange={(event) => setKeyValue(event.target.value)} /></label>
        <p className="org-help">Keys can use any available OpenRouter model or router. Upstream key restrictions, routing policy, and organization spending limits still apply.</p>
        <p className="org-help">Enabled by default. Adding the key replaces the model allowlist with the current text-to-text catalog; you can narrow it below.</p>
        <button className="org-primary" disabled={busy}>{busy ? "Adding…" : "Add OpenRouter key"}</button></form>}
      {!keys.length && <p className="org-help">No OpenRouter keys are stored.</p>}
      <ul className="org-admin-list">{keys.map((key) => <li key={key.id}><div><strong>{key.label}</strong><small>{key.provider} · {key.fingerprint} · {key.disabled_at ? "Disabled" : "Active"}</small></div>
        {!key.disabled_at && <button disabled={busy} onClick={() => { if (window.confirm("Disable this stored key? Add a replacement first if needed. This does not revoke the upstream OpenRouter credential.")) void action(() => request(root + "/provider-keys/" + key.id, { method: "DELETE" })); }}>Disable</button>}
      </li>)}</ul><ProviderKeyUsage key={keys.map(key => key.id).join(",")} organizationId={organization.id} /></section><section><h3>Spending limit</h3><form onSubmit={(event) => { event.preventDefault(); void action(() => request(root + "/policy", { method: "PUT", body: {
      monthly_budget_microusd: decimalBudget(budget),
    } })); }}><p className="org-help">Beta conversations reserve this budget before execution. Known charges and unresolved reservations reduce the available balance. Set upstream OpenRouter key limits too; local reservations do not guarantee the provider bill.</p>
      <label>Monthly budget (USD)<input required inputMode="decimal" value={budget} onChange={(event) => setBudget(event.target.value)} /></label>
      <button className="org-primary" disabled={busy}>Save budget</button>
    </form></section></div>}
  </Dialog>;
}

export function OrganizationApp() {
  const [pairCode, setPairCode] = useState(() => typeof window === "undefined" ? undefined : new URLSearchParams(window.location.search).get("bot_connect") ?? undefined);
  const [user, setUser] = useState<Account | null>(null);
  const [loading, setLoading] = useState(true);
  const [initialized, setInitialized] = useState(true);
  const [invitation, setInvitation] = useState(() => typeof window === "undefined" ? "" : new URLSearchParams(window.location.hash.slice(1)).get("invite") ?? "");
  const [verificationToken, setVerificationToken] = useState(() => typeof window === "undefined" ? "" : new URLSearchParams(window.location.hash.slice(1)).get("verify") ?? "");
  const [mode, setMode] = useState<"login" | "join">(invitation ? "join" : "login");
  const [organizations, setOrganizations] = useState<Organization[]>([]);
  const [workspace, setWorkspace] = useState(() => {
    const query = new URLSearchParams(typeof window === "undefined" ? "" : window.location.search);
    return { organization: query.get("organization"), view: query.get("view"), topic: query.get("topic") ?? undefined, project: query.get("project") ?? undefined };
  });
  const [organizationId, setOrganizationId] = useState<string | null>(workspace.organization);
  const [hierarchy, setHierarchy] = useState<Hierarchy>(EMPTY_HIERARCHY);
  const [navigationUsage, setNavigationUsage] = useState<NavigationUsage>();
  const [members, setMembers] = useState<Member[]>([]);
  const [selection, setSelection] = useState<{ topic: string; anchor: number | null } | null>(null);
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const [modelAttempts, setModelAttempts] = useState<Record<string, ModelAttempt | undefined>>({});
  const [computeAttempts, setComputeAttempts] = useState<Record<string, ComputeAttempt | undefined>>({});
  const [workflowAttempts, setWorkflowAttempts] = useState<Record<string, WorkflowAttempt | undefined>>({});
  const [workflowExecutionAttempts, setWorkflowExecutionAttempts] = useState<Record<string, WorkflowExecutionAttempt | undefined>>({});
  const [creation, setCreation] = useState<Creation | null>(null);
  const [creationParent, setCreationParent] = useState<string | undefined>();
  const [entitySettings, setEntitySettings] = useState<{ kind: "project" | "channel" | "topic"; id: string } | null>(null);
  const [admin, setAdmin] = useState(false);
  const [information, setInformation] = useState<"user" | "organization" | null>(null);
  const [tool, setTool] = useState<"knowledge" | "prices" | "usage" | "bots" | null>(pairCode ? "bots" : null);
  const [knowledgeView, setKnowledgeView] = useState<KnowledgeView | null>(["ledger", "bibliography", "journal"].includes(workspace.view ?? "") ? workspace.view as KnowledgeView : null);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(() => new URLSearchParams(window.location.search).get("view") === "help");
  useEffect(() => {
    const changed = () => { const query = new URLSearchParams(window.location.search), view = query.get("view");
      setWorkspace({organization:query.get("organization"),view,topic:query.get("topic") ?? undefined,project:query.get("project") ?? undefined});
      setKnowledgeView(["ledger", "bibliography", "journal"].includes(view ?? "") ? view as KnowledgeView : null);
      if(query.get("organization")) setOrganizationId(query.get("organization"));
      if(query.get("topic")) setSelection({topic:query.get("topic")!,anchor:null});
    };
    window.addEventListener("popstate", changed); return () => window.removeEventListener("popstate", changed);
  }, []);
  const [navOpen, setNavOpen] = useState(() => typeof window !== "undefined" && window.innerWidth > 700);
  const [toolbarOpen, setToolbarOpen] = useState(() => typeof window === "undefined" || window.innerWidth > 600);
  useEffect(() => {
    const phone = window.matchMedia("(max-width: 600px)");
    const update = () => setToolbarOpen(!phone.matches);
    update(); phone.addEventListener("change", update);
    return () => phone.removeEventListener("change", update);
  }, []);
  useEffect(() => {
    let narrow = window.innerWidth <= 700;
    const resize = () => {
      const next = window.innerWidth <= 700;
      if (next !== narrow) { narrow = next; setNavOpen(!next); }
    };
    window.addEventListener("resize", resize);
    return () => window.removeEventListener("resize", resize);
  }, []);
  const [presenceOpen, setPresenceOpen] = useState(() => typeof window !== "undefined" && window.innerWidth > 1100);
  useEffect(() => {
    const narrow = window.matchMedia("(max-width: 1100px)");
    const closeOnNarrowScreen = () => { if (narrow.matches) setPresenceOpen(false); };
    narrow.addEventListener("change", closeOnNarrowScreen);
    return () => narrow.removeEventListener("change", closeOnNarrowScreen);
  }, []);
  const [error, setError] = useState("");
  const [search, setSearch] = useState("");
  const [results, setResults] = useState<SearchResult[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [online, setOnline] = useState(true);
  const [refresh, setRefresh] = useState(0);
  const searchInput = useRef<HTMLInputElement>(null);
  const organization = organizations.find((item) => item.id === organizationId);
  const canCreate = Boolean(organization && organization.role !== "viewer");
  const canCreateOrganization = organizations.some((item) => item.role === "owner");
  const topic = hierarchy.topics.find((item) => item.id === selection?.topic);
  const channel = hierarchy.channels.find((item) => item.id === topic?.channel_id);
  const selectedProject = hierarchy.projects.find((item) => item.id === channel?.project_id);
  const canWriteSelected = Boolean(organization && (organization.role !== "viewer" || selectedProject?.project_role === "owner"));
  const draftKey = user && organization && topic ? user.id + "/" + organization.id + "/" + topic.id : "";
  const draft = drafts[draftKey] ?? { text: "", clientId: "", attempted: false };
  const preferenceRefresh = useCallback(() => setRefresh((value) => value + 1), []);
  const beginCreation = (kind: Creation, parentId?: string) => { setCreationParent(parentId); setCreation(kind); };
  const authLost = useCallback(() => {
    setUser(null); setOrganizations([]); setOrganizationId(null); setHierarchy(EMPTY_HIERARCHY); setMembers([]);
    setSelection(null); setDrafts({}); setModelAttempts({}); setComputeAttempts({}); setWorkflowAttempts({}); setWorkflowExecutionAttempts({}); setAdmin(false); setCreation(null); setCreationParent(undefined); setResults(null); setSearch("");
    setInformation(null); setTool(null); setEntitySettings(null);
    setNavigationUsage(undefined);
  }, []);

  useEffect(() => {
    if (![...Object.values(computeAttempts), ...Object.values(workflowAttempts), ...Object.values(workflowExecutionAttempts)].some(Boolean)) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [computeAttempts, workflowAttempts, workflowExecutionAttempts]);

  useEffect(() => {
    if (invitation || verificationToken) window.history.replaceState(null, "", window.location.pathname + window.location.search);
    const controller = new AbortController();
    const failure = (cause: unknown) => {
      if (!controller.signal.aborted && !aborted(cause) && !(cause instanceof ApiError && cause.status === 401)) setError(errorMessage(cause));
    };
    void Promise.all([
      request<{ initialized: boolean }>("/health", { signal: controller.signal })
        .then(health => { if (!controller.signal.aborted) setInitialized(health.initialized); }).catch(failure),
      request<Account>("/auth/me", { signal: controller.signal })
        .then(account => { if (!controller.signal.aborted) { setUser(account); setLoading(false); } }).catch(failure),
    ]).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (!user) return;
    const controller = new AbortController();
    void request<Organization[]>("/organizations", { signal: controller.signal }).then((items) => {
      setOrganizations(items); setOrganizationId((current) => workspace.organization ?? (items.some((item) => item.id === current) ? current : items[0]?.id ?? null));
    }).catch((cause) => { if (!aborted(cause)) setError(errorMessage(cause)); });
    return () => controller.abort();
  }, [user?.id]);

  useEffect(() => {
    if (!user || !organizationId) return;
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const [, people, orgs] = await Promise.all([
          request<Hierarchy>(organizationPath(organizationId) + "/hierarchy", { signal: controller.signal }).then(tree => {
            if (controller.signal.aborted) return tree;
            setHierarchy(tree);
            setSelection((current) => {
              if (current && tree.topics.some(item => item.id === current.topic)) return current;
              const saved = workspace.topic ?? readNavigation(user.id, organizationId).topic;
              const initial = tree.topics.find(item => item.id === saved) ?? tree.topics[0];
              return initial ? { topic: initial.id, anchor: null } : null;
            });
            return tree;
          }),
          request<Member[]>(organizationPath(organizationId) + "/members", { signal: controller.signal }),
          request<Organization[]>("/organizations", { signal: controller.signal }),
        ]);
        if (controller.signal.aborted) return;
        setMembers(people); setOrganizations(orgs); setOnline(true);
      } catch (cause) {
        if (aborted(cause)) return;
        if (cause instanceof ApiError && cause.status === 401) { authLost(); return; }
        if (cause instanceof ApiError && cause.status === 404) {
          setHierarchy(EMPTY_HIERARCHY); setMembers([]); setSelection(null); setOrganizationId(null); setOrganizations([]); setAdmin(false);
        }
        setOnline(false); setError(errorMessage(cause));
      }
      if (!controller.signal.aborted) timer = schedulePoll(poll, 4000, controller.signal);
    };
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [user?.id, organizationId, refresh, authLost]);

  useEffect(() => {
    if (!user || !organizationId) return;
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const spend = await request<NavigationUsage>(organizationPath(organizationId) + "/navigation-usage", { signal: controller.signal });
        if (!controller.signal.aborted) setNavigationUsage(spend);
      } catch (cause) {
        if (aborted(cause)) return;
        if (cause instanceof ApiError && cause.status === 401) { authLost(); return; }
        setError(errorMessage(cause));
      }
      if (!controller.signal.aborted) timer = schedulePoll(poll, 15000, controller.signal);
    };
    void poll(); return () => { controller.abort(); clearTimeout(timer); };
  }, [user?.id, organizationId, refresh, authLost]);

  useEffect(() => {
    if (user && organizationId && selection && hierarchy.topics.some(item => item.id === selection.topic)) {
      saveNavigation(user.id, organizationId, { topic: selection.topic });
    }
  }, [user?.id, organizationId, selection?.topic, hierarchy.topics]);

  useEffect(() => {
    if (!draftKey || drafts[draftKey]) return;
    setDrafts((existing) => initializeDraft(existing, draftKey));
  }, [draftKey]);

  useEffect(() => {
    if (!search.trim() || !organizationId) { setResults(null); setSearching(false); return; }
    const controller = new AbortController(); setSearching(true);
    const timer = setTimeout(() => {
      void request<SearchResult[]>(organizationPath(organizationId) + "/search?q=" + encodeURIComponent(search), { signal: controller.signal })
        .then(setResults).catch((cause) => { if (!aborted(cause)) setError(errorMessage(cause)); })
        .finally(() => { if (!controller.signal.aborted) setSearching(false); });
    }, 250);
    return () => { controller.abort(); clearTimeout(timer); };
  }, [search, organizationId]);

  useEffect(() => {
    const keyboard = (event: KeyboardEvent) => {
      const input = event.target instanceof HTMLElement && (event.target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(event.target.tagName));
      if (event.isComposing || event.defaultPrevented) return;
      if (event.key === "?" && !input && !event.ctrlKey && !event.metaKey && !event.altKey) {
        event.preventDefault();
        if (helpOpen) setHelpOpen(false); else setShortcutsOpen(open => !open);
        return;
      }
      if (event.target instanceof HTMLElement && event.target.closest("dialog")) return;
      if ((event.ctrlKey || event.metaKey) && event.shiftKey && !event.altKey && event.key.toLowerCase() === "p") {
        event.preventDefault(); setPresenceOpen(open => !open); return;
      }
      if ((event.ctrlKey || event.metaKey) && !event.altKey && !event.shiftKey && event.key.toLowerCase() === "b") {
        event.preventDefault(); setNavOpen(open => !open); return;
      }
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault(); const menu = searchInput.current?.closest("details"); if (menu) menu.open = true; searchInput.current?.focus();
      }
      if (creation || admin) return;
      if (event.key === "Escape") { setSearch(""); setResults(null); }
      if (event.altKey && ["ArrowUp", "ArrowDown"].includes(event.key) && hierarchy.topics.length) {
        event.preventDefault();
        const index = hierarchy.topics.findIndex((item) => item.id === selection?.topic);
        const next = (index + (event.key === "ArrowDown" ? 1 : -1) + hierarchy.topics.length) % hierarchy.topics.length;
        setSelection({ topic: hierarchy.topics[next].id, anchor: null }); setSearch("");
      }
    };
    window.addEventListener("keydown", keyboard); return () => window.removeEventListener("keydown", keyboard);
  }, [hierarchy.topics, selection, creation, admin, helpOpen]);

  const authenticated = async (session: Session) => {
    setUser(await request<Account>("/auth/me")); setInitialized(true); setError("");
    if (session.organization_id) { setOrganizationId(workspace.organization ?? session.organization_id); setInvitation(""); }
  };
  if (loading) return <main className="org-auth"><p role="status">Opening Reasoning…</p></main>;
  if (!user) return <><AuthForm key={initialized ? mode : "setup"} mode={!initialized ? "setup" : mode} invitation={invitation} onMode={setMode} onAuthenticated={authenticated} />
    {error && <div className="org-startup-error" role="alert">{error} <button onClick={() => window.location.reload()}>Reconnect</button></div>}</>;

  const select = (id: string, anchor: number | null = null) => {
    setHelpOpen(false);
    if (knowledgeView) { setKnowledgeView(null); const url = new URL(window.location.href); url.searchParams.delete("view"); url.searchParams.set("topic", id); window.history.pushState(null, "", url); }
    setWorkspace(current=>({...current,topic:id,view:null,project:undefined}));
    setSelection({ topic: id, anchor });
    if (window.innerWidth <= 700) setNavOpen(false);
    setSearch(""); setResults(null);
  };
  const switchOrganization = (id: string) => {
    setHelpOpen(false);
    setOrganizationId(id || null); setHierarchy(EMPTY_HIERARCHY); setMembers([]); setSelection(null); setSearch(""); setResults(null); setError(""); setAdmin(false); setCreation(null); setCreationParent(undefined);
    setInformation(null); setTool(pairCode ? "bots" : null); setEntitySettings(null);
    setNavigationUsage(undefined);
    setKnowledgeView(null);
    setWorkspace({organization:id || null, view:null,topic:undefined,project:undefined});
    window.history.replaceState(null,"",id ? "/organization.html?organization="+encodeURIComponent(id) : "/organization.html");
  };
  const openKnowledge = (view: KnowledgeView) => { setHelpOpen(false); setKnowledgeView(view); setWorkspace(current=>({...current,view,topic:topic?.id,project:undefined})); setSearch(""); setResults(null);
    const query = new URLSearchParams({ organization: organizationId!, view }); if (topic) query.set("topic", topic.id);
    window.history.pushState(null, "", "/organization.html?" + query); };
  return <div className={"org-app org-tree-tone-" + Math.max(0, hierarchy.topics.filter(value => value.channel_id === topic?.channel_id).findIndex(value => value.id === topic?.id)) % 4}>
    <header className="org-appbar org-topic-color"><button type="button" className="org-icon-button org-nav-toggle" aria-label="Toggle project navigation"
      title="Toggle project navigation · Ctrl/⌘B" aria-controls="org-project-navigation" aria-expanded={navOpen} onClick={() => setNavOpen(open => !open)}>
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M9 4v16" /></svg></button>
      <div className="org-wordmark" title={organization?.name}><img src="/reasoning.svg" alt="" /><span>{organization?.name ?? "Organizations"}</span></div>
      {knowledgeView ? <h1 className="org-workspace-title">{knowledgeView === "journal" ? "Research blog editor" : knowledgeView === "ledger" ? "Ledger" : "Bibliography"}</h1> : organization && topic && <TopicBreadcrumb channel={channel} topic={topic}
        subscribed={hierarchy.topic_state.find((item) => item.topic_id === topic.id)?.subscribed !== false}
        onFollow={() => void request(topicPath(organization.id, topic.id) + "/subscription", { method: "PUT", body: {
          subscribed: hierarchy.topic_state.find((item) => item.topic_id === topic.id)?.subscribed === false,
        } }).then(preferenceRefresh).catch((cause) => {
          if (cause instanceof ApiError && cause.status === 401) authLost(); else setError(errorMessage(cause));
        })}
        onSettings={organization.role === "owner" || organization.role === "admin" || selectedProject?.project_role === "owner"
          ? () => setEntitySettings({ kind: "topic", id: topic.id }) : undefined} />}
      <details className="org-toolbar-actions" open={toolbarOpen} onToggle={event => {
        if (event.target === event.currentTarget) setToolbarOpen(window.innerWidth > 600 || event.currentTarget.open);
      }}><summary aria-label="More topic actions" title="More topic actions">⋯</summary><div className="org-toolbar-content">
      <label className="org-search"><span className="org-visually-hidden">Search organization messages</span><input ref={searchInput} type="search" placeholder="Search messages · ⌘K" value={search} maxLength={200} disabled={!organization}
        onChange={(event) => setSearch(event.target.value)} /></label>
      <div className="org-toolbar-icons"><ThemeToggle />{organization && <button className="org-icon-button org-presence-toggle" aria-label="Show online people and bots" title="People" aria-expanded={presenceOpen}
        onClick={() => setPresenceOpen((value) => !value)}><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true"><circle cx="9" cy="7" r="3"/><path d="M3 21v-3a6 6 0 0 1 12 0v3M17 4a3 3 0 0 1 0 6M21 21v-3a6 6 0 0 0-3-5"/></svg></button>}
      {organization && topic && <TopicCopyButton key={organization.id + "/" + topic.id} organizationId={organization.id} topicId={topic.id} onAuthLost={authLost} />}
      {organization && <KnowledgeLinks organizationId={organization.id} topicId={topic?.id} onOpen={openKnowledge} />}
      <a className="org-icon-button" href="/organization.html?view=help" aria-label="Help" title="Help" onClick={event => {
        if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
        event.preventDefault(); setHelpOpen(value => !value);
      }}>?</a></div>
      </div></details>
    </header>
    {error && <div className="org-banner" role="alert">{error}<button aria-label="Dismiss error" onClick={() => setError("")}>×</button></div>}
    {verificationToken && <VerifyEmailBanner token={verificationToken} onDone={() => {
      setVerificationToken(""); window.history.replaceState(null, "", window.location.pathname + window.location.search);
      void request<Account>("/auth/me").then(setUser).catch(cause => setError(errorMessage(cause)));
    }} />}
    {invitation && <div className="org-banner">You have an invitation. <button onClick={() => void request<Session>("/invitations/accept", { method: "POST", body: { token: invitation } })
      .then(async (session) => { await authenticated(session); setRefresh((value) => value + 1); setUser(await request<Account>("/auth/me")); })
      .catch((cause) => setError(errorMessage(cause)))}>Accept invitation</button><button onClick={() => setInvitation("")}>Dismiss</button></div>}
    <div className="org-body">
      {navOpen && <button className="org-nav-backdrop" aria-label="Close project navigation" onClick={() => setNavOpen(false)} />}
      <aside id="org-project-navigation" hidden={!navOpen} className={"org-sidebar " + (navOpen ? "org-sidebar-open" : "")}>
        <header><strong>Projects</strong><span className="org-sidebar-heading-actions"><span role="status" className={online ? "org-connected" : "org-offline"}>{online ? "" : "Offline"}</span>
          {canCreate && <button type="button" className="org-tree-add" aria-label="New project" title="New project" onClick={() => beginCreation("project")}>+</button>}</span></header>
        <TopicNavigation key={user.id + "/" + organizationId} hierarchy={hierarchy} organizationId={organizationId ?? undefined} userId={user.id} usage={navigationUsage} selected={selection?.topic ?? null} canCreate={canCreate}
          canManageOrganization={Boolean(organization && ["owner", "admin"].includes(organization.role))} onSelect={(id) => select(id)}
          onCreateChannel={(projectId) => beginCreation("channel", projectId)} onCreateTopic={(channelId) => beginCreation("topic", channelId)}
          onProjectSettings={(id) => setEntitySettings({ kind: "project", id })} onChannelSettings={(id) => setEntitySettings({ kind: "channel", id })}
          onTopicSettings={(id) => setEntitySettings({ kind: "topic", id })} />
        {!hierarchy.projects.length && organization && <p className="org-tree-empty">{organization.role === "viewer" ? "Projects will appear here." : "Create a project to begin."}</p>}
        <footer><AccountMenu user={user} organization={organization} organizations={organizations} canCreateOrganization={canCreateOrganization}
          onSwitch={switchOrganization} onAccount={() => setInformation("user")} onOrganization={() => setInformation("organization")}
          onAdmin={() => setAdmin(true)} onCreate={() => beginCreation("organization")} onKnowledge={() => setTool("knowledge")} onBots={() => setTool("bots")}
          onModels={() => setTool("prices")} onUsage={() => setTool("usage")}
          onShortcuts={() => setShortcutsOpen(true)}
          onSignOut={() => void request("/auth/logout", { method: "POST", body: {} }).then(authLost).catch((cause) => setError(errorMessage(cause)))} /></footer>
      </aside>
      <main className="org-main">
        {helpOpen ? <HelpPage onClose={() => setHelpOpen(false)} /> : knowledgeView && organization ? <MediaScope.Provider value={{ organizationId: organization.id }}><KnowledgeWorkspace key={organization.id + knowledgeView} organization={organization} hierarchy={hierarchy} view={knowledgeView}
          embedded initialTopicId={workspace.topic ?? topic?.id} initialProjectId={workspace.project} onAuthLost={authLost} /></MediaScope.Provider> : searching || results !== null ? <section className="org-search-results"><header><h1>Search messages</h1><button onClick={() => setSearch("")}>Close search</button></header>
          {searching ? <p role="status">Searching…</p> : !results?.length ? <p>No matching messages.</p> : <ol>{results.map((result) => <li key={result.id}><button onClick={() => select(result.topic_id, result.sequence)}>
            <small>{result.project_name} / #{result.channel_name} / {KIND_SYMBOL[result.topic_kind]}-{result.topic_name}</small>
            <p>{mentionText(result.content, members)}</p></button></li>)}</ol>}
        </section> : organization && topic ? <Conversation key={organization.id + "/" + topic.id + "/" + selection?.anchor} organizationId={organization.id} topic={topic} members={members} user={user}
          canWrite={canWriteSelected} canManage={["owner", "admin"].includes(organization.role)} draft={draft}
          modelAttempt={modelAttempts[draftKey]} onModelAttempt={(value) => setModelAttempts((existing) => ({ ...existing, [draftKey]: value }))}
          computeAttempt={computeAttempts[draftKey]} onComputeAttempt={(value) => setComputeAttempts((existing) => ({ ...existing, [draftKey]: value }))}
          workflowAttempt={workflowAttempts[draftKey]} onWorkflowAttempt={(value) => setWorkflowAttempts((existing) => ({ ...existing, [draftKey]: value }))}
          workflowExecutionAttempt={workflowExecutionAttempts[draftKey]} onWorkflowExecutionAttempt={(value) => setWorkflowExecutionAttempts((existing) => ({ ...existing, [draftKey]: value }))}
          updateDraft={(value) => setDrafts((existing) => ({ ...existing, [draftKey]: value }))} anchor={selection?.anchor ?? null} onPreference={preferenceRefresh}
          onForked={(id) => { void request<Hierarchy>(organizationPath(organization.id) + "/hierarchy").then((tree) => {
            setHierarchy(tree); select(id); preferenceRefresh();
          }).catch((cause) => setError(errorMessage(cause))); }} onAuthLost={authLost} />
          : <section className="org-empty"><h1>{organization ? "A place to reason together" : "Choose or create an organization"}</h1>
            <p>{organization ? "Projects hold channels, and channels hold focused conversations." : "Organizations keep their own conversations, members, and provider keys."}</p>
            {canCreate && <button onClick={() => hierarchy.channels.length ? beginCreation("topic", hierarchy.channels[0].id)
              : hierarchy.projects.length ? beginCreation("channel", hierarchy.projects[0].id) : beginCreation("project")}>Create {hierarchy.channels.length ? "a topic" : hierarchy.projects.length ? "a channel" : "a project"}</button>}
            {!organization && canCreateOrganization && <button onClick={() => beginCreation("organization")}>Create organization</button>}
          </section>}
      </main>
      {organization && <PresenceSidebar organizationId={organization.id} open={presenceOpen} onClose={() => setPresenceOpen(false)} onAuthLost={authLost} />}
    </div>
    {information && <Dialog title={information === "user" ? "User information" : "Organization information"} onClose={() => setInformation(null)}>
      {information === "user" ? <><dl><dt>Name</dt><dd>{user.name}</dd><dt>Email</dt><dd>{user.email}</dd></dl>
        <EmailVerification onVerified={() => { void request<Account>("/auth/me").then(setUser).catch(cause => setError(errorMessage(cause))); }} /></>
        : organization && <dl><dt>Name</dt><dd>{organization.name}</dd><dt>Your role</dt><dd>{organization.role}</dd></dl>}
    </Dialog>}
    {tool && organization && <Dialog title={tool === "bots" ? "My bots" : tool === "knowledge" ? "Shared knowledge" : tool === "prices" ? "Model capabilities" : "Usage and energy"} onClose={() => setTool(null)}>
      {tool === "bots" && pairCode && <label>Approve in organization<select value={organization.id} onChange={(event) => switchOrganization(event.target.value)}>
        {organizations.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}
      </select></label>}
      {tool === "bots" ? <BotAdministration key={organization.id} organizationId={organization.id} personal readOnly={organization.role === "viewer"} pairCode={pairCode} onConnected={() => {
        setPairCode(undefined); setTool(null); const url = new URL(window.location.href); url.searchParams.delete("bot_connect"); window.history.replaceState(null, "", url);
      }} />
        : tool === "knowledge" ? <KnowledgeBrowser key={organization.id} organizationId={organization.id} hierarchy={hierarchy}
          canWrite={organization.role !== "viewer"} onAuthLost={authLost} />
        : tool === "prices" ? <OrganizationModelBrowser key={organization.id} organizationId={organization.id} canManage={["owner", "admin"].includes(organization.role)} onAuthLost={authLost} />
          : <OrganizationUsagePanel key={organization.id} organizationId={organization.id} canManage={["owner", "admin"].includes(organization.role)} onAuthLost={authLost} />}
    </Dialog>}
    {shortcutsOpen && <ShortcutsHelp onClose={() => setShortcutsOpen(false)} />}
    {creation === "organization" && <OrganizationCreationDialog organizations={organizations}
      onClose={() => { setCreation(null); setCreationParent(undefined); }} onCreate={async (body) => {
        const item = await request<Organization>("/organizations", { method: "POST", body });
        setOrganizations((existing) => [...existing, item]); switchOrganization(item.id);
      }} />}
    {creation && creation !== "organization" && organization && <CreationDialog kind={creation} hierarchy={hierarchy} organizationId={organization.id} parentId={creationParent}
      members={members} userId={user.id} canCreatePrivate={["owner", "admin"].includes(organization.role)} onClose={() => { setCreation(null); setCreationParent(undefined); }} onCreate={async (body) => {
      if (organization) {
        const entity = await request<{ id: string }>(organizationPath(organization.id) + "/" + creation + "s", { method: "POST", body });
        if (creation === "topic") setSelection({ topic: entity.id, anchor: null });
        preferenceRefresh();
      }
    }} />}
    {entitySettings?.kind === "project" && organization && hierarchy.projects.find((item) => item.id === entitySettings.id) && <ProjectSettingsDialog
      organizationId={organization.id} project={hierarchy.projects.find((item) => item.id === entitySettings.id)!} members={members}
      canDelete={["owner", "admin"].includes(organization.role)}
      onClose={() => setEntitySettings(null)} onChanged={preferenceRefresh} />}
    {entitySettings?.kind === "channel" && organization && hierarchy.channels.find((item) => item.id === entitySettings.id) && <ChannelSettingsDialog
      organizationId={organization.id} channel={hierarchy.channels.find((item) => item.id === entitySettings.id)!}
      onClose={() => setEntitySettings(null)} onChanged={preferenceRefresh} />}
    {entitySettings?.kind === "topic" && organization && hierarchy.topics.some((item) => item.id === entitySettings.id) && <Dialog title="Topic settings" onClose={() => setEntitySettings(null)}>
      <TopicRenameControl key={entitySettings.id} organizationId={organization.id} topic={hierarchy.topics.find(item => item.id === entitySettings.id)!} onChanged={preferenceRefresh} />
      <DeletionControl organizationId={organization.id} kind="topic" entityId={entitySettings.id} onDeleted={() => { preferenceRefresh(); setEntitySettings(null); }} />
    </Dialog>}
    {admin && organization && <Administration key={organization.id} organization={organization} onClose={() => setAdmin(false)} onRefresh={preferenceRefresh} onAuthLost={authLost} />}
  </div>;
}
