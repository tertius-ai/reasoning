import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

let server, api, components, administration, resourceComponent, scalerAdministration;
const org = "a".repeat(32), topic = "b".repeat(32), user = "c".repeat(32);
before(async () => {
  server = await createServer({ appType: "custom", logLevel: "silent", server: { middlewareMode: true } });
  api = await server.ssrLoadModule("/src/compute-api.ts");
  components = await server.ssrLoadModule("/src/ComputeConversation.tsx");
  administration = await server.ssrLoadModule("/src/ComputeAdministration.tsx");
  resourceComponent = await server.ssrLoadModule("/src/ComputeResources.tsx");
  scalerAdministration = await server.ssrLoadModule("/src/ScalerAdministration.tsx");
});
after(async () => { await server?.close(); });
const image = () => ({ id: "cas", name: "Approved algebra", languages: ["sage", "python"], maximum_resources: { ...api.DEFAULT_COMPUTE_RESOURCES } });
const captured = () => api.captureComputeAttempt(image(), "sage", "print(2 + 2)", api.DEFAULT_COMPUTE_RESOURCES, "same-request");
const receipt = (attempt) => ({ id: "d".repeat(32), organization_id: org, topic_id: topic, requested_by: user,
  client_id: attempt.client_id, parameters: { image_id: attempt.image_id, language: attempt.language, program: attempt.program, resources: attempt.resources } });

test("Lean project example has explicit target/type and bounded mathlib source", () => {
  const value = JSON.parse(components.LEAN_PROJECT_EXAMPLE);
  assert.equal(value.format, 1); assert.equal(value.target, "target");
  assert.equal(value.expected_type, "(2 : Nat) + 3 = 5");
  assert.match(value.files[value.entry], /import Mathlib.Tactic.NormNum/);
  assert.ok(components.LEAN_PROJECT_EXAMPLE.length < 16000);
});

test("scaler grant capture enforces immutable selected-image limits and explicit ceilings", () => {
  const ids = ["cas"], limits = { ...api.DEFAULT_COMPUTE_RESOURCES };
  const body = api.captureScalerGrant("Operator scaler", ids, limits, 2, 30, [image()]);
  ids.push("other"); limits.cpu = 64;
  assert.deepEqual(body.image_ids, ["cas"]); assert.equal(body.resources.cpu, 1);
  assert.equal(body.maximum_workers, 2); assert.equal(body.lifetime_seconds, 2592000);
  for (const maximum of [0, 65, true, "2", 1.5, NaN]) assert.throws(() => api.captureScalerGrant("Scaler", ["cas"], api.DEFAULT_COMPUTE_RESOURCES, maximum, 30, [image()]));
  for (const days of [0, 366, true, "30", 1.5, NaN]) assert.throws(() => api.captureScalerGrant("Scaler", ["cas"], api.DEFAULT_COMPUTE_RESOURCES, 2, days, [image()]));
  for (const chosen of [[], ["cas", "cas"], ["unknown"]]) assert.throws(() => api.captureScalerGrant("Scaler", chosen, api.DEFAULT_COMPUTE_RESOURCES, 2, 30, [image()]));
  assert.throws(() => api.captureScalerGrant("Scaler", ["cas"], { ...api.DEFAULT_COMPUTE_RESOURCES, cpu: 2 }, 2, 30, [image()]));
  for (const name of ["", " ", "x".repeat(201), "x\0y"]) assert.throws(() => api.captureScalerGrant(name, ["cas"], api.DEFAULT_COMPUTE_RESOURCES, 2, 30, [image()]));
});

test("scaler administration explains delegated execution and keeps cloud authority separate", () => {
  const markup = renderToStaticMarkup(React.createElement(scalerAdministration.ScalerAdministration, { organizationId: org, images: [image()], onAuthLost: () => {} }));
  assert.match(markup, /Scoped compute automation/); assert.match(markup, /assigned programs and execute jobs/);
  assert.match(markup, /cannot read chat\/provider keys or create cloud hardware/);
  assert.match(markup, /Worker counts are not cloud spending limits/);
  assert.match(markup, /Maximum scaler-enrolled workers/); assert.match(markup, /Scaler lifetime \(days\)/);
  assert.match(markup, /Create scaler credential/); assert.match(markup, /rotation preserves worker enrollment request IDs/);
  assert.doesNotMatch(markup, /token_hash|Authorization: Bearer|localStorage|sessionStorage/);
});

