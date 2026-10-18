// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";

let server, events;
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL("../", import.meta.url)),
    appType: "custom", logLevel: "silent", server: { middlewareMode: true } });
  events = await server.ssrLoadModule("/src/topicEvents.ts");
});
after(async () => { await server?.close(); });

const settle = async (turns = 20) => { for (let index = 0; index < turns; index++) await new Promise(resolve => setImmediate(resolve)); };
const run = (id, output_text, extra = {}) => ({ id, status: "running", output_text, ...extra });

/** A fetch that answers each topic stream request with a controllable event stream, or with a refusal. */
function streams(refusals = []) {
  const opened = [];
  const fetch = async (path, options) => {
    const refusal = refusals.shift();
    if (refusal) return new Response(JSON.stringify({ detail: "refused" }), { status: refusal });
    let controller;
    const body = new ReadableStream({ start(value) { controller = value; } });
    const encoder = new TextEncoder();
    const stream = { path, options, aborted: false,
      send: (name, data) => controller.enqueue(encoder.encode(`event: ${name}\ndata: ${JSON.stringify(data)}\n\n`)),
      raw: text => controller.enqueue(encoder.encode(text)),
      close: () => controller.close() };
    options.signal.addEventListener("abort", () => { stream.aborted = true; try { controller.error(new DOMException("aborted", "AbortError")); } catch { /* closed */ } });
    opened.push(stream);
    return new Response(body, { headers: { "content-type": "text/event-stream" } });
  };
  return { fetch, opened };
}

async function withFetch(fetch, work) {
  const previous = globalThis.fetch;
  globalThis.fetch = fetch;
  try { await work(); } finally { globalThis.fetch = previous; }
}

function recorder() {
  const seen = [];
  const listener = Object.fromEntries(["status", "messages", "runs", "agents", "goal", "bots"].map(name => [name, value => seen.push([name, value])]));
  return { seen, listener, last: name => seen.filter(entry => entry[0] === name).at(-1)?.[1] };
}

test("the event parser handles split chunks, CRLF, comments and multi-line data", () => {
  const seen = [];
  const parser = new events.EventParser((name, data) => seen.push([name, data]));
  parser.push("event: runs\r\ndata: {\"a\":");
  parser.push("1}\r\n\r\n: keepalive\n\nevent: goal\ndata: line one\ndata: line two\n");
  assert.deepEqual(seen, [["runs", '{"a":1}']]);
  parser.push("\ndata: plain\n\n");
  assert.deepEqual(seen, [["runs", '{"a":1}'], ["goal", "line one\nline two"], ["message", "plain"]]);
});

test("run patches append output, keep unchanged runs and drop runs that leave the list", () => {
  const known = new Map();
  const first = events.applyRunPatch(known, { order: ["a", "b"], runs: [run("a", "One"), run("b", "Done", { status: "succeeded" })] });
  const second = events.applyRunPatch(known, { order: ["a", "b"], runs: [{ id: "a", status: "running", output_append: " two" }] });
  assert.equal(second[0].output_text, "One two");
  assert.equal(second[1], first[1], "an unchanged run keeps its identity, so it does not re-render");
  assert.deepEqual(events.applyRunPatch(known, { order: ["b"], runs: [] }).map(item => item.id), ["b"]);
  assert.throws(() => events.applyRunPatch(known, { order: ["a"], runs: [{ id: "a", output_append: "x" }] }), /unknown run/);
  assert.throws(() => events.applyRunPatch(new Map(), { order: ["z"], runs: [] }), /unknown run/);
});

