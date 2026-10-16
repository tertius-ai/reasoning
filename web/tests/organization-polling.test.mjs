import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { after, before, test } from "node:test";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";

let server, schedulePoll, PollBackoff, api;
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL("../", import.meta.url)),
    appType: "custom", logLevel: "silent", server: { middlewareMode: true } });
  ({ schedulePoll, PollBackoff } = await server.ssrLoadModule("/src/polling.ts"));
  api = await server.ssrLoadModule("/src/organization-api.ts");
});
after(async () => { await server?.close(); });

const source = (path) => readFile(new URL(`../src/${path}`, import.meta.url), "utf8");

test("polling pauses hidden tabs, resumes on visibility, and cleans up on abort", context => {
  const previous = globalThis.document;
  const document = new EventTarget();
  document.hidden = false;
  globalThis.document = document;
  context.mock.timers.enable({ apis: ["setTimeout"] });
  try {
    let calls = 0;
    const controller = new AbortController();
    schedulePoll(() => calls++, 1000, controller.signal);
    context.mock.timers.tick(1000);
    assert.equal(calls, 1);
    document.hidden = true;
    schedulePoll(() => calls++, 1000, controller.signal);
    context.mock.timers.tick(3600000);
    assert.equal(calls, 1, "hidden tabs make no requests");
    document.hidden = false;
    document.dispatchEvent(new Event("visibilitychange"));
    assert.equal(calls, 2, "returning to a tab refreshes immediately");
    context.mock.timers.tick(30000);
    assert.equal(calls, 2, "visibility refresh cancels the old timer");
    schedulePoll(() => calls++, 1000, controller.signal);
    controller.abort();
    context.mock.timers.tick(30000);
    document.dispatchEvent(new Event("visibilitychange"));
    assert.equal(calls, 2, "unmounted views cannot keep polling");
    schedulePoll(() => calls++, 1000, controller.signal);
    context.mock.timers.tick(30000);
    assert.equal(calls, 2, "already-aborted views cannot schedule work");
  } finally {
    if (previous === undefined) delete globalThis.document;
    else globalThis.document = previous;
    context.mock.timers.reset();
  }
});

test("idle views back off after a grace period and return to the active cadence", () => {
  const backoff = new PollBackoff(1000, 5000);
  assert.deepEqual([false, false, false, false, false, false, false].map(active => backoff.next(active)),
    [1000, 1000, 1000, 2000, 4000, 5000, 5000]);
  assert.equal(backoff.next(true), 1000);
  assert.equal(backoff.next(false), 1000);
});

async function withFetch(handler, work) {
  const previousFetch = globalThis.fetch;
  const previousDocument = Object.getOwnPropertyDescriptor(globalThis, "document");
  Object.defineProperty(globalThis, "document", { configurable: true, value: { cookie: "" } });
  globalThis.fetch = handler;
  api.clearSharedReads();
  try { await work(); }
  finally {
    api.clearSharedReads();
    globalThis.fetch = previousFetch;
    if (previousDocument) Object.defineProperty(globalThis, "document", previousDocument); else delete globalThis.document;
  }
}

test("shared reads deduplicate concurrent views and reuse only recent results", async context => {
  const requests = [];
  await withFetch(async (path, options) => {
    requests.push([options.method, path]);
    return new Response(JSON.stringify({ path, count: requests.length }), { status: 200 });
  }, async () => {
    context.mock.timers.enable({ apis: ["Date"], now: 1000 });
    const [first, second] = await Promise.all([
      api.sharedRequest("/organizations/o/topics/t/goal", { maxAge: 500 }),
      api.sharedRequest("/organizations/o/topics/t/goal", { maxAge: 2500 }),
    ]);
    assert.deepEqual(first, second);
    assert.equal(requests.length, 1, "two mounted views share one goal request");
    context.mock.timers.tick(1000);
    await api.sharedRequest("/organizations/o/topics/t/goal", { maxAge: 2500 });
    assert.equal(requests.length, 1, "a view with a longer interval reuses a recent result");
    await api.sharedRequest("/organizations/o/topics/t/goal", { maxAge: 500 });
    assert.equal(requests.length, 2, "an older result is fetched again");
    await api.sharedRequest("/organizations/o/model-prices", { maxAge: 300000, durable: true });
    await api.request("/organizations/o/topics/t/read", { method: "PUT", body: { sequence: 1 } });
    await api.sharedRequest("/organizations/o/model-prices", { maxAge: 300000, durable: true });
    await api.sharedRequest("/organizations/o/topics/t/goal", { maxAge: 2500 });
    assert.deepEqual(requests.slice(2).map(item => item[1]), ["/api/organizations/o/model-prices", "/api/organizations/o/topics/t/read",
      "/api/organizations/o/topics/t/goal"], "topic writes keep catalogs but invalidate other shared reads");
    await api.request("/organizations/o/policy", { method: "PUT", body: {} });
    await api.sharedRequest("/organizations/o/model-prices", { maxAge: 300000, durable: true });
    assert.equal(requests.at(-1)[1], "/api/organizations/o/model-prices", "organization writes invalidate catalogs");
    const controller = new AbortController();
    const pending = api.sharedRequest("/organizations/o/hierarchy", { maxAge: 1000, signal: controller.signal });
    controller.abort();
    await assert.rejects(pending, error => api.aborted(error));
    api.clearSharedReads();
    const count = requests.length;
    await api.sharedRequest("/organizations/o/model-prices", { maxAge: 300000, durable: true });
    assert.equal(requests.length, count + 1, "signing out forgets every shared read");
  });
  context.mock.timers.reset();
});

