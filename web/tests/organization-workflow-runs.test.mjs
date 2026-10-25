import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { after, before, test } from "node:test";
import { createServer } from "vite";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

let server, api, definitions, components, capacity;
const org = "a".repeat(32), topic = "b".repeat(32), user = "c".repeat(32), workflow = "d".repeat(32), runId = "e".repeat(32);
before(async () => {
  server = await createServer({ appType: "custom", logLevel: "silent", server: { middlewareMode: true } });
  api = await server.ssrLoadModule("/src/workflow-run-api.ts");
  definitions = await server.ssrLoadModule("/src/workflow-api.ts");
  components = await server.ssrLoadModule("/src/WorkflowRuns.tsx");
  capacity = await server.ssrLoadModule("/src/WorkflowCapacity.tsx");
});
after(async () => { await server?.close(); });
const version = () => {
  const definition = structuredClone(definitions.WORKFLOW_EXAMPLE);
  return { workflow_id: workflow, version: 2, definition,
    definition_digest: "sha256:" + createHash("sha256").update(definitions.canonicalWorkflow(definition)).digest("hex") };
};
const run = (attempt) => ({ id: runId, organization_id: org, topic_id: topic, requested_by: user, client_id: attempt.client_id,
  workflow_id: workflow, definition_version: attempt.version, definition_digest: attempt.definition_digest, definition: attempt.definition,
  status: "succeeded", revision: 15, cancel_requested: 0, restore_fenced: 0 });

test("collapsed and expanded comparison cards retain the entire question and model names without execution clutter", () => {
  const question = "A retained question\n" + "long mathematical statement " .repeat(80);
  for (const initiallyOpen of [false, true]) {
    const html = renderToStaticMarkup(React.createElement(components.WorkflowActivityCard, {
      run: { status: "succeeded", created_at: 1700000000, comparison: { question, models: ["new/model", "openrouter/auto"] } },
      initiallyOpen,
    }, React.createElement("p", null, "answer")));
    assert.ok(html.includes(question)); assert.match(html, /new\/model · openrouter\/auto/);
    assert.doesNotMatch(html, /revision|Run again|Workflow state events|>succeeded</);
    assert.equal(html.includes("answer"), initiallyOpen);
  }
});

async function withFetch(work) {
  const oldFetch = globalThis.fetch, oldDocument = Object.getOwnPropertyDescriptor(globalThis, "document");
  Object.defineProperty(globalThis, "document", { configurable: true, value: { cookie: "reasoning_csrf=workflow-csrf" } });
  try { await work(); } finally {
    globalThis.fetch = oldFetch;
    if (oldDocument) Object.defineProperty(globalThis, "document", oldDocument); else delete globalThis.document;
  }
}

test("execution captures clone and freeze pinned definitions, revisions, and approval attempts", () => {
  const original = version(), captured = api.captureWorkflowStart(original, "workflow-start-001");
  original.definition.nodes[0].parameters.message = "changed";
  assert.notEqual(captured.definition.nodes[0].parameters.message, "changed");
  assert.ok(Object.isFrozen(captured) && Object.isFrozen(captured.definition.nodes[0].parameters));
  const retry = api.captureWorkflowAction(runId, "workflow-retry-001", { operation: "retry", revision: 4 });
  assert.ok(Object.isFrozen(retry)); assert.equal(retry.revision, 4);
  for (const invalid of [0, NaN, 1.5, Number.MAX_SAFE_INTEGER + 1]) {
    assert.throws(() => api.captureWorkflowAction(runId, "workflow-retry-001", { operation: "retry", revision: invalid }), /exact/);
  }
  assert.throws(() => api.captureWorkflowStart({ ...version(), version: 129 }, "workflow-start-001"), /exact/);
  assert.throws(() => api.captureWorkflowStart(version(), "short"), /stable/);
  assert.throws(() => api.captureWorkflowAction(runId, "workflow-approve-001", { operation: "approve", node_id: "../node", attempt: 1, decision: "approve" }), /approval/);
  assert.throws(() => api.captureWorkflowAction(runId, "workflow-approve-001", { operation: "approve", node_id: "review", attempt: 17, decision: "approve" }), /exact/);
});