test("a shared stream goes live, replays the latest state and reconnects after a scheduled end", async () => {
  const { fetch, opened } = streams();
  await withFetch(fetch, async () => {
    const first = recorder(), second = recorder();
    const stop = events.subscribeTopicEvents("o", "a".repeat(32), first.listener);
    await settle();
    assert.equal(opened.length, 1);
    assert.equal(opened[0].path, `/api/organizations/o/topics/${"a".repeat(32)}/events`);
    assert.equal(opened[0].options.headers.Accept, "text/event-stream");
    opened[0].send("ready", { heartbeat_ms: 15000 });
    opened[0].send("messages", { sequence: 4, updates: 1 });
    opened[0].send("runs", { order: ["r"], runs: [run("r", "Hel")] });
    opened[0].send("runs", { order: ["r"], runs: [{ id: "r", status: "running", output_append: "lo" }] });
    await settle();
    assert.deepEqual(first.seen.map(entry => entry[0]), ["status", "status", "messages", "runs", "runs"]);
    assert.equal(first.last("status"), "live");
    assert.equal(first.last("runs")[0].output_text, "Hello");
    // A second view of the same topic shares the connection and receives the current state at once.
    const stopSecond = events.subscribeTopicEvents("o", "a".repeat(32), second.listener);
    assert.equal(opened.length, 1);
    assert.deepEqual(second.seen.map(entry => entry[0]), ["status", "messages", "runs"]);
    assert.equal(second.last("runs")[0].output_text, "Hello");
    // Lifetime ends reconnect quickly without leaving the live state; the new connection starts from a full snapshot.
    opened[0].send("end", { reason: "lifetime" });
    await settle();
    await new Promise(resolve => setTimeout(resolve, 1100));
    await settle();
    assert.equal(opened.length, 2);
    assert.ok(!first.seen.some(entry => entry[0] === "status" && entry[1] === "unavailable"));
    opened[1].send("ready", {});
    opened[1].send("runs", { order: ["r"], runs: [run("r", "Hello, world")] });
    await settle();
    assert.equal(first.last("runs")[0].output_text, "Hello, world");
    // An append that does not match this connection's state is a protocol error: the stream is dropped.
    opened[1].send("runs", { order: ["x"], runs: [{ id: "x", output_append: "?" }] });
    await settle();
    assert.equal(first.last("status"), "unavailable", "views fall back to polling");
    stop(); stopSecond();
    await new Promise(resolve => setTimeout(resolve, 2100));
    await settle();
    assert.equal(opened.at(-1).aborted, true, "the last view's departure closes the stream");
    const count = opened.length;
    await new Promise(resolve => setTimeout(resolve, 1500));
    assert.equal(opened.length, count, "a closed stream does not reconnect");
  });
});

test("refused and failing streams fall back to polling and retry with backoff", async context => {
  context.mock.timers.enable({ apis: ["setTimeout"] });
  const { fetch, opened } = streams([503]);
  try {
    await withFetch(fetch, async () => {
      const view = recorder();
      const stop = events.subscribeTopicEvents("o", "b".repeat(32), view.listener);
      await settle();
      assert.equal(view.last("status"), "unavailable", "a disabled stream leaves polling in place");
      assert.equal(opened.length, 0);
      context.mock.timers.tick(59_000); await settle();
      assert.equal(opened.length, 0, "a refusal is retried only after a minute");
      context.mock.timers.tick(1_000); await settle();
      assert.equal(opened.length, 1);
      // No ready event within ten seconds: the connection is abandoned and retried.
      context.mock.timers.tick(10_000); await settle();
      assert.equal(opened[0].aborted, true);
      context.mock.timers.tick(1_999); await settle();
      assert.equal(opened.length, 1, "the second failure waits two seconds");
      context.mock.timers.tick(1); await settle();
      assert.equal(opened.length, 2);
      opened[1].send("ready", { heartbeat_ms: 1000 });
      await settle();
      assert.equal(view.last("status"), "live");
      // Silence for three heartbeats means a dead connection.
      context.mock.timers.tick(2_900); await settle();
      assert.equal(opened[1].aborted, false);
      opened[1].raw(": keepalive\n\n"); await settle();
      context.mock.timers.tick(2_900); await settle();
      assert.equal(opened[1].aborted, false, "heartbeat comments keep the connection");
      context.mock.timers.tick(200); await settle();
      assert.equal(opened[1].aborted, true);
      assert.equal(view.last("status"), "unavailable");
      // Unsubscribing every view closes the stream after a short grace period, and no retry follows.
      stop();
      context.mock.timers.tick(120_000); await settle();
      assert.equal(opened.length, 2);
    });
  } finally { context.mock.timers.reset(); }
});

test("an access or session end stops streaming until polling has had a chance to report it", async context => {
  context.mock.timers.enable({ apis: ["setTimeout"] });
  const { fetch, opened } = streams();
  try {
    await withFetch(fetch, async () => {
      const view = recorder();
      const stop = events.subscribeTopicEvents("o", "c".repeat(32), view.listener);
      await settle();
      opened[0].send("ready", {});
      opened[0].send("end", { reason: "access" });
      await settle();
      assert.equal(view.last("status"), "unavailable");
      context.mock.timers.tick(30_000); await settle();
      assert.equal(opened.length, 1);
      stop();
    });
  } finally { context.mock.timers.reset(); }
});
