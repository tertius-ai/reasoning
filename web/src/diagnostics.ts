// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0

/**
 * The last failed requests and page errors in this tab, kept in memory only, for a bug report its author chooses to
 * attach. Nothing here is sent anywhere on its own. Paths keep their identifiers but lose query strings (search terms
 * travel there), a direct conversation's identifier is masked, and no request or response body is recorded.
 */
export type FailedRequest = { at: number; method: string; path: string; status: number; detail: string };
export type PageError = { at: number; message: string; stack: string };

export const MAX_FAILED_REQUESTS = 10, MAX_PAGE_ERRORS = 5;
const failedRequests: FailedRequest[] = [], pageErrors: PageError[] = [];
const now = () => Math.floor(Date.now() / 1000);
const keep = <T>(list: T[], item: T, limit: number) => { list.push(item); if (list.length > limit) list.splice(0, list.length - limit); };

/** A request path without its query, with any direct conversation reduced to "…" so a report never names it. */
export const reportPath = (path: string) => path.split(/[?#]/)[0].replace(/(\/direct-conversations\/)[^/]+/g, "$1…").slice(0, 300);

export function recordFailedRequest(method: string, path: string, status: number, detail: string) {
  keep(failedRequests, { at: now(), method: method.slice(0, 10), path: reportPath("/api" + path), status, detail: detail.slice(0, 300) }, MAX_FAILED_REQUESTS);
}

export function recordPageError(error: unknown, fallback = "") {
  const message = error instanceof Error ? `${error.name}: ${error.message}` : String(error ?? fallback);
  // The first frames locate the fault in the release's bundle; deeper frames are framework internals.
  const stack = error instanceof Error && error.stack ? error.stack.split("\n").slice(1, 5).map(line => line.trim()).join("\n") : "";
  keep(pageErrors, { at: now(), message: message.slice(0, 300), stack: stack.slice(0, 800) }, MAX_PAGE_ERRORS);
}

export const recentFailedRequests = (): FailedRequest[] => failedRequests.map(item => ({ ...item }));
export const recentPageErrors = (): PageError[] => pageErrors.map(item => ({ ...item }));

if (typeof window !== "undefined") {
  window.addEventListener("error", event => recordPageError(event.error, event.message));
  window.addEventListener("unhandledrejection", event => recordPageError(event.reason, "Unhandled promise rejection"));
}
