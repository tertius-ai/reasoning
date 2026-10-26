import React, { useEffect, useRef, useState } from "react";
import { ActivityDialog, TopicActivity } from "./TopicActivity";
import { ComputeConfiguration, initialComputeResources } from "./compute-api";
import { ModelPicker } from "./ModelBrowser";
import { availableEfforts } from "./ReasoningEffort";
import { withBotModels } from "./botModels";
import { useTopicCommand } from "./TopicCommands";
import { ResearchFiles } from "./ResearchFiles";
import { MathematicsBrowser } from "./MathematicsBrowser";
import {
  ApiError, BotModel, InferenceConfiguration, OrganizationPrices, aborted, errorMessage, modelCatalog, organizationPath, request, sharedRequest, topicPath,
} from "./organization-api";
import { WorkflowRuns } from "./WorkflowRuns";
import { WorkflowExecutionAttempt } from "./workflow-run-api";
import {
  WORKFLOW_EXAMPLE, WorkflowAttempt, WorkflowDefinition, WorkflowNode, WorkflowVersion,
  captureWorkflowAttempt, deliverWorkflowAttempt,
} from "./workflow-api";

type EditorMode = "comparison" | "builder" | "json" | null;
type StepKind = WorkflowNode["kind"];
export type BuilderStep = {
  kind: StepKind; model: string; effort?: string; prompt: string; message: string; requiredRole: "member" | "admin";
  imageId: string; language: string; program: string;
};

const outputFor = (kind: StepKind) => kind === "llm" ? "text" : kind === "compute" ? "artifacts" : "decision";
const isComparisonDefinition = (definition: WorkflowDefinition) => definition.nodes.length >= 2 && definition.nodes.every((node) =>
  node.kind === "llm" && node.depends_on.length === 0 && node.parameters.prompt === definition.nodes[0].parameters.prompt);

export function comparisonDefinition(question: string, models: string[], catalog: OrganizationPrices | null = null,
  efforts: Record<string, string> = {}): WorkflowDefinition {
  const prompt = question.trim(), selected = models.filter(Boolean);
  if (!prompt || selected.length < 2 || selected.length > 8 || new Set(selected).size !== selected.length) {
    throw new Error("Enter a question and choose two to eight different models.");
  }
  const label = prompt.replace(/\s+/g, " ");
  return { format: 1, name: label.length > 72 ? label.slice(0, 69).trimEnd() + "…" : label, nodes: selected.map((model, index) => ({
    id: `model_${index + 1}`, kind: "llm", depends_on: [], inputs: {},
    parameters: { model, prompt, max_completion_tokens: null,
      ...(efforts[model] && availableEfforts(catalog, model).includes(efforts[model]) ? { effort: efforts[model] } : {}) },
  })) };
}

export function builderDefinition(name: string, steps: BuilderStep[], compute: ComputeConfiguration | null,
  catalog: OrganizationPrices | null = null): WorkflowDefinition {
  if (!name.trim() || !steps.length) throw new Error("Name the workflow and add at least one step.");
  const nodes: WorkflowNode[] = steps.map((step, index) => {
    const previous = index ? `step_${index}` : null;
    const inputs: WorkflowNode["inputs"] = previous
      ? { previous: { node: previous, output: outputFor(steps[index - 1].kind) } } : {};
    const shared = { id: `step_${index + 1}`, kind: step.kind, depends_on: previous ? [previous] : [], inputs };
    if (step.kind === "llm") {
      if (!step.model || !step.prompt.trim()) throw new Error(`Complete AI step ${index + 1}.`);
      return { ...shared, parameters: { model: step.model, prompt: step.prompt.trim(), max_completion_tokens: null,
        ...(step.effort && availableEfforts(catalog, step.model).includes(step.effort) ? { effort: step.effort } : {}) } };
    }
    if (step.kind === "approval") {
      if (!step.message.trim()) throw new Error(`Complete approval step ${index + 1}.`);
      return { ...shared, parameters: { message: step.message.trim(), required_role: step.requiredRole } };
    }
    const image = compute?.images.find((item) => item.id === step.imageId && compute.policy.allowed_images.includes(item.id));
    if (!image || !image.languages.includes(step.language) || !step.program.trim()) throw new Error(`Complete compute step ${index + 1}.`);
    return { ...shared, parameters: { image_id: image.id, language: step.language, program: step.program.trim(),
      resources: initialComputeResources(image.maximum_resources) } };
  });
  return { format: 1, name: name.trim(), nodes };
}