test("resource validation matches all six strict integer server bounds and scratch relation", () => {
  assert.deepEqual(api.validateComputeResources(api.DEFAULT_COMPUTE_RESOURCES), api.DEFAULT_COMPUTE_RESOURCES);
  for (const [key, , minimum, maximum] of api.RESOURCE_FIELDS) {
    for (const value of [true, "1", NaN, Infinity, minimum - 1, maximum + 1, minimum + 0.5]) {
      assert.throws(() => api.validateComputeResources({ ...api.DEFAULT_COMPUTE_RESOURCES, [key]: value }), /whole number/);
    }
  }
  assert.throws(() => api.validateComputeResources({ ...api.DEFAULT_COMPUTE_RESOURCES, extra: 1 }), /six/);
  assert.throws(() => api.validateComputeResources({ ...api.DEFAULT_COMPUTE_RESOURCES, artifact_bytes: api.DEFAULT_COMPUTE_RESOURCES.disk_bytes }), /half/);
  assert.throws(() => api.validateComputeResources({ ...api.DEFAULT_COMPUTE_RESOURCES, cpu: 2 }, api.DEFAULT_COMPUTE_RESOURCES), /CPU cores/);
});

test("retired enrollment labels remain distinct from reversible draining and administrative revocation", () => {
  const worker = { retired_at: null, revoked_at: null, draining: 0 };
  assert.equal(api.computeWorkerStatus(worker), "Accepting claims");
  assert.equal(api.computeWorkerStatus({ ...worker, draining: 1 }), "Draining");
  assert.equal(api.computeWorkerStatus({ ...worker, revoked_at: 1 }), "Revoked");
  assert.equal(api.computeWorkerStatus({ ...worker, retired_at: 1, revoked_at: 1, draining: 1 }), "Retired (execution access revoked)");
});

test("new-image defaults fit smaller approved maxima without mutating shared defaults", () => {
  const maximum = { ...api.DEFAULT_COMPUTE_RESOURCES, memory_bytes: 64 * 1024 ** 2, disk_bytes: 8 * 1024 ** 2, wall_time_seconds: 10, pids: 16 };
  const result = api.initialComputeResources(maximum);
  assert.equal(result.wall_time_seconds, 10); assert.equal(result.memory_bytes, maximum.memory_bytes); assert.equal(result.pids, 16);
  assert.equal(api.DEFAULT_COMPUTE_RESOURCES.wall_time_seconds, 120);
  assert.deepEqual(api.validateComputeResources(result, maximum), result);
});

test("mathlib memory default is explicit and still clamped to approved maxima", () => {
  const defaults = { ...api.DEFAULT_COMPUTE_RESOURCES, memory_bytes: 4 * 1024 ** 3 };
  const maximum = { ...defaults, memory_bytes: 8 * 1024 ** 3 };
  assert.equal(api.initialComputeResources(maximum, defaults).memory_bytes, 4 * 1024 ** 3);
  assert.equal(api.initialComputeResources(api.DEFAULT_COMPUTE_RESOURCES, defaults).memory_bytes, 1024 ** 3);
  assert.equal(api.DEFAULT_COMPUTE_RESOURCES.memory_bytes, 1024 ** 3);
});

test("attempt capture freezes values against later edits and refuses invalid source or language", () => {
  const resources = { ...api.DEFAULT_COMPUTE_RESOURCES };
  const attempt = api.captureComputeAttempt(image(), "sage", "print(2 + 2)", resources, "stable");
  resources.cpu = 64;
  assert.equal(attempt.resources.cpu, 1);
  for (const program of ["", "  ", "x\0y", "x".repeat(16001), "\ud800", "\udc00"]) assert.throws(() => api.captureComputeAttempt(image(), "sage", program, api.DEFAULT_COMPUTE_RESOURCES, "stable"));
  assert.equal(api.captureComputeAttempt(image(), "sage", "😀", api.DEFAULT_COMPUTE_RESOURCES, "stable").program, "😀");
  assert.throws(() => api.captureComputeAttempt(image(), "unsupported", "print(2)", api.DEFAULT_COMPUTE_RESOURCES, "stable"), /language/);
  assert.throws(() => api.captureComputeAttempt(image(), "sage", "x", api.DEFAULT_COMPUTE_RESOURCES, ""), /stable request ID/);
});

test("Unicode and escaped source are bounded by actual encoded JSON request bytes", () => {
  assert.throws(() => api.captureComputeAttempt(image(), "sage", "\u0001".repeat(12000), api.DEFAULT_COMPUTE_RESOURCES, "stable"), /64 KiB/);
  assert.ok(api.captureComputeAttempt(image(), "sage", "😀".repeat(16000), api.DEFAULT_COMPUTE_RESOURCES, "x".repeat(128)));
  assert.ok(api.captureComputeAttempt(image(), "sage", "x".repeat(16000), api.DEFAULT_COMPUTE_RESOURCES, "stable"));
});

