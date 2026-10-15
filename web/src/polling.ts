/** Bound background-tab traffic and refresh immediately when a tab returns. */
export function schedulePoll(callback: () => void, delay: number, signal: AbortSignal): ReturnType<typeof setTimeout> {
  const cleanup = () => {
    clearTimeout(timer);
    document.removeEventListener("visibilitychange", visible);
    signal.removeEventListener("abort", cleanup);
  };
  const run = () => { cleanup(); if (!signal.aborted) callback(); };
  const visible = () => { if (!document.hidden) run(); };
  const timer = setTimeout(run, document.hidden ? Math.max(delay, 30000) : delay);
  if (signal.aborted) cleanup();
  else {
    document.addEventListener("visibilitychange", visible);
    signal.addEventListener("abort", cleanup, { once: true });
  }
  return timer;
}
