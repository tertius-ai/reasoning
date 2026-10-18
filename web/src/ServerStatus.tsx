// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import { useCallback, useEffect, useRef, useState } from "react";
import { aborted, ApiError, request } from "./organization-api";

/** The public source repository, as in pyproject.toml's `Repository` URL. */
export const REPOSITORY_URL = "https://github.com/tertius-ai/reasoning";

/** Public `GET /api/health`; release fields are null unless the server verified its release manifest. */
export type ServerHealth = {
  status: string; initialized: boolean; mail_worker_running?: boolean;
  release_id?: string | null; source_revision?: string | null; source_modified?: boolean | null;
  schema_version?: number; uptime_seconds?: number;
};
export type StatusCheck = { health: ServerHealth | null; error: string; checkedAt: Date | null; checking: boolean };

const REVISION = /^(?:[a-f0-9]{40}|[a-f0-9]{64})$/;
const RELEASE = /^[a-f0-9]{64}$/;

/** A compact uptime such as "3 h 4 min"; seconds are omitted. */
export function formatUptime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 60) return "under a minute";
  const minutes = Math.floor(seconds / 60), hours = Math.floor(minutes / 60), days = Math.floor(hours / 24);
  if (days) return `${days} d ${hours % 24} h`;
  if (hours) return `${hours} h ${minutes % 60} min`;
  return `${minutes} min`;
}

/** The short commit link, or an explicit unknown: a release without a verified source revision is never guessed. */
function Commit({ health }: { health: ServerHealth | null }) {
  const revision = health?.source_revision;
  if (typeof revision === "string" && REVISION.test(revision)) return <>
    <a href={`${REPOSITORY_URL}/commit/${revision}`} target="_blank" rel="noopener noreferrer" title={revision}><code>{revision.slice(0, 12)}</code></a>
    {health?.source_modified === true && " with uncommitted changes"}</>;
  if (health && !health.release_id) return <>development build</>;
  return <>unknown</>;
}

export function ServerStatusView({ check, onRefresh }: { check: StatusCheck; onRefresh: () => void }) {
  const { health, error, checkedAt, checking } = check;
  const release = typeof health?.release_id === "string" && RELEASE.test(health.release_id) ? health.release_id.slice(0, 12) : null;
  const details = health ? [
    health.status === "ok" ? "OK" : String(health.status),
    health.initialized ? "initialized" : "awaiting setup",
    ...(typeof health.mail_worker_running === "boolean" ? [health.mail_worker_running ? "mail worker running" : "mail worker stopped"] : []),
    ...(typeof health.schema_version === "number" ? [`schema ${health.schema_version}`] : []),
    ...(typeof health.uptime_seconds === "number" ? [`up ${formatUptime(health.uptime_seconds)}`] : []),
  ].join(" · ") : null;
  return <footer className="org-server-status" aria-label="Server status" aria-busy={checking}>
    <h2>Server status</h2>
    <dl>
      <dt>Commit</dt><dd><Commit health={health} />{release && <> · release <code title={health?.release_id ?? undefined}>{release}</code></>}</dd>
      <dt>Server</dt><dd role="status">{error ? <span className="org-server-status-error">Unavailable: {error}</span> : details ?? "Checking…"}</dd>
      <dt>Checked</dt><dd>{checkedAt ? <time dateTime={checkedAt.toISOString()}>{checkedAt.toLocaleTimeString()}</time> : "not yet"}
        {" "}<button type="button" onClick={onRefresh} disabled={checking}>{checking ? "Checking…" : "Refresh"}</button></dd>
    </dl>
  </footer>;
}

/** Checks server health once when shown and on explicit refresh or reconnection; it never polls. */
export function ServerStatus() {
  const [check, setCheck] = useState<StatusCheck>({ health: null, error: "", checkedAt: null, checking: true });
  const current = useRef<AbortController | null>(null);
  const refresh = useCallback(() => {
    current.current?.abort();
    const controller = new AbortController();
    current.current = controller;
    setCheck(previous => ({ ...previous, checking: true }));
    if (typeof navigator !== "undefined" && navigator.onLine === false) {
      setCheck({ health: null, error: "this device is offline", checkedAt: new Date(), checking: false });
      return;
    }
    request<ServerHealth>("/health", { signal: controller.signal }).then(
      health => { if (!controller.signal.aborted) setCheck({ health, error: "", checkedAt: new Date(), checking: false }); },
      cause => {
        if (controller.signal.aborted || aborted(cause)) return;
        // A proxy's error page has no API detail, so report the status code rather than a generic form error.
        const error = cause instanceof ApiError ? `server responded with HTTP ${cause.status}` : "server unreachable";
        setCheck({ health: null, error, checkedAt: new Date(), checking: false });
      },
    );
  }, []);
  useEffect(() => {
    refresh();
    window.addEventListener("online", refresh);
    return () => { window.removeEventListener("online", refresh); current.current?.abort(); };
  }, [refresh]);
  return <ServerStatusView check={check} onRefresh={refresh} />;
}
