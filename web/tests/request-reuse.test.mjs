// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { after, before, test } from "node:test";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";

let server, api, controls;
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL("../", import.meta.url)),
    appType: "custom", logLevel: "silent", server: { middlewareMode: true } });
  api = await server.ssrLoadModule("/src/organization-api.ts");
  controls = await server.ssrLoadModule("/src/ResearchControls.tsx");
});
after(async () => { await server?.close(); });

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

test("the client's read-only mathematics operations are exactly the server's", async () => {
  const source = await readFile(new URL("../../src/reasoning_ledger/server/workbench_tools.py", import.meta.url), "utf8");
  const block = /^READS = \{([^}]*)\}/m.exec(source)?.[1];
  assert.ok(block, "server READS set");
  assert.deepEqual([...api.MATHEMATICS_READS].sort(), [...block.matchAll(/"([a-z_]+)"/g)].map(match => match[1]).sort());
});

test("read-only mathematics operations keep shared catalogs; writes still invalidate", async () => {
  const requests = [];
  await withFetch(async (path, options) => {
    requests.push(options.method + " " + path);
    return new Response(JSON.stringify({ references: [], claims: {} }), { status: 200 });
  }, async () => {
    const catalog = () => api.modelCatalog("o"), benchmarks = () => api.sharedRequest("/organizations/o/model-benchmarks",
      { maxAge: api.CATALOG_MAX_AGE, durable: true });
    const reads = () => requests.filter(item => /model-(prices|benchmarks)$/.test(item)).length;
    await catalog(); await benchmarks(); await api.sharedRequest("/organizations/o/members", { maxAge: 60000 });
    assert.equal(reads(), 2);
    // The Bibliography's reference listing and other read-only operations.
    for (const name of ["ledger_references", "ledger_search_references", "ledger_status", "ledger_read"])
      await api.request("/organizations/o/mathematics", { method: "POST", body: { name, arguments: {} } });
    await catalog(); await benchmarks(); await api.sharedRequest("/organizations/o/members", { maxAge: 60000 });
    assert.equal(reads(), 2, "read-only operations reuse the catalog and benchmarks");
    assert.equal(requests.filter(item => item.endsWith("/members")).length, 1, "and every other shared read");
    // Mathematics writes, unknown operations and other POST routes still invalidate.
    for (const [path, body] of [["/organizations/o/mathematics", { name: "ledger_claim", client_id: "c", arguments: {} }],
      ["/organizations/o/mathematics", { name: "ledger_unknown" }], ["/organizations/o/mathematics", null],
      ["/organizations/o/mathematics/import", { name: "ledger_references" }], ["/organizations/o/policy", { name: "ledger_status" }]]) {
      const before = reads();
      await api.request(path, { method: "POST", body });
      await catalog(); await benchmarks();
      assert.equal(reads(), before + 2, `${path} ${JSON.stringify(body)} invalidates`);
    }
    // Account and access changes still invalidate.
    for (const [path, method] of [["/organizations/o/policy", "PUT"], ["/organizations/o/members/u", "DELETE"], ["/auth/logout", "POST"]]) {
      const before = reads();
      await api.request(path, { method, body: {} });
      await catalog();
      assert.equal(reads(), before + 1, `${method} ${path} invalidates`);
    }
  });
});

test("tool receipt summaries merge by ID, keep five, and hold the cursor at the oldest pending receipt", () => {
  const receipt = (id, created_at, status = "completed") => ({ id, name: "web_search", status, created_at, finished_at: null });
  const empty = { receipts: [], cursor: 0 };
  assert.equal(controls.toolReceiptAfter(empty), 0);
  let state = controls.mergeToolReceipts(empty, [receipt("a", 10, "requested"), receipt("b", 9)]);
  assert.deepEqual(state.receipts.map(item => item.id), ["a", "b"]);
  assert.equal(state.cursor, 10);
  assert.equal(controls.mergeToolReceipts(state, []), state, "an empty delta keeps the state");
  assert.equal(controls.mergeToolReceipts(state, [receipt("a", 10, "requested")]), state, "an unchanged delta keeps the state");
  state = controls.mergeToolReceipts(state, [receipt("c", 20), receipt("d", 21)]);
  assert.equal(state.cursor, 21);
  assert.equal(controls.toolReceiptAfter(state), 10, "the pending receipt's status change is still requested");
  state = controls.mergeToolReceipts(state, [receipt("a", 10)]);
  assert.equal(state.receipts.find(item => item.id === "a").status, "completed");
  assert.equal(controls.toolReceiptAfter(state), 21, "resolved receipts release the cursor");
  state = controls.mergeToolReceipts(state, [receipt("e", 22), receipt("f", 23), receipt("g", 23)]);
  assert.deepEqual(state.receipts.map(item => item.id), ["g", "f", "e", "d", "c"]);
  assert.equal(state.receipts.length, controls.RECENT_TOOL_LIMIT);
});

test("recent topics keep receipt summaries and cursors, bounded", () => {
  const state = index => ({ receipts: [{ id: "r" + index, name: "n", status: "completed", created_at: index }], cursor: index });
  for (let index = 0; index <= 8; index++) controls.rememberToolReceipts("user topic-" + index, state(index));
  assert.equal(controls.readToolReceipts("user topic-0"), undefined, "the least recently viewed topic is dropped");
  assert.deepEqual(controls.readToolReceipts("user topic-8"), state(8));
  controls.rememberToolReceipts("user topic-8", null);
  assert.equal(controls.readToolReceipts("user topic-8"), undefined);
});

test("research controls request receipt summaries from the retained cursor", async () => {
  const source = await readFile(new URL("../src/ResearchControls.tsx", import.meta.url), "utf8");
  assert.match(source, /"\/research-tools\?summary=1&after=" \+ toolReceiptAfter\(toolState\.current\)/);
  assert.doesNotMatch(source, /research-tools\?after=/);
});
