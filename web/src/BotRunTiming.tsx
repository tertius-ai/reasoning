// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import React from "react";
import { BotRun } from "./organization-api";
import "./topic-commands.css";

function duration(value: number | null | undefined) {
  return value == null ? "Not reported" : `${(value / 1000).toFixed(2)} s`;
}

/** Retained bot wait stages. Unreported stages say so; none is estimated from second-rounded timestamps. */
export function BotRunTiming({ run }: { run: BotRun }) {
  const timing = run.timing;
  if (!timing) return null;
  const runner = timing.runner;
  const rows: [string, number | null | undefined][] = [
    ["Queued → claimed", timing.queue_wait_ms],
    ["Claimed → CLI started", runner.cli_started_ms],
    ["Claimed → CLI session ready", runner.cli_ready_ms],
    ["Claimed → first text", runner.first_text_ms],
    ["Claimed → final text", runner.final_text_ms],
    ["Claimed → CLI exited", runner.cli_exited_ms],
    ["Claimed → reply committed", timing.claim_to_commit_ms],
  ];
  return <details className="org-bot-timing">
    <summary>Wait stages</summary>
    <dl className="org-shortcut-list">
      {rows.map(([label, value]) => <React.Fragment key={label}><dt>{label}</dt><dd>{duration(value)}</dd></React.Fragment>)}
      <dt>CLI model</dt><dd>{timing.resolved_model ?? "Not reported"}</dd>
    </dl>
    <small className="org-help">CLI stages are measured by the bot's runner from when it received the claim; queue and commit times by the server.</small>
  </details>;
}
