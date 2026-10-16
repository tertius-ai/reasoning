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
