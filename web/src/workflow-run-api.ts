import { organizationPath, request } from "./organization-api";
import { WorkflowDefinition, WorkflowNode, WorkflowVersion, canonicalWorkflow, workflowPath } from "./workflow-api";

export type WorkflowRunStatus = "running" | "awaiting_approval" | "outcome_unknown" | "succeeded" | "failed" | "cancelled";
export type WorkflowNodeAttempt = {
  organization_id: string; run_id: string; node_id: string; attempt: number; kind: WorkflowNode["kind"];
  status: WorkflowRunStatus | "pending" | "blocked"; created_at: number; finished_at: number | null;
  error_code: string | null; inputs_digest: string | null; compute_job_id: string | null; inference_run_id: string | null;
  bot_run_id?: string | null;
  approved_by: string | null; approval_decision: "approve" | "reject" | null;
};
export type WorkflowRunSummary = {
  sequence: number; id: string; organization_id: string; topic_id: string; workflow_id: string; definition_version: number; definition_digest: string;
  requested_by: string; client_id: string; created_at: number; status: WorkflowRunStatus; revision: number;
  cancel_requested: number; restore_fenced: number; capacity_released?: number; finished_at: number | null; name?: string; has_output_caps?: number;
  comparison?: { question: string; models: string[] } | null;
};
export type WorkflowRun = WorkflowRunSummary & {
  definition: WorkflowDefinition; definition_digest: string; node_order: string[]; images: Record<string, unknown>;
  nodes: WorkflowNodeAttempt[];
};
export type WorkflowRunEvent = { sequence: number; created_at: number; event: Record<string, unknown> };
export type WorkflowStartAttempt = {
  operation: "start"; client_id: string; workflow_id: string; version: number; definition: WorkflowDefinition; definition_digest: string;
};
export type WorkflowActionSpec = (
  { operation: "cancel" } | { operation: "retry"; revision: number } | { operation: "release-capacity"; revision: number } |
  { operation: "approve"; node_id: string; attempt: number; decision: "approve" | "reject" }
);
export type WorkflowActionAttempt = { client_id: string; run_id: string } & WorkflowActionSpec;
export type WorkflowExecutionAttempt = WorkflowStartAttempt | WorkflowActionAttempt;
export type WorkflowActionReceipt = {
  run_id: string; client_id: string; requested_by: string; revision: number; operation: "cancel" | "retry" | "approve" | "release-capacity";
  node_id?: string; attempt?: number; decision?: "approve" | "reject";
};

export const workflowRunPath = (organizationId: string, runId: string) =>
  organizationPath(organizationId) + "/workflow-runs/" + encodeURIComponent(runId);
export const terminalWorkflow = (run: WorkflowRunSummary) => ["succeeded", "failed", "cancelled"].includes(run.status);
export const canControlWorkflow = (run: WorkflowRunSummary, userId: string, canWrite: boolean, canManage: boolean) =>
  canWrite && !run.restore_fenced && (run.requested_by === userId || canManage);
export const canApproveWorkflow = (run: WorkflowRun, node: WorkflowNodeAttempt, canWrite: boolean, canManage: boolean) => {
  const spec = run.definition.nodes.find((value) => value.id === node.node_id);
  return canWrite && !run.restore_fenced && !run.cancel_requested && node.status === "awaiting_approval" && node.kind === "approval" &&
    !!spec && spec.kind === "approval" && (spec.parameters.required_role === "member" || canManage);
};

function clientId(value: string) {
  if (!/^[A-Za-z0-9_-]{16,128}$/.test(value)) throw new Error("A stable workflow request ID is required.");
}
function identifier(value: string) {
  if (!/^[a-f0-9]{32}$/.test(value)) throw new Error("Select a valid workflow or run.");
}
function whole(value: number, upper = Number.MAX_SAFE_INTEGER) {
  if (!Number.isSafeInteger(value) || value < 1 || value > upper) throw new Error("Select an exact workflow version, revision, or attempt.");
}
function immutable<T>(value: T): T {
  if (value !== null && typeof value === "object") { Object.values(value).forEach(immutable); Object.freeze(value); }
  return value;
}

