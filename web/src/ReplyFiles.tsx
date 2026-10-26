// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import React from "react";
import "./reply-files.css";

/** A project file revision that a reply's own run saved, as the server read it from that run's receipts. */
export type ReplyFile = { path: string; revision: number; sha256: string; total_characters: number; url: string; download_url: string };

const OPEN_FILE = "reasoning-open-project-file";
type OpenFileDetail = { topicId: string; path: string; revision: number; handled: boolean };

/** Show a saved revision in the topic's Project files viewer, or its source in a new tab where that viewer is absent. */
export function openProjectFile(topicId: string, file: ReplyFile) {
  const detail: OpenFileDetail = { topicId, path: file.path, revision: file.revision, handled: false };
  window.dispatchEvent(new CustomEvent(OPEN_FILE, { detail }));
  if (!detail.handled) window.open(file.url, "_blank", "noopener,noreferrer");
}

export function useOpenProjectFile(topicId: string, onOpen: (file: { path: string; revision: number }) => void) {
  const handler = React.useRef(onOpen); handler.current = onOpen;
  React.useEffect(() => {
    const receive = (event: Event) => {
      const detail = (event as CustomEvent<OpenFileDetail>).detail;
      if (detail?.topicId !== topicId) return;
      detail.handled = true;
      handler.current({ path: detail.path, revision: detail.revision });
    };
    window.addEventListener(OPEN_FILE, receive);
    return () => window.removeEventListener(OPEN_FILE, receive);
  }, [topicId]);
}

const fileName = (path: string) => path.split("/").at(-1) ?? path;
const extension = (path: string) => (path.match(/\.([a-z0-9]+)$/i)?.[1] ?? "txt").slice(0, 3).toUpperCase();
const characters = (count: number) => count >= 1000 ? `${Math.round(count / 1000).toLocaleString()}k chars` : `${count} chars`;

function DownloadIcon() {
  return <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M12 4v11" /><path d="m7 10 5 5 5-5" /><path d="M5 19h14" /></svg>;
}

function OpenIcon() {
  return <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z" /><circle cx="12" cy="12" r="2.8" /></svg>;
}

/** One chip per file a reply saved, independent of whether the reply's text links to it. */
export function ReplyFiles({ topicId, files }: { topicId: string; files: ReplyFile[] }) {
  return <ul className="reply-files" aria-label="Files saved by this reply">{files.map(file => {
    const name = fileName(file.path);
    return <li key={file.path + "@" + file.revision} className="reply-file">
      <span className="reply-file-kind" aria-hidden="true">{extension(file.path)}</span>
      <span className="reply-file-name" title={file.path}>{name}</span>
      <small>rev {file.revision} · {characters(file.total_characters)}</small>
      <span className="reply-file-actions">
        <a href={file.download_url} download={name} aria-label={`Download ${name}`} title="Download"><DownloadIcon /></a>
        <button type="button" aria-label={`Open ${name} in project files`} title="Open" onClick={() => openProjectFile(topicId, file)}><OpenIcon /></button>
      </span>
    </li>;
  })}</ul>;
}