test("lost start replies recover the same body and accept current completed progress", async () => withFetch(async () => {
  const captured = api.captureWorkflowStart(version(), "workflow-start-001"), calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url, ...options });
    if (calls.length === 1) throw new Error("lost reply");
    return new Response(JSON.stringify(run(captured)), { status: 202 });
  };
  await assert.rejects(api.deliverWorkflowExecution(org, topic, user, captured), /lost reply/);
  assert.equal((await api.deliverWorkflowExecution(org, topic, user, captured)).status, "succeeded");
  assert.equal(calls[0].body, calls[1].body);
  assert.deepEqual(JSON.parse(calls[1].body), { client_id: captured.client_id, version: 2 });
  assert.equal(calls[1].url, `/api/organizations/${org}/workflows/${workflow}/runs`);
  assert.equal(calls[1].credentials, "same-origin"); assert.equal(calls[1].headers["X-CSRF-Token"], "workflow-csrf");
  for (const changed of [{ id: "bad" }, { organization_id: "other" }, { topic_id: "other" }, { requested_by: "other" },
    { client_id: "other" }, { workflow_id: "other" }, { definition_version: 3 }, { definition_digest: "other" },
    { definition: { ...captured.definition, name: "different" } }]) {
    globalThis.fetch = async () => new Response(JSON.stringify({ ...run(captured), ...changed }), { status: 202 });
    await assert.rejects(api.deliverWorkflowExecution(org, topic, user, captured), /does not match/);
  }
}));

test("approval, cancellation, and retry replays preserve exact request and bind immutable receipts", async () => withFetch(async () => {
  for (const spec of [{ operation: "cancel" }, { operation: "retry", revision: 15 },
    { operation: "release-capacity", revision: 15 },
    { operation: "approve", node_id: "review", attempt: 3, decision: "reject" }]) {
    const captured = api.captureWorkflowAction(runId, "workflow-action-001", spec), calls = [];
    const receipt = { run_id: runId, client_id: captured.client_id, requested_by: user, revision: 16, operation: spec.operation,
      ...(spec.operation === "approve" ? { node_id: "review", attempt: 3, decision: "reject" } : {}) };
    globalThis.fetch = async (url, options) => {
      calls.push({ url, ...options }); if (calls.length === 1) throw new Error("lost action reply");
      return new Response(JSON.stringify(receipt), { status: 200 });
    };
    await assert.rejects(api.deliverWorkflowExecution(org, topic, user, captured), /lost action reply/);
    assert.deepEqual(await api.deliverWorkflowExecution(org, topic, user, captured), receipt);
    assert.equal(calls[0].body, calls[1].body);
    const body = JSON.parse(calls[1].body); assert.equal(body.operation, undefined); assert.equal(body.run_id, undefined);
    if (spec.operation === "retry") assert.equal(body.revision, 15);
    if (spec.operation === "approve") { assert.equal(body.attempt, 3); assert.equal(body.decision, "reject"); }
    const suffix = spec.operation === "approve" ? "nodes/review/approval" : spec.operation;
    assert.equal(calls[1].url, `/api/organizations/${org}/workflow-runs/${runId}/${suffix}`);
    for (const changed of [{ run_id: "other" }, { client_id: "other" }, { requested_by: "other" }, { operation: "other" }, { revision: 0 },
      ...(spec.operation === "retry" ? [{ revision: 15 }] : []),
      ...(spec.operation === "approve" ? [{ node_id: "other" }, { attempt: 4 }, { decision: "approve" }] : [])]) {
      globalThis.fetch = async () => new Response(JSON.stringify({ ...receipt, ...changed }), { status: 200 });
      await assert.rejects(api.deliverWorkflowExecution(org, topic, user, captured), /does not match/);
    }
  }
}));

