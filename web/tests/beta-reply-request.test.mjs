import assert from "node:assert/strict";
import { after, afterEach, before, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

let server, api, beta;
before(async () => {
  server = await createServer({ appType: "custom", logLevel: "silent", server: { middlewareMode: true } });
  api = await server.ssrLoadModule("/src/organization-api.ts");
  beta = await server.ssrLoadModule("/src/BetaConversation.tsx");
});
after(async () => { await server?.close(); });

const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; delete globalThis.document; });

// Records every submitted body and answers with the scripted outcomes in order.
function serverStub(outcomes) {
  const bodies = [];
  globalThis.document = { cookie: "" };
  globalThis.fetch = async (url, options) => {
    bodies.push({ url, body: JSON.parse(options.body) });
    const outcome = outcomes.shift();
    if (outcome instanceof Error) throw outcome;
    return new Response(JSON.stringify(outcome.body), { status: outcome.status, headers: { "Content-Type": "application/json" } });
  };
  return bodies;
}

const attempt = () => ({
  client_id: "stable-client", models: ["moonshotai/kimi-k3"], max_completion_tokens: null,
  context_message_limit: 50, through_sequence: 338, effort: "high",
});

test("a lost submission response keeps the request for retry with a sanitized reason", async () => {
  const bodies = serverStub([new TypeError("Failed to fetch")]);
  const reported = [];
  await assert.rejects(beta.requestModelReply("org", "topic", attempt(), (value) => reported.push(value)));
  assert.equal(bodies.length, 1);
  assert.match(bodies[0].url, /\/api\/organizations\/org\/topics\/topic\/inference-runs$/);
  assert.deepEqual(bodies[0].body, attempt());
  assert.deepEqual(reported, [{ ...attempt(), unconfirmed: { reason: "The connection failed before the server answered." } }]);
});

test("retry replays the identical request and clears once the server answers", async () => {
  const bodies = serverStub([{ status: 202, body: [{ id: "run" }] }]);
  const reported = [];
  const pending = { ...attempt(), unconfirmed: { reason: "The connection failed before the server answered." } };
  await beta.requestModelReply("org", "topic", pending, (value) => reported.push(value));
  // Same client ID, model, effort, and context cutoff; no browser-only fields.
  assert.deepEqual(bodies.map((entry) => entry.body), [attempt()]);
  assert.deepEqual(reported, [undefined]);
});

test("only a definitive rejection drops the pending retry", async () => {
  for (const [status, detail, kept] of [
    [409, "client_id was already used for a different model request", false],
    [401, "sign-in required", false],
    [408, "timeout", true],
    [504, "Gateway timeout after acceptance", true],
    [502, null, true],
  ]) {
    serverStub([{ status, body: detail ? { detail } : "<html>bad gateway</html>" }]);
    const reported = [];
    await assert.rejects(beta.requestModelReply("org", "topic", attempt(), (value) => reported.push(value)));
    assert.equal(reported.length, 1);
    assert.equal(reported[0] !== undefined, kept, `HTTP ${status}`);
    assert.equal(beta.definitiveRejection(new api.ApiError(status, "")), !kept, `HTTP ${status}`);
  }
});

test("reasons name the transport failure without inventing details", () => {
  assert.equal(beta.unconfirmedReason(new api.ApiError(408, "timeout")), "The server timed out (HTTP 408) before confirming it.");
  assert.equal(beta.unconfirmedReason(new api.ApiError(504, "Gateway timeout after acceptance")),
    "The server reported an error (HTTP 504: Gateway timeout after acceptance) before confirming it.");
  // A proxy error page has no detail; do not show the generic form-validation text.
  assert.equal(beta.unconfirmedReason(new api.ApiError(502, api.UNDESCRIBED_ERROR)),
    "The server reported an error (HTTP 502) before confirming it.");
});

const render = (props) => renderToStaticMarkup(React.createElement(beta.PendingReplyRequest, {
  busy: false, retrying: false, retryBlocked: "", onRetry: () => {}, onDismiss: () => {}, ...props,
}));

test("an in-flight submission is a status, not a recovery prompt", () => {
  const markup = render({ attempt: attempt() });
  assert.match(markup, /role="status"/);
  assert.match(markup, /Sending the reply request to moonshotai\/kimi-k3…/);
  assert.doesNotMatch(markup, /not confirmed|Retry|Dismiss|needs attention/);
});

test("an unconfirmed request explains itself and offers retry beside dismissal", () => {
  const reason = "The connection failed before the server answered.";
  const markup = render({ attempt: { ...attempt(), unconfirmed: { reason } } });
  assert.match(markup, /Reply request not confirmed/);
  assert.match(markup, /Your message was saved, but the server did not confirm the reply request to moonshotai\/kimi-k3, so it may or may not\s+have started\./);
  assert.ok(markup.includes(reason));
  assert.match(markup, /Conversation context ends at message #338 · effort high/);
  assert.match(markup, /it does not cancel an accepted reply or its charges/);
  assert.match(markup, /<button type="button">Retry same request<\/button><button type="button">Dismiss pending retry<\/button>/);
  assert.doesNotMatch(markup, /Abandon|context through|needs attention/);
});

test("retry shows its in-flight, connectivity, and permission states", () => {
  const pending = { ...attempt(), unconfirmed: { reason: "x" } };
  assert.match(render({ attempt: pending, busy: true, retrying: true }),
    /<button type="button" disabled="">Retrying…<\/button><button type="button" disabled="">Dismiss pending retry<\/button>/);
  for (const blocked of ["Retry is available when this browser is back online.", "Retry needs write access to this topic."]) {
    const markup = render({ attempt: pending, retryBlocked: blocked });
    assert.match(markup, /<button type="button" disabled="">Retry same request<\/button><button type="button">Dismiss pending retry<\/button>/);
    assert.ok(markup.includes(blocked));
  }
});
