// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import { execFileSync } from "node:child_process";

/** A squash-merged pull request's subject: its title followed by ` (#number)`. */
const MERGED = /^(.+) \(#([0-9]+)\)$/;
const UNIT = "\u001f";
export const NOTES_ID = "reasoning-release-notes";

/**
 * Release notes from first-parent history (`%H`, `%cI`, `%s` per line, newest first): the build's own revision and
 * the merged pull request titles, at most `limit` of them.
 */
export function parseHistory(log, limit = 40) {
  const commits = log.split("\n").filter(Boolean).map(line => line.split(UNIT));
  const entries = [];
  for (const [commit, date, subject] of commits) {
    const match = MERGED.exec(subject ?? "");
    if (match && entries.length < limit) entries.push({ commit, date, number: Number(match[2]), title: match[1] });
  }
  return { format: 1, revision: commits[0]?.[0] ?? null, revision_date: commits[0]?.[1] ?? null, entries };
}

/** Notes for the checkout at `cwd`; a source tree without Git history has none. */
export function releaseNotes(cwd) {
  try {
    return parseHistory(execFileSync("git", ["log", "--first-parent", "-n", "200", `--format=%H${UNIT}%cI${UNIT}%s`],
      { cwd, encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] }));
  } catch {
    return { format: 1, revision: null, revision_date: null, entries: [] };
  }
}

/** An inert JSON data block; `<` is escaped so no title can close the element. */
export const notesBlock = notes =>
  `<script type="application/json" id="${NOTES_ID}">${JSON.stringify(notes).replace(/</g, "\\u003c")}</script>`;

/** Embeds the build's release notes in organization.html, so a release carries them without a new bundle file. */
export function releaseNotesPlugin(cwd) {
  return {
    name: "reasoning-release-notes",
    transformIndexHtml: {
      order: "post",
      handler: (html, context) => context.filename.endsWith("organization.html")
        ? html.replace("</head>", notesBlock(releaseNotes(cwd)) + "</head>") : html,
    },
  };
}