test("receipt matching binds requester, organization, topic, identifier, source and every resource", () => {
  const attempt = captured(), job = receipt(attempt);
  assert.ok(api.matchesComputeAttempt(job, attempt, org, topic, user));
  for (const key of ["organization_id", "topic_id", "requested_by", "client_id"]) assert.equal(api.matchesComputeAttempt({ ...job, [key]: "different" }, attempt, org, topic, user), false);
  for (const key of ["image_id", "language", "program"]) assert.equal(api.matchesComputeAttempt({ ...job, parameters: { ...job.parameters, [key]: "different" } }, attempt, org, topic, user), false);
  for (const [key] of api.RESOURCE_FIELDS) assert.equal(api.matchesComputeAttempt({ ...job, parameters: { ...job.parameters, resources: { ...job.parameters.resources, [key]: 999 } } }, attempt, org, topic, user), false);
});

test("lost response and HTTP refusal preserve exact retry body, ID, cookies and CSRF", async () => {
  const oldFetch = globalThis.fetch, oldDocument = Object.getOwnPropertyDescriptor(globalThis, "document");
  Object.defineProperty(globalThis, "document", { configurable: true, value: { cookie: "__Host-reasoning_csrf=csrf" } });
  const attempt = captured(), requests = [];
  globalThis.fetch = async (path, options) => {
    requests.push(options.body);
    assert.equal(path, `/api/organizations/${org}/topics/${topic}/compute-jobs`);
    assert.equal(options.headers["X-CSRF-Token"], "csrf"); assert.equal(options.credentials, "same-origin");
    assert.equal(options.headers.Authorization, undefined);
    if (requests.length === 1) throw new TypeError("lost receipt");
    if (requests.length === 2) return new Response('{"detail":"quota held"}', { status: 409 });
    return new Response(JSON.stringify(receipt(attempt)), { status: 202 });
  };
  try {
    await assert.rejects(api.deliverComputeAttempt(org, topic, user, attempt), /lost receipt/);
    await assert.rejects(api.deliverComputeAttempt(org, topic, user, attempt), /quota held/);
    assert.equal((await api.deliverComputeAttempt(org, topic, user, attempt)).id, "d".repeat(32));
    assert.deepEqual(requests, Array(3).fill(JSON.stringify(attempt)));
  } finally { globalThis.fetch = oldFetch; if (oldDocument) Object.defineProperty(globalThis, "document", oldDocument); else delete globalThis.document; }
});

test("mismatched accepted receipt cannot authorize clearing a pending request", async () => {
  const oldFetch = globalThis.fetch, oldDocument = Object.getOwnPropertyDescriptor(globalThis, "document");
  Object.defineProperty(globalThis, "document", { configurable: true, value: { cookie: "" } });
  const attempt = captured();
  globalThis.fetch = async () => new Response(JSON.stringify({ ...receipt(attempt), topic_id: "different" }), { status: 202 });
  try { await assert.rejects(api.deliverComputeAttempt(org, topic, user, attempt), /does not match/); }
  finally { globalThis.fetch = oldFetch; if (oldDocument) Object.defineProperty(globalThis, "document", oldDocument); else delete globalThis.document; }
});

test("exclusive event cursor ordering rejects duplicate, stale, decreasing and oversized pages", () => {
  const entries = [{ sequence: 3, event: { type: "log", text: "plain output" } }, { sequence: 4, event: { type: "state", status: "succeeded" } }];
  assert.deepEqual(api.appendComputeEvents([{ sequence: 1 }], entries, 1), [{ sequence: 1 }, ...entries]);
  for (const page of [[{ sequence: 1 }], [{ sequence: 0 }], [{ sequence: 3 }, { sequence: 3 }], [{ sequence: 4 }, { sequence: 3 }], [{ sequence: NaN }], Array.from({ length: 201 }, (_, index) => ({ sequence: index + 2 }))]) {
    assert.throws(() => api.appendComputeEvents([], page, 1), /cursor/);
  }
  assert.equal(api.appendComputeEvents(Array.from({ length: 4300 }, (_, index) => ({ sequence: index + 1 })), [{ sequence: 4301 }], 4300).length, 4300);
});

test("gamma compute form separates single jobs from workflows and defaults to disabled submission", () => {
  const markup = renderToStaticMarkup(React.createElement(components.ComputeConversation, { organizationId: org, topicId: topic, userId: user,
    canWrite: true, canManage: false, onAttempt: () => {}, onAuthLost: () => {} }));
  assert.match(markup, /Containerized computations/);
  assert.doesNotMatch(markup, /Latest 50 topic computations|Open computation by ID|Compute program/);
  const readonly = renderToStaticMarkup(React.createElement(components.ComputeConversation, { organizationId: org, topicId: topic, userId: user,
    canWrite: false, canManage: false, onAttempt: () => {}, onAuthLost: () => {} }));
  assert.doesNotMatch(readonly, /Submit computation|Compute program/);
});