test("current authority, restoration fences, and node state determine available controls", () => {
  const value = structuredClone({ ...run(api.captureWorkflowStart(version(), "workflow-start-001")), status: "awaiting_approval" });
  assert.ok(api.canControlWorkflow(value, user, true, false));
  assert.ok(api.canControlWorkflow(value, "another", true, true));
  assert.equal(api.canControlWorkflow(value, "another", true, false), false);
  assert.equal(api.canControlWorkflow(value, user, false, true), false);
  assert.equal(api.canControlWorkflow({ ...value, restore_fenced: 1 }, user, true, true), false);
  const node = { node_id: "review", kind: "approval", status: "awaiting_approval" };
  assert.equal(api.canApproveWorkflow(value, node, true, false), false);
  assert.ok(api.canApproveWorkflow(value, node, true, true));
  assert.equal(api.canApproveWorkflow({ ...value, cancel_requested: 1 }, node, true, true), false);
  assert.equal(api.canApproveWorkflow({ ...value, restore_fenced: 1 }, node, true, true), false);
  assert.equal(api.canApproveWorkflow(value, { ...node, status: "succeeded" }, true, true), false);
  value.definition.nodes.find((entry) => entry.id === "review").parameters.required_role = "member";
  assert.ok(api.canApproveWorkflow(value, node, true, false));
  assert.equal(api.canApproveWorkflow(value, node, false, false), false);
  assert.equal(api.terminalWorkflow(value), false); assert.ok(api.terminalWorkflow({ ...value, status: "cancelled" }));
});

test("progress pages enforce monotonic bounded cursors and retain a bounded display tail", () => {
  assert.deepEqual(api.appendWorkflowEvents([{ sequence: 1 }], [{ sequence: 3 }], 1), [{ sequence: 1 }, { sequence: 3 }]);
  for (const page of [[{ sequence: 1 }], [{ sequence: 4 }, { sequence: 3 }], [{ sequence: 1.5 }], Array.from({ length: 201 }, (_, i) => ({ sequence: i + 2 }))]) {
    assert.throws(() => api.appendWorkflowEvents([], page, 1), /cursor/);
  }
  assert.equal(api.appendWorkflowEvents(Array.from({ length: 500 }, (_, i) => ({ sequence: i + 1 })), [{ sequence: 501 }], 500).length, 500);
  assert.throws(() => api.appendWorkflowEvents([], [], -1), /cursor/);
});

test("rendered run controls enforce writer/admin/fenced/locked boundaries and escape node evidence", () => {
  const value = structuredClone({ ...run(api.captureWorkflowStart(version(), "workflow-start-001")), node_order: ["review", "reason"], images: {}, status: "awaiting_approval",
    nodes: [{ node_id: "review", kind: "approval", status: "awaiting_approval", attempt: 1 }, { node_id: "reason", kind: "llm", status: "pending", attempt: 1 }] });
  value.definition.nodes[0].parameters.message = "<script>untrusted instruction</script>";
  const props = { run: value, userId: user, canWrite: true, canManage: true, locked: false, onAction: () => {}, onInspect: () => {} };
  const render = (changes = {}) => renderToStaticMarkup(React.createElement(components.WorkflowRunView, { ...props, ...changes }));
  const owner = render();
  assert.match(owner, /Approve review attempt 1/); assert.match(owner, /Reject review attempt 1/);
  assert.doesNotMatch(owner, /Cancel workflow run|Retry unsuccessful nodes/);
  assert.match(owner, /&lt;script&gt;untrusted instruction&lt;\/script&gt;/); assert.doesNotMatch(owner, /<script>/);
  assert.match(owner, /Inspect review attempts and results/); assert.match(owner, /Pinned definition and image evidence/);
  assert.doesNotMatch(render({ canManage: false }), /Approve review|Reject review/);
  const viewer = render({ canWrite: false, canManage: false });
  assert.doesNotMatch(viewer, /Approve review|Reject review|Cancel workflow run|Retry unsuccessful/); assert.match(viewer, /Inspect review/);
  assert.doesNotMatch(render({ run: { ...value, restore_fenced: 1 } }), /Approve review|Reject review|Cancel workflow run/);
  assert.match(render({ locked: true }), /disabled=""[^>]*>Approve review/);
  assert.doesNotMatch(render({ run: { ...value, status: "failed", nodes: value.nodes.map((node) => ({ ...node, status: "failed" })) } }), /Retry unsuccessful nodes/);
});