const blankStep = (kind: StepKind, models: string[], compute: ComputeConfiguration | null): BuilderStep => {
  const image = compute?.images.find((item) => compute.policy.allowed_images.includes(item.id));
  return { kind, model: models[0] ?? "", prompt: "", message: "", requiredRole: "member",
    imageId: image?.id ?? "", language: image?.languages[0] ?? "", program: "" };
};

const kindName = (kind: StepKind) => kind === "llm" ? "AI" : kind === "compute" ? "Compute" : "Approval";

export function WorkflowVersionDetail({ version }: { version: WorkflowVersion }) {
  return <section className="org-workflow-version" aria-label="Selected workflow">
    <header><h3>{version.definition.name}</h3><span>v{version.version}</span></header>
    <ol>{version.node_order.map((id) => { const node = version.definition.nodes.find((item) => item.id === id)!;
      return <li key={id}><strong>{kindName(node.kind)}</strong><span>{node.kind === "llm" ? String(node.parameters.model) : node.id}</span></li>;
    })}</ol>
    <details><summary>Technical details</summary><p><code>{version.definition_digest}</code></p>
      <pre>{JSON.stringify(version.definition, null, 2)}</pre></details>
  </section>;
}

function ComparisonSetup({ models, catalog, busy, onCancel, onSave }: {
  models: string[]; catalog: OrganizationPrices | null; busy: boolean; onCancel: () => void;
  onSave: (definition: WorkflowDefinition) => void;
}) {
  const [question, setQuestion] = useState("");
  const [selected, setSelected] = useState<string[]>([models[0] ?? "", models[1] ?? ""]);
  const [efforts, setEfforts] = useState<Record<string, string>>({});
  const [error, setError] = useState("");
  useEffect(() => {
    setSelected((current) => current.every(Boolean) || models.length < 2 ? current : [models[0], models[1]]);
  }, [models]);
  return <form className="org-workflow-editor" onSubmit={(event) => { event.preventDefault(); try {
    onSave(comparisonDefinition(question, selected, catalog, efforts)); setError("");
  } catch (cause) { setError(errorMessage(cause)); } }}>
    <header><div><h3>Compare models</h3><small>Every model receives the same question.</small></div><button type="button" onClick={onCancel}>Cancel</button></header>
    <p className="org-help" aria-label="Comparison capabilities">Native comparisons have no research tools, browsing or source readers.
      Referenced attachments supply extracted text and supported image previews. For source research, use a beta topic with a tool-capable model.
      CLI bot models use their runner's current grants.</p>
    <label>Question<textarea autoFocus required rows={4} value={question} onChange={(event) => setQuestion(event.target.value)} /></label>
    <fieldset><legend>Models</legend>{selected.map((model, index) => <div className="org-workflow-model" key={index}>
      <ModelPicker label={`Model ${index + 1}`} models={models.filter(id => !selected.some((chosen, position) => position !== index && chosen === id))}
        catalog={catalog} value={model} disabled={busy} onChange={id => setSelected(current => current.map((item, position) => position === index ? id : item))}
        effort={efforts[model] ?? ""} onEffortChange={value => setEfforts(current => ({ ...current, [model]: value }))} />
      {selected.length > 2 && <button type="button" aria-label={`Remove model ${index + 1}`} onClick={() => setSelected((current) => current.filter((_, position) => position !== index))}>×</button>}
    </div>)}</fieldset>
    {models.length < 2 && <p className="org-help">Enable at least two organization models to compare them.</p>}
    {selected.length < Math.min(8, models.length) && <button type="button" onClick={() => setSelected((current) => [...current, models.find((item) => !current.includes(item)) ?? ""])}>+ Add model</button>}
    {error && <p role="alert" className="org-error">{error}</p>}
    <button className="org-primary" disabled={busy || models.length < 2}>{busy ? "Starting…" : "Run comparison"}</button>
  </form>;
}