test("pending request is visibly immutable with unchanged replay and explicit abandon warning", () => {
  const markup = renderToStaticMarkup(React.createElement(components.ComputeConversation, { organizationId: org, topicId: topic, userId: user,
    canWrite: true, canManage: true, attempt: captured(), onAttempt: () => {}, onAuthLost: () => {} }));
  assert.match(markup, /Unacknowledged request same-request/); assert.match(markup, /Retry unchanged computation/);
  assert.match(markup, /Stop retrying request/); assert.match(markup, /reloading closes this in-memory record/);
  for (const [field, label] of [["image", "Compute image"], ["language", "Program language"], ["program", "Compute program"]]) {
    assert.ok(markup.includes(`<label for="compute-${field}-${topic}">${label}</label>`));
    assert.ok(markup.includes(`id="compute-${field}-${topic}"`));
  }
});

test("unknown and cancellation status do not imply physical termination or quota release", () => {
  const attempt = captured();
  const job = { ...receipt(attempt), image: image(), status: "outcome_unknown", cancel_requested: 1, reserved_cpu_seconds: 120, accounted_at: null };
  assert.ok(api.activeCompute(job));
  const markup = renderToStaticMarkup(React.createElement(components.ComputeJobSummary, { job, canCancel: true, busy: false, onSelect: () => {}, onCancel: () => {} }));
  assert.match(markup, /reservation held/); assert.match(markup, /120 CPU-seconds reserved/); assert.match(markup, /remote termination is not yet confirmed/);
  assert.doesNotMatch(markup, /Request cancellation/);
  for (const status of ["succeeded", "failed", "cancelled"]) assert.equal(api.activeCompute({ status }), false);
});

test("cancellation controls require live write authority and requester or administrator role", () => {
  const job = { requested_by: user, status: "running", cancel_requested: 0 };
  assert.ok(api.canCancelCompute(job, user, true, false));
  assert.ok(api.canCancelCompute(job, "other", true, true));
  assert.equal(api.canCancelCompute(job, "other", true, false), false);
  assert.equal(api.canCancelCompute(job, user, false, false), false);
  assert.equal(api.canCancelCompute({ ...job, cancel_requested: 1 }, user, true, true), false);
  assert.equal(api.canCancelCompute({ ...job, status: "succeeded" }, user, true, true), false);
});

test("image names and error text render as escaped untrusted text", () => {
  const job = { ...receipt(captured()), image: { ...image(), name: "<script>unsafe</script>" }, status: "failed", error_code: "<img src=x>",
    parameters: captured(), accounted_at: 1, used_cpu_seconds: 1 };
  const markup = renderToStaticMarkup(React.createElement(components.ComputeJobSummary, { job, canCancel: false, busy: false, onSelect: () => {}, onCancel: () => {} }));
  assert.match(markup, /&lt;script&gt;/); assert.match(markup, /&lt;img src=x&gt;/); assert.doesNotMatch(markup, /<script>|<img/);
});

test("resource controls expose exact bounded byte units and six independent limits", () => {
  const markup = renderToStaticMarkup(React.createElement(resourceComponent.ComputeResources, { value: api.DEFAULT_COMPUTE_RESOURCES, maximum: api.DEFAULT_COMPUTE_RESOURCES, onChange: () => {} }));
  for (const [, label] of api.RESOURCE_FIELDS) assert.ok(markup.includes(label));
  assert.equal((markup.match(/type="number"/g) ?? []).length, 6); assert.match(markup, /max="120"/); assert.match(markup, /1 GiB = 1073741824/);
});

test("compute administration distinguishes enrollment from hardware and credential secrecy", () => {
  const markup = renderToStaticMarkup(React.createElement(administration.ComputeAdministration, { organizationId: org, onAuthLost: () => {} }));
  assert.match(markup, /Compute administration/); assert.match(markup, /does not provision a machine/); assert.match(markup, /separate Linux\/Docker hosts/);
  assert.doesNotMatch(markup, /token_hash|Authorization: Bearer/);
});

test("a job's pinned input files show their sandbox path, size and checksum", () => {
  const sha = "e".repeat(64);
  const markup = renderToStaticMarkup(React.createElement(components.ComputeInputFiles, { files: [
    { identity: "d".repeat(32), sha256: sha, topic_id: topic, name: "data set.bin", mime: "application/octet-stream",
      size_bytes: 12, path: "01-data_set.bin" }] }));
  assert.match(markup, /Input files/);
  assert.match(markup, /\/work\/in\/01-data_set\.bin · data set\.bin · 12 bytes/);
  assert.match(markup, new RegExp(`SHA-256 ${sha}`));
  assert.equal(renderToStaticMarkup(React.createElement(components.ComputeInputFiles, {})), "");
});
