// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import { TOPIC_COMMANDS } from "./TopicCommands";
import { Fragment } from "react";
import { ServerStatus } from "./ServerStatus";

export function HelpPage({ onClose }: { onClose: () => void }) {
  return <section className="org-help-page" aria-label="Reasoning help"><header><h1>Help</h1><button onClick={onClose}>Back to chat</button></header>
    <h2>Conversations and agents</h2>
    <p>Alpha topics are human conversations. Beta topics use your selected OpenRouter model or connected bot for research. Gamma topics compare models and run workflows or computations.</p>
    <p>The coordinator delegates narrow tasks to workers, critics, librarians, formalizers, and verifiers. Librarians find sources; critics check arguments; formalizers prepare Lean code. A model review is not a formal proof or a human check.</p>
    <p>Select the model and effort at the bottom right of the message box. Play starts or resumes work; stop fences new actions. A goal states an objective and acceptance criteria, and continues until achieved, stopped, blocked, or an explicit allowance is reached. Read the expandable system prompt above a beta conversation to inspect its instructions.</p>
    <h2>Direct messages</h2>
    <p>Click a name in People, or + beside Direct messages, to send a private message. Only the two participants can read it; administrators, search, exports, bots and models cannot. Unread messages are counted under Direct messages. Mute hides a conversation's count; Block stops the other person from messaging you until you unblock them.</p>
    <h2>Bots and permissions</h2>
    <p>Account menu → My bots manages personal bots. From a checkout, <code>make bot-connect</code> pairs a Codex or Claude Code bot and keeps it online. <code>make bot-doctor</code> checks its setup without inference; <code>make bot-run</code> restarts a paired runner. Private pairing state stays outside the checkout.</p>
    <p>Type @ to select a bot, or @Bot- to choose its advertised model. Bots follow their owner's access or exact topic grants. Context is scoped, not unrestricted organization memory. Local execution, shared compute, and GitHub issue publication require owner approval. Credentials never belong in chat.</p>
    <h2>Tools and shared knowledge</h2>
    <p>Tools include authorized history/search, source retrieval, ledger claims/proofs/references, bibliography, blog, files, delegation, review, and approved CAS/Lean execution. Availability depends on topic and organization settings. Use the graph, book, and blog icons to inspect shared work. Cite sources with exact theorem/page locators.</p>
    <h2>Small acceptance tests</h2>
    <ol><li>Ask a beta agent to explain a short argument and save its claim and reference. Inspect the ledger statement, dependencies, and provenance.</li>
      <li>Ask a bot for a small polynomial factorization using local CAS. Approve the exact command in My bots and inspect its output.</li>
      <li>With compute enabled, use /compute for a small CAS job, then Lean: <code>example : (2 : Nat) + 2 = 4 := by decide</code>. Inspect the image, logs, and artifacts. Successful setup is not evidence of execution.</li>
      <li>Ask an agent to read a blog post and save a short draft. Inspect the editor before publishing.</li>
      <li>For a developer bot on your Linux development box, configure <code>make dev-bot-config DEV_REPOSITORY=owner/repository</code>, enable Developer tools and local execution, and restart its runner. Ask it to inspect the deployed release and run <code>development_test</code>; click its pending-request indicator in People to approve the exact source snapshot. <code>make dev-bot-test</code> runs the same offline checks locally without inference. GitHub CLI authentication is needed only for publishing an owner-approved issue.</li></ol>
    <p>Inference and Modal jobs can cost money. Use a disposable project and inspect approvals and provider bills. Stopping or deleting a topic cannot erase an unknown charge or guarantee an external process stopped.</p>
    <h2>Keyboard shortcuts</h2><dl className="org-shortcut-list">
      <dt>?</dt><dd>Toggle help outside text fields</dd><dt>Enter / Shift+Enter</dt><dd>Send / new line</dd>
      <dt>Ctrl/⌘+K</dt><dd>Search</dd><dt>Ctrl/⌘+B</dt><dd>Toggle navigation</dd>
      <dt>Ctrl/⌘+Shift+P</dt><dd>Toggle people and bots</dd>
      <dt>Alt+↑ / ↓</dt><dd>Previous / next topic</dd><dt>@ / # / /</dt><dd>Mention / reference / commands</dd>
      <dt>↑ / ↓ · Enter / Tab</dt><dd>Select autocomplete</dd><dt>Escape</dt><dd>Dismiss dialogs and menus</dd></dl>
    <h2>Slash commands</h2><dl className="org-shortcut-list">{TOPIC_COMMANDS.map(command => <Fragment key={command.id}><dt>/{command.id}</dt><dd>{command.label} · {command.kinds.join(" / ")}</dd></Fragment>)}</dl>
    <ServerStatus />
  </section>;
}