function WorkflowBuilder({ models, catalog, compute, busy, onCancel, onSave }: {
  models: string[]; catalog: OrganizationPrices | null; compute: ComputeConfiguration | null; busy: boolean;
  onCancel: () => void; onSave: (definition: WorkflowDefinition) => void;
}) {
  const [name, setName] = useState("");
  const [steps, setSteps] = useState<BuilderStep[]>([blankStep("llm", models, compute)]);
  const [error, setError] = useState("");
  useEffect(() => {
    setSteps((current) => current.map((step) => step.kind === "llm" && !step.model && models[0]
      ? { ...step, model: models[0] } : step.kind === "compute" && !step.imageId ? { ...step, ...blankStep("compute", models, compute) } : step));
  }, [models, compute]);
  const computeAvailable = compute?.images.some((item) => compute.policy.allowed_images.includes(item.id)) ?? false;
  const change = (index: number, update: Partial<BuilderStep>) => setSteps((current) => current.map((step, position) => position === index ? { ...step, ...update } : step));
  return <form className="org-workflow-editor" onSubmit={(event) => { event.preventDefault(); try {
    onSave(builderDefinition(name, steps, compute, catalog)); setError("");
  } catch (cause) { setError(errorMessage(cause)); } }}>
    <header><div><h3>New workflow</h3><small>Steps run from top to bottom.</small></div><button type="button" onClick={onCancel}>Cancel</button></header>
    <label>Name<input autoFocus required maxLength={200} value={name} onChange={(event) => setName(event.target.value)} /></label>
    <ol className="org-workflow-steps">{steps.map((step, index) => {
      const images = compute?.images.filter((item) => compute.policy.allowed_images.includes(item.id)) ?? [];
      const image = images.find((item) => item.id === step.imageId);
      return <li key={index}><header><span>{index + 1}</span><select aria-label={`Step ${index + 1} type`} value={step.kind} onChange={(event) => change(index, blankStep(event.target.value as StepKind, models, compute))}>
        <option value="llm">AI</option><option value="approval">Approval</option><option value="compute" disabled={!computeAvailable}>Compute</option></select>
        {steps.length > 1 && <button type="button" aria-label={`Remove step ${index + 1}`} onClick={() => setSteps((current) => current.filter((_, position) => position !== index))}>×</button>}</header>
        {step.kind === "llm" && <><ModelPicker label={`Step ${index + 1} model`} models={models} catalog={catalog} value={step.model}
          onChange={id => change(index, { model: id, effort: undefined })} disabled={busy}
          effort={step.effort ?? ""} onEffortChange={value => change(index, { effort: value })} />
          <label>Instruction<textarea required rows={3} value={step.prompt} onChange={(event) => change(index, { prompt: event.target.value })} /></label></>}
        {step.kind === "approval" && <><label>Approval request<input required value={step.message} onChange={(event) => change(index, { message: event.target.value })} /></label>
          <label>Required role<select value={step.requiredRole} onChange={(event) => change(index, { requiredRole: event.target.value as "member" | "admin" })}>
            <option value="member">Member</option><option value="admin">Administrator</option></select></label></>}
        {step.kind === "compute" && <>{images.length ? <><label>Image<select required value={step.imageId} onChange={(event) => { const next = images.find((item) => item.id === event.target.value); change(index, { imageId: event.target.value, language: next?.languages[0] ?? "" }); }}>
          {images.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}</select></label>
          <label>Language<select required value={step.language} onChange={(event) => change(index, { language: event.target.value })}>
            {image?.languages.map((language) => <option key={language}>{language}</option>)}</select></label>
          <label>Program<textarea required rows={5} spellCheck={false} value={step.program} onChange={(event) => change(index, { program: event.target.value })} /></label></>
          : <p className="org-help">No compute image is enabled.</p>}</>}
      </li>;
    })}</ol>
    <div className="org-workflow-add"><button type="button" onClick={() => setSteps((current) => [...current, blankStep("llm", models, compute)])}>+ AI</button>
      <button type="button" onClick={() => setSteps((current) => [...current, blankStep("approval", models, compute)])}>+ Approval</button>
      <button type="button" disabled={!computeAvailable} onClick={() => setSteps((current) => [...current, blankStep("compute", models, compute)])}>+ Compute</button></div>
    {error && <p role="alert" className="org-error">{error}</p>}
    <button className="org-primary" disabled={busy}>{busy ? "Submitting…" : "Run workflow"}</button>
  </form>;
}

