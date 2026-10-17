/** Schedule the next poll; hidden tabs pause until they are visible again and then refresh immediately. */
export function schedulePoll(callback: () => void, delay: number, signal: AbortSignal): ReturnType<typeof setTimeout> {
  const cleanup = () => {
    clearTimeout(timer);
    document.removeEventListener("visibilitychange", visible);
    signal.removeEventListener("abort", cleanup);
  };
  const run = () => { cleanup(); if (!signal.aborted) callback(); };
  const visible = () => { if (!document.hidden) run(); };
  const timer = document.hidden ? undefined : setTimeout(run, delay);
  if (signal.aborted) cleanup();
  else {
    document.addEventListener("visibilitychange", visible);
    signal.addEventListener("abort", cleanup, { once: true });
  }
  // Callers only pass the result to clearTimeout, which accepts undefined.
  return timer as ReturnType<typeof setTimeout>;
}

/**
 * Poll delay for views whose work can be idle: `base` while work is active and for `grace` quiet polls,
 * then doubling up to `maximum`. Any active observation returns to `base`.
 */
export class PollBackoff {
  private quiet = 0;
  constructor(private readonly base: number, private readonly maximum: number, private readonly grace = 3) {}
  next(active: boolean): number {
    this.quiet = active ? 0 : this.quiet + 1;
    return this.quiet <= this.grace ? this.base : Math.min(this.maximum, this.base * 2 ** (this.quiet - this.grace));
  }
}

/**
 * Poll repeatedly: `poll` returns the next delay, or null to stop. `wake` brings the next poll forward; a wake
 * during a poll runs another as soon as it finishes, so polls never overlap. The first poll runs at once unless
 * `initialDelay` is positive; later polls pause in hidden tabs as with schedulePoll.
 */
export function startPolling(poll: () => Promise<number | null>, signal: AbortSignal, initialDelay = 0): { wake(): void } {
  let running = false, again = false, cancel: (() => void) | undefined;
  const schedule = (delay: number) => {
    cancel?.();
    const local = new AbortController(), forward = () => local.abort();
    signal.addEventListener("abort", forward, { once: true });
    cancel = () => { signal.removeEventListener("abort", forward); local.abort(); };
    schedulePoll(() => { signal.removeEventListener("abort", forward); cancel = undefined; void run(); }, delay, local.signal);
  };
  const run = async () => {
    if (signal.aborted) return;
    running = true; again = false;
    let delay: number | null = null;
    try { delay = await poll(); } finally { running = false; }
    if (!signal.aborted && delay !== null) schedule(again ? 0 : delay);
  };
  if (signal.aborted) { /* Nothing to start. */ }
  else if (initialDelay > 0) schedule(initialDelay);
  else void run();
  return { wake: () => { if (signal.aborted) return; if (running) again = true; else schedule(0); } };
}

/**
 * Opt-in timing marks for live updates. A page (for example a browser test) that sets `window.__reasoningTiming`
 * to an array receives `{ name, at }` entries with `performance.now()` times; otherwise nothing is recorded.
 */
export function recordTiming(name: string): void {
  const sink = (globalThis as { __reasoningTiming?: { name: string; at: number }[] }).__reasoningTiming;
  if (Array.isArray(sink) && sink.length < 10_000) sink.push({ name, at: performance.now() });
}
