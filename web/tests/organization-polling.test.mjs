import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";

let server, schedulePoll;
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL("../", import.meta.url)),
    appType: "custom", logLevel: "silent", server: { middlewareMode: true } });
  ({ schedulePoll } = await server.ssrLoadModule("/src/polling.ts"));
});
after(async () => { await server?.close(); });

test("polling bounds background traffic, resumes on visibility, and cleans up on abort", context => {
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
    context.mock.timers.tick(1000);
    assert.equal(calls, 1, "hidden tabs must not keep their one-second cadence");
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
