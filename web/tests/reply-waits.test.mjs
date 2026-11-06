import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

let server, waits;
before(async () => {
  server = await createServer({ appType: "custom", logLevel: "silent", server: { middlewareMode: true } });
  waits = await server.ssrLoadModule("/src/ReplyWaits.tsx");
});
after(async () => { await server?.close(); });

const proposal = { kind: "proposal", id: "p1", status: "pending", action: "compute", compute: {
  image_id: "texlive", language: "python", resources: { cpu: 1, wall_time_seconds: 120 }, program: "print(2+2)", program_truncated: false } };
const run = (answer, extra = {}) => ({ id: "r1", status: "succeeded", model: "anthropic/claude-fable-5", message_id: "m1",
  agent: { role: "coordinator", root_run: "r1", thread_status: "active" }, answer_state: answer, ...extra });
const render = (value, canWrite = true) => renderToStaticMarkup(React.createElement(waits.ReplyWaits, {
  organizationId: "o1", topicId: "t1", run: value, canWrite, online: true, onUpdated: () => {}, onAuthLost: () => {} }));

test("a reply waits only while something it needs is pending and its thread is not stopped", () => {
  assert.equal(waits.waitingReply(run({ status: "waiting", waits: [proposal] })), true);
  assert.equal(waits.waitingReply(run({ status: "waiting", waits: [] })), false);
  assert.equal(waits.waitingReply(run({ status: "continued", waits: [proposal] })), false);
  assert.equal(waits.waitingReply(run({ status: "waiting", waits: [proposal] }, { status: "running" })), false);
  assert.equal(waits.waitingReply(run({ status: "waiting", waits: [proposal] },
    { agent: { role: "coordinator", root_run: "r1", thread_status: "cancelled" } })), false);
});

test("the card names what holds the reply, approval first", () => {
  assert.equal(waits.waitingLabel([proposal, { kind: "agents", count: 2 }]), "Waiting for approval");
  assert.equal(waits.waitingLabel([{ kind: "compute", id: "j1", status: "queued", created_at: 1 }]), "Waiting for a computation");
  assert.equal(waits.waitingLabel([{ kind: "agents", count: 1 }]), "Waiting for delegated work");
  assert.equal(waits.waitingLabel([{ kind: "review", id: "v1", status: "pending" }]), "Waiting for a review");
});

test("a proposed computation offers approval only when something can run it", () => {
  const runnable = render(run({ status: "waiting", waits: [proposal], compute_available: true }));
  assert.match(runnable, /Approve this computation\?/);
  assert.match(runnable, /texlive · python · 1 CPU · up to 2 min/);
  assert.match(runnable, /print\(2\+2\)/);
  assert.match(runnable, /Approve and run/);
  assert.match(runnable, /Reject and continue/);
  const unavailable = render(run({ status: "waiting", waits: [proposal], compute_available: false }));
  assert.doesNotMatch(unavailable, /Approve and run/);
  assert.match(unavailable, /can(&#x27;|')t run computations/);
  assert.match(unavailable, /Reject and continue/);
  assert.doesNotMatch(render(run({ status: "waiting", waits: [proposal], compute_available: true }), false), /<button/);
});

test("a queued computation says whether anything can run it and can be cancelled", () => {
  const job = { kind: "compute", id: "j1", status: "queued", created_at: Math.floor(Date.now() / 1000) - 600 };
  const queued = render(run({ status: "waiting", waits: [job], compute_available: true }));
  assert.match(queued, /Queued · waiting for a compute worker/);
  assert.match(queued, /Waiting 10 min/);
  assert.match(queued, /Cancel and continue/);
  assert.match(render(run({ status: "waiting", waits: [job], compute_available: false })), /Queued, but nothing can run it/);
});

test("a computation no worker accepts says so instead of offering approval or waiting silently", () => {
  const large = render(run({ status: "waiting", waits: [{ ...proposal, runnable: false }], compute_available: true }));
  assert.doesNotMatch(large, /Approve and run/);
  assert.match(large, /accepts a job this large/);
  assert.match(large, /Reject and continue/);
  const job = { kind: "compute", id: "j1", status: "queued", created_at: Math.floor(Date.now() / 1000), runnable: false };
  const queued = render(run({ status: "waiting", waits: [job], compute_available: true }));
  assert.match(queued, /Queued, but too large for any worker/);
  assert.match(queued, /Cancel and continue/);
  assert.doesNotMatch(render(run({ status: "waiting", waits: [{ ...job, status: "running" }], compute_available: true })),
    /too large/);
});