test("retained execution requests disclose their original identity and do not launch automatically", () => {
  const props = { organizationId: org, topicId: topic, userId: user, canWrite: true, canManage: false,
    version: null, definitionBusy: false, onAttempt: () => {}, onAuthLost: () => {} };
  const pending = api.captureWorkflowAction(runId, "workflow-cancel-001", { operation: "cancel" });
  const markup = renderToStaticMarkup(React.createElement(components.WorkflowRuns, { ...props, attempt: pending }));
  assert.match(markup, /workflow-cancel-001/); assert.match(markup, /Retry original cancel request/); assert.match(markup, /does not create a new request/);
  assert.doesNotMatch(markup, /localStorage|sessionStorage/);
  const readonly = renderToStaticMarkup(React.createElement(components.WorkflowRuns, { ...props, version: version(), canWrite: false }));
  assert.doesNotMatch(readonly, /Run workflow/);
});


test("workflow allowance counts hidden occupied places and keeps released runs under their own filter", () => {
  const retained = { id: runId, topic_id: topic, created_at: 1700000000, status: "outcome_unknown", revision: 4,
    capacity_released: 1, workflow_name: "Tensor comparison <script>", topic_name: "Motives", project_name: "Research",
    channel_name: "Mathematics", requested_by_name: "Owner", topic_deleted: 1 };
  const html = renderToStaticMarkup(React.createElement(capacity.WorkflowCapacitySummary, {
    value: { limit: 64, in_use: 4, available: 60, hidden_in_use: 4, released_count: 1, runs: [retained], truncated: false }, onInspect: () => {},
  }));
  assert.match(html, /4 of 64/); assert.match(html, /60 available/); assert.match(html, /4 occupied runs are/);
  assert.match(html, /<meter[^>]+max="64"[^>]+value="4"/);
  assert.match(html, /aria-pressed="true">In use <span>0<\/span>/); assert.match(html, />Released <span>1<\/span>/);
  assert.doesNotMatch(html, /Tensor comparison/, "a released run is listed only under Released");
  assert.doesNotMatch(html, /<script>|outcome_unknown|Release workflow slot/);
  assert.match(html, /aria-label="Search workflows and topics"/);
});

test("workflow allowance lists occupied work like project files, one row per run with Inspect", () => {
  const html = renderToStaticMarkup(React.createElement(capacity.WorkflowCapacitySummary, {
    value: { limit: 64, in_use: 1, available: 63, hidden_in_use: 0, released_count: 0, truncated: false, runs: [{
      id: runId, topic_id: topic, created_at: 1700000000, status: "awaiting_approval", revision: 1, capacity_released: 0,
      workflow_name: "Check the argument", project_name: "Research", channel_name: "Mathematics", topic_name: "Motives", requested_by_name: "Ben", topic_deleted: 0,
    }] }, onInspect: () => {},
  }));
  assert.match(html, /Check the argument/); assert.match(html, /Research \/ Mathematics \/ Motives · Ben/); assert.match(html, /Waiting for approval/);
  assert.match(html, />Waiting <span>1<\/span>/); assert.match(html, /data-tone="waiting"/);
  assert.match(html, /aria-label="Inspect Check the argument · [^"]+">Inspect<\/button>/);
  assert.doesNotMatch(html, /cannot access|Deleted topic/);
});
