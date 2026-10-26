// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0

/** Response header naming the release of the server that answered (`--deployment-release`). */
export const RELEASE_HEADER = "X-Reasoning-Release";
const RELEASE = /^[a-f0-9]{64}$/;

/** A complete release manifest digest, or null for anything else (absent, development, malformed). */
export const validRelease = (value: unknown): string | null => typeof value === "string" && RELEASE.test(value) ? value : null;

/** The release that served this page, embedded by the server as `<meta name="reasoning-release">`; null when unknown. */
export function pageRelease(document: Pick<Document, "querySelector"> | undefined = globalThis.document): string | null {
  return validRelease(document?.querySelector('meta[name="reasoning-release"]')?.getAttribute("content"));
}

/**
 * Tracks the release reported by responses the page already receives. A notice is due when a reported release
 * differs from the page's own; a page or response without a known release never produces one.
 */
export class ReleaseWatch {
  private reported: string | null = null;
  private readonly listeners = new Set<() => void>();
  constructor(readonly loaded: string | null) {}

  /** Record one response's header value; an unknown value (for example a proxy error page) changes nothing. */
  observe(value: string | null | undefined): void {
    const release = validRelease(value);
    if (!this.loaded || !release) return;
    const reported = release === this.loaded ? null : release;
    if (reported !== this.reported) { this.reported = reported; this.notify(); }
  }

  /** The newer release the server reports, if any. */
  get available(): string | null { return this.reported; }

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  };

  private notify() { for (const listener of [...this.listeners]) listener(); }
}

export const releaseWatch = new ReleaseWatch(pageRelease());

/** Record the release header of a response from this server. */
export const observeRelease = (response: Pick<Response, "headers">): void => releaseWatch.observe(response.headers.get(RELEASE_HEADER));
