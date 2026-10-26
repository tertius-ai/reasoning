// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";

let server, release;
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL("../", import.meta.url)),
    appType: "custom", logLevel: "silent", server: { middlewareMode: true } });
  release = await server.ssrLoadModule("/src/release.ts");
});
after(async () => { await server?.close(); });

const loaded = "a".repeat(64), newer = "b".repeat(64), newest = "c".repeat(64);
const page = content => ({ querySelector: selector => selector === 'meta[name="reasoning-release"]' && content !== undefined
  ? { getAttribute: name => name === "content" ? content : null } : null });

test("only a complete release digest counts as a known release", () => {
  assert.equal(release.validRelease(loaded), loaded);
  for (const value of [null, undefined, "", "A".repeat(64), "a".repeat(40), loaded + "\n", 123]) assert.equal(release.validRelease(value), null);
  assert.equal(release.pageRelease(page(loaded)), loaded);
  assert.equal(release.pageRelease(page("development")), null);
  assert.equal(release.pageRelease(page(undefined)), null);
  assert.equal(release.pageRelease(undefined), null, "server-side rendering has no page release");
});

test("a different reported release is offered, and returning to the page's release withdraws it", () => {
  const watch = new release.ReleaseWatch(loaded);
  let notified = 0;
  const unsubscribe = watch.subscribe(() => { notified += 1; });
  watch.observe(loaded);
  assert.equal(watch.available, null, "the page's own release is current");
  watch.observe(null); watch.observe("not-a-release");
  assert.equal(watch.available, null);
  assert.equal(notified, 0);
  watch.observe(newer);
  assert.equal(watch.available, newer);
  watch.observe(newer); watch.observe(null);
  assert.equal(watch.available, newer, "repeats and unknown values change nothing");
  assert.equal(notified, 1);
  watch.observe(newest);
  assert.equal(watch.available, newest, "a further upgrade is offered");
  watch.observe(loaded);
  assert.equal(watch.available, null, "returning to the page's release withdraws the offer");
  unsubscribe();
  watch.observe(newer);
  assert.equal(notified, 3);
});

test("a page without a known release never offers a reload", () => {
  const watch = new release.ReleaseWatch(null);
  watch.observe(newer);
  assert.equal(watch.available, null);
});

test("responses report through the shared watch", () => {
  // Server-side rendering has no page meta, so the shared watch behaves as a development page.
  assert.equal(release.releaseWatch.loaded, null);
  release.observeRelease(new Response(null, { headers: { [release.RELEASE_HEADER]: newer } }));
  assert.equal(release.releaseWatch.available, null);
});