export function WorkflowConversation({ organizationId, topicId, userId, canWrite, canManage, attempt, onAttempt, executionAttempt, onExecutionAttempt, onAuthLost, toolbarTarget, menuTarget = null, onActivity, hidePlay }: {
  organizationId: string; topicId: string; userId: string; canWrite: boolean; canManage: boolean; attempt?: WorkflowAttempt;
  executionAttempt?: WorkflowExecutionAttempt; onExecutionAttempt: (value?: WorkflowExecutionAttempt) => void;
  onAttempt: (value?: WorkflowAttempt) => void; onAuthLost: () => void; toolbarTarget: HTMLDivElement | null;
  /** The topic details card in the top bar: Workflow activity appears there. */
  menuTarget?: HTMLElement | null;
  onActivity: (entries: TopicActivity[]) => void;
  hidePlay?: boolean;
}) {
  const [version, setVersion] = useState<WorkflowVersion | null>(null);
  const [source, setSource] = useState(JSON.stringify(attempt?.definition ?? WORKFLOW_EXAMPLE, null, 2));
  const [editor, setEditor] = useState<EditorMode>(attempt ? "json" : null), [error, setError] = useState(""), [busy, setBusy] = useState(false);
  const [models, setModels] = useState<string[]>([]), [catalog, setCatalog] = useState<OrganizationPrices | null>(null);
  const [compute, setCompute] = useState<ComputeConfiguration | null>(null), [autoStartKey, setAutoStartKey] = useState<string | null>(null);
  const [resource, setResource] = useState<"ledger" | "files" | null>(null);
  const alive = useRef(true), action = useRef(false), pending = useRef(attempt);
  pending.current = attempt;
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const failure = (cause: unknown) => {
    if (!alive.current) return;
    if (cause instanceof ApiError && cause.status === 401) onAuthLost(); else setError(errorMessage(cause));
  };
  useEffect(() => {
    if (attempt) { setSource(JSON.stringify(attempt.definition, null, 2)); setEditor("json"); }
  }, [attempt]);
  useEffect(() => {
    const controller = new AbortController();
    const root = organizationPath(organizationId);
    let allowed: string[] = [], bots: BotModel[] = [], prices: OrganizationPrices | null = null;
    const updateModels = () => {
      if (!controller.signal.aborted) { setModels([...allowed, ...bots.map(bot => bot.id)]); setCatalog(withBotModels(prices, bots)); }
    };
    void sharedRequest<InferenceConfiguration>(root + "/inference-configuration", { maxAge: 2000, signal: controller.signal })
      .then(value => { allowed = value.allowed_models; updateModels(); }).catch(cause => { if (!aborted(cause)) failure(cause); });
    void sharedRequest<BotModel[]>(topicPath(organizationId, topicId) + "/bot-models", { maxAge: 2000, signal: controller.signal })
      .then(value => { bots = value; updateModels(); }).catch(cause => { if (!aborted(cause)) failure(cause); });
    void modelCatalog(organizationId, controller.signal)
      .then(value => { prices = value; updateModels(); }).catch(() => {});
    void sharedRequest<ComputeConfiguration>(root + "/compute-configuration", { maxAge: 2000, signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) setCompute(value); }).catch(cause => { if (!aborted(cause)) failure(cause); });
    return () => controller.abort();
  }, [organizationId, topicId, onAuthLost]);
  const saveDefinition = async (definition: WorkflowDefinition) => {
    if (!canWrite || action.current || executionAttempt) return;
    const encoded = JSON.stringify(definition, null, 2); setSource(encoded);
    action.current = true; setBusy(true); setError("");
    try {
      const captured = pending.current ?? captureWorkflowAttempt(encoded, crypto.randomUUID());
      pending.current = captured; onAttempt(captured);
      const receipt = await deliverWorkflowAttempt(organizationId, topicId, userId, captured);
      if (!alive.current) return;
      pending.current = undefined; onAttempt(undefined); setVersion(receipt); setEditor(null);
      setAutoStartKey(receipt.workflow_id + ":" + receipt.version);
    } catch (cause) {
      if (alive.current && cause instanceof ApiError && [400, 403, 404, 409, 422, 429].includes(cause.status)) {
        pending.current = undefined; onAttempt(undefined);
      }
      failure(cause);
    } finally { action.current = false; if (alive.current) setBusy(false); }
  };
  const locked = busy || !!attempt || !!executionAttempt;
  useTopicCommand(topicId, command => {
    if (["workflow", "compare", "json"].includes(command) && canWrite && !locked) {
      setEditor(command === "workflow" ? "builder" : command === "compare" ? "comparison" : "json"); setError("");
    }
    if (command === "files" || command === "ledger") setResource(command);
  });
  return <>
    {resource && <ActivityDialog title={resource === "files" ? "Topic files" : "Shared ledger"} onClose={() => setResource(null)}>
      {resource === "files" ? <ResearchFiles {...{ organizationId, topicId, canWrite, onAuthLost }} />
        : <MathematicsBrowser {...{ organizationId, topicId, canWrite, onAuthLost }} />}
    </ActivityDialog>}
    {!editor && error && <p role="alert" className="org-error">{error}</p>}
    {!editor && attempt && <button type="button" onClick={() => setEditor("json")}>Resume workflow request</button>}
    {editor && <ActivityDialog title={editor === "comparison" ? "Compare models" : editor === "builder" ? "New workflow" : "Workflow JSON"} onClose={() => setEditor(null)}>
      {error && <p role="alert" className="org-error">{error}</p>}
      {attempt && <p role="status">An unacknowledged definition <code>{attempt.client_id}</code> is retained. Retry sends the exact same request.</p>}
      <fieldset disabled={busy || !!executionAttempt || !!attempt}>
        {editor === "comparison" && <ComparisonSetup models={models} catalog={catalog} busy={busy} onCancel={() => setEditor(null)} onSave={definition => void saveDefinition(definition)} />}
        {editor === "builder" && <WorkflowBuilder models={models} catalog={catalog} compute={compute} busy={busy} onCancel={() => setEditor(null)} onSave={definition => void saveDefinition(definition)} />}
      </fieldset>
      {editor === "json" && <form className="org-workflow-editor" onSubmit={event => { event.preventDefault(); try {
        void saveDefinition(attempt?.definition ?? JSON.parse(source) as WorkflowDefinition);
      } catch (cause) { failure(cause); } }}>
        <label>Definition JSON<textarea aria-label="Definition JSON" rows={12} maxLength={65536} spellCheck={false} value={attempt ? JSON.stringify(attempt.definition, null, 2) : source}
          disabled={locked} onChange={event => setSource(event.target.value)} /></label>
        <button className="org-primary" disabled={busy || !!executionAttempt}>{busy ? "Submitting…" : attempt ? "Retry unchanged definition" : "Run workflow"}</button>
      </form>}
    </ActivityDialog>}
    <WorkflowRuns organizationId={organizationId} topicId={topicId} userId={userId} canWrite={canWrite} canManage={canManage}
      version={version} definitionBusy={busy || !!attempt} attempt={executionAttempt} onAttempt={onExecutionAttempt} onAuthLost={onAuthLost}
      autoStartKey={autoStartKey} onAutoStarted={() => setAutoStartKey(null)} toolbarTarget={toolbarTarget} menuTarget={menuTarget} onActivity={onActivity} hidePlay={hidePlay}
      onUncap={definition => { setSource(JSON.stringify(definition, null, 2)); setEditor("json"); setError(""); }} />
  </>;
}
