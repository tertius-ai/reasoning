// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { createServer } from "vite";

let server, diagnostics;
before(async () => {
  server = await createServer({ appType: "custom", logLevel: "silent", server: { middlewareMode: true } });
  diagnostics = await server.ssrLoadModule("/src/diagnostics.ts");
});
after(async () => { await server?.close(); });

test("report paths drop query strings and never name a direct conversation", () => {
  assert.equal(diagnostics.reportPath("/api/organizations/o/search?q=private+words"), "/api/organizations/o/search");
  assert.equal(diagnostics.reportPath("/api/organizations/o/direct-conversations/c1/messages?after=4"), "/api/organizations/o/direct-conversations/…/messages");
  assert.equal(diagnostics.reportPath("/api/organizations/o/topics/t/media/m/chunks/3"), "/api/organizations/o/topics/t/media/m/chunks/3");
});

test("only the latest failed requests and page errors are kept, bounded in length", () => {
  for (let index = 0; index < diagnostics.MAX_FAILED_REQUESTS + 3; index++)
    diagnostics.recordFailedRequest("PUT", `/organizations/o/topics/t/media/m/chunks/${index}?x=1`, 409, "x".repeat(500));
  const requests = diagnostics.recentFailedRequests();
  assert.equal(requests.length, diagnostics.MAX_FAILED_REQUESTS);
  assert.equal(requests.at(-1).path, `/api/organizations/o/topics/t/media/m/chunks/${diagnostics.MAX_FAILED_REQUESTS + 2}`);
  assert.equal(requests[0].detail.length, 300);
  for (let index = 0; index < diagnostics.MAX_PAGE_ERRORS + 2; index++) diagnostics.recordPageError(new TypeError("broken " + index));
  const errors = diagnostics.recentPageErrors();
  assert.equal(errors.length, diagnostics.MAX_PAGE_ERRORS);
  assert.equal(errors.at(-1).message, `TypeError: broken ${diagnostics.MAX_PAGE_ERRORS + 1}`);
  assert.ok(errors.at(-1).stack.split("\n").length <= 4);
});