test("failed shared reads are not reused", async () => {
  let calls = 0;
  await withFetch(async () => {
    calls += 1;
    return calls === 1 ? new Response('{"detail":"unavailable"}', { status: 503 }) : new Response("[]", { status: 200 });
  }, async () => {
    await assert.rejects(api.sharedRequest("/organizations/o/members", { maxAge: 60000 }), /unavailable/);
    assert.deepEqual(await api.sharedRequest("/organizations/o/members", { maxAge: 60000 }), []);
    assert.equal(calls, 2);
  });
});

test("startup lists organizations once and idle views do not poll unused configuration", async () => {
  const [app, compute] = await Promise.all([source("OrganizationApp.tsx"), source("ComputeConversation.tsx")]);
  assert.equal(app.match(/request<Organization\[\]>\("\/organizations"/g)?.length ?? 0, 0, "organization lists use the shared read");
  assert.match(app, /lists \? sharedRequest<Organization\[\]>\("\/organizations"/);
  assert.doesNotMatch(compute.slice(compute.indexOf("export function ComputeConversation")), /Promise\.all\(\[\s*request<ComputeConfiguration>/);
  assert.match(compute, /if \(!editorOpen\) return;/);
});

test("sign-in loads only the shell; the signed-in application and rare views load on demand", async () => {
  const [main, shell, app] = await Promise.all([source("organization-main.tsx"), source("OrganizationShell.tsx"), source("OrganizationApp.tsx")]);
  assert.match(main, /import \{ OrganizationShell \} from "\.\/OrganizationShell"/);
  assert.doesNotMatch(main, /OrganizationApp/);
  assert.doesNotMatch(shell, /^import[^\n]*from "\.\/OrganizationApp"/m, "the shell imports the application only dynamically");
  assert.match(shell, /import\("\.\/OrganizationApp"\)/);
  assert.doesNotMatch(shell, /react-markdown|katex/);
  for (const view of ["KnowledgeWorkspace", "KnowledgeBrowser", "BotAdministration", "ComputeAdministration", "OrganizationUsage", "HelpPage"]) {
    assert.doesNotMatch(app, new RegExp(`^import[^\\n]*from "\\./${view}"`, "m"), view);
    assert.match(app, new RegExp(`import\\("\\./${view}"\\)`), view);
  }
});

test("recent topic snapshots are bounded and forgotten on sign-out", () => {
  api.clearSharedReads();
  const snapshot = index => ({ messages: [{ id: "m" + index, sequence: 1 }], cursor: 1, editCursor: 0, older: false });
  for (let index = 0; index <= api.TOPIC_SNAPSHOT_LIMIT; index++) api.rememberTopicSnapshot("topic-" + index, snapshot(index));
  assert.equal(api.readTopicSnapshot("topic-0"), undefined, "the least recently viewed topic is dropped");
  assert.equal(api.readTopicSnapshot("topic-1").messages[0].id, "m1");
  api.rememberTopicSnapshot("topic-1", null);
  assert.equal(api.readTopicSnapshot("topic-1"), undefined);
  api.clearSharedReads();
  assert.equal(api.readTopicSnapshot("topic-2"), undefined);
});