export function captureWorkflowStart(version: WorkflowVersion, id: string): WorkflowStartAttempt {
  clientId(id); identifier(version.workflow_id); whole(version.version, 128);
  if (!/^sha256:[a-f0-9]{64}$/.test(version.definition_digest)) throw new Error("Select a verified immutable workflow version.");
  return immutable({ operation: "start", client_id: id, workflow_id: version.workflow_id, version: version.version,
    definition: JSON.parse(canonicalWorkflow(version.definition)), definition_digest: version.definition_digest });
}
export function captureWorkflowAction(runId: string, id: string, action: WorkflowActionSpec): WorkflowActionAttempt {
  identifier(runId); clientId(id);
  if (action.operation === "cancel") return immutable({ operation: "cancel", run_id: runId, client_id: id });
  if (action.operation === "retry" || action.operation === "release-capacity") {
    whole(action.revision!);
    return immutable({ operation: action.operation, run_id: runId, client_id: id, revision: action.revision! });
  }
  if (action.operation !== "approve" || !/^[a-z][a-z0-9_-]{0,63}$/.test(action.node_id ?? "") ||
      !["approve", "reject"].includes(action.decision ?? "")) throw new Error("Select an exact approval node and decision.");
  whole(action.attempt!, 16);
  return immutable({ operation: "approve", run_id: runId, client_id: id,
    node_id: action.node_id!, attempt: action.attempt!, decision: action.decision! });
}

export async function deliverWorkflowExecution(organizationId: string, topicId: string, userId: string,
  attempt: WorkflowExecutionAttempt): Promise<WorkflowRun | WorkflowActionReceipt> {
  if (attempt.operation === "start") {
    const run = await request<WorkflowRun>(workflowPath(organizationId, attempt.workflow_id) + "/runs",
      { method: "POST", body: { client_id: attempt.client_id, version: attempt.version } });
    // Start replay returns current progress; identity and pinned definition must
    // still match the original request even if the run has already completed.
    if (!run || !/^[a-f0-9]{32}$/.test(run.id) || run.organization_id !== organizationId || run.topic_id !== topicId ||
        run.requested_by !== userId || run.client_id !== attempt.client_id || run.workflow_id !== attempt.workflow_id ||
        run.definition_version !== attempt.version || run.definition_digest !== attempt.definition_digest ||
        canonicalWorkflow(run.definition) !== canonicalWorkflow(attempt.definition)) throw new Error(
          "Workflow run receipt does not match the original request. Retain its ID and retry unchanged.");
    return run;
  }
  const path = workflowRunPath(organizationId, attempt.run_id);
  const body = attempt.operation === "approve" ? { client_id: attempt.client_id, attempt: attempt.attempt, decision: attempt.decision } :
    (attempt.operation === "retry" || attempt.operation === "release-capacity")
      ? { client_id: attempt.client_id, revision: attempt.revision } : { client_id: attempt.client_id };
  const suffix = attempt.operation === "approve" ? "/nodes/" + encodeURIComponent(attempt.node_id) + "/approval" : "/" + attempt.operation;
  const result = await request<WorkflowActionReceipt>(path + suffix, { method: "POST", body });
  if (!result || result.run_id !== attempt.run_id || result.client_id !== attempt.client_id || result.requested_by !== userId ||
      result.operation !== attempt.operation || !Number.isSafeInteger(result.revision) || result.revision < 1 ||
      ((attempt.operation === "retry" || attempt.operation === "release-capacity") && result.revision <= attempt.revision) ||
      (attempt.operation === "approve" && (result.node_id !== attempt.node_id || result.attempt !== attempt.attempt || result.decision !== attempt.decision))) {
    throw new Error("Workflow action receipt does not match the original request. Retain its ID and retry unchanged.");
  }
  return result;
}

export function appendWorkflowEvents(existing: WorkflowRunEvent[], page: WorkflowRunEvent[], after: number): WorkflowRunEvent[] {
  if (!Number.isSafeInteger(after) || after < 0 || page.length > 200 || page.some((entry, index) => !Number.isSafeInteger(entry.sequence) ||
      entry.sequence <= (index ? page[index - 1].sequence : after))) throw new Error("Invalid workflow event cursor order.");
  return [...existing, ...page].slice(-500);
}
