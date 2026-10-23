// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import { TOPIC_COMMANDS } from "./TopicCommands";
import { Fragment } from "react";
import { ServerStatus } from "./ServerStatus";

export function HelpPage({ onClose }: { onClose: () => void }) {
  return <section className="org-help-page" aria-label="Reasoning help"><header><h1>Help</h1><button onClick={onClose}>Back to chat</button></header>
    <h2>Conversations and agents</h2>
    <p>Alpha topics are human conversations. Beta topics use your selected OpenRouter model or connected bot for research. Gamma topics compare models and run workflows or computations.</p>
    <p>Ordinary beta questions get a conversational answer. Ledger records and independent review are optional ways to support substantial work. When useful, a coordinator can delegate narrow tasks to workers, critics, librarians, formalizers, and verifiers. A model review is not a formal proof or a human check.</p>
    <p>Select the model and effort at the bottom right of the message box. Play starts or resumes work; stop fences new actions. A goal states an objective and acceptance criteria, and continues until achieved, stopped, blocked, or an explicit allowance is reached. Read the expandable system prompt above a beta conversation to inspect its instructions.</p>
    <h2>Research settings and answers</h2>
    <p>Use <code>/tools</code> to choose Conversation and research or a focused proof strategy. Fresh-context review makes independent review available; a reviewer is selected automatically when review is requested. You can choose a reviewer override or disable review.</p>
    <p><strong>Automatic relevant ledger records (0 disables)</strong> controls how many matching records enter the prompt: eight by default, adjustable from zero to twenty. Broader knowledge remains available through granted readers; this setting does not change anyone's access.</p>
    <p>The capability summary above the message box distinguishes source readers from general web search. Native gamma comparisons have no research tools; use a beta topic with a tool-capable model for source research. Connected bots use their runner's actual grants.</p>
    <p>Model activity details can include a <strong>Latest answer checkpoint</strong>, remaining gap and sources. Retained checkpoints for interrupted work or review handoffs also appear in the conversation. A waiting review or delegate handoff is an incomplete answer, even if that execution turn succeeded. Partial answers keep their stopping reason.</p>
    <h2>Direct messages</h2>
    <p>Click a name in People, or + in the People header, to send a private message. People you have messaged are listed first in People, with their unread count beside their name. Only the two participants can read it; administrators, search, exports, bots and models cannot. Mute hides a conversation's count; Block stops the other person from messaging you until you unblock them.</p>
    <p>Use the message's reaction button for the same emoji picker as topic chat. Choose another emoji to replace yours, or press your selected emoji to remove it. Reactions stay private to the two participants; viewers cannot reply or react.</p>
    <h2>Bots and permissions</h2>
    <p>Account menu → My bots manages personal bots. From a checkout, <code>make bot-connect</code> pairs a Codex or Claude Code bot and keeps it online. <code>make bot-doctor</code> checks its setup without inference; <code>make bot-run</code> restarts a paired runner. Private pairing state stays outside the checkout.</p>
    <p>Type @ to select a bot, or @Bot- to choose its advertised model. In My bots, the owner chooses <strong>Who can use this bot</strong>: only the owner, named members, or people who can write in its topics. Topic grants still limit what it can read and do.</p>
    <p>Local execution and shared compute require owner approval. GitHub issues require an exact preview approval; the owner can let selected trusted requesters approve their own issues within repository and daily limits. Credentials never belong in chat.</p>
    <h2>Tools and shared knowledge</h2>
    <p>Tools include authorized history/search, source retrieval, ledger claims/proofs/references, bibliography, blog, files, delegation, review, and approved CAS/Lean execution. Availability depends on topic and organization settings. Use the graph, book, and blog icons to inspect shared work. Cite sources with exact theorem/page locators.</p>
    <h2>Files, TeX and PDFs</h2>
    <p><strong>Project files</strong> or <code>/files</code> opens saved project text and the topic's attachments. TeX displays as source. <strong>Open source</strong>, <strong>Copy complete source</strong> and <strong>Download complete file</strong> retrieve the complete saved revision, even when its preview is shortened. Ask a bot to publish a local workspace file before sharing its link.</p>
    <p>Use <code>/attach</code> for a picture or PDF. PDF text extraction reports coverage and failures; uploading does not guarantee readable text. Topic writers can choose <strong>Retry text extraction</strong>, including for existing uploads. Scans need OCR or a TeX/text source. Native models receive extracted PDF text, which may omit diagrams; authorized connected bots can download the original.</p>
    <h2>Recovery and diagnostics</h2>
    <p>For a bot with an unknown retained outcome, stop its runner and follow its printed recovery commands. <code>uv run --locked reasoning-bot recover --state-dir BOT_STATE --action inspect</code> inspects the paired state without changing it; replace BOT_STATE with that runner's state directory. Inspect the result before explicitly skipping retained work. An unknown operation is not automatically replayed.</p>
    <p>In a gamma topic, <strong>Workflow capacity</strong> shows admission usage and unfinished runs you can access. An administrator can inspect stopped uncertain work and choose <strong>Release workflow slot</strong>. Releasing a slot retains its unknown outcome and held charges; it does not retry or settle the work.</p>
    <h2>Small acceptance tests</h2>
    <ol><li>Ask a beta agent to explain a short argument. If it is useful to retain, ask it to save the claim and reference, then inspect their statement, dependencies, and provenance.</li>
      <li>Ask a bot for a small polynomial factorization using local CAS. Approve the exact command in My bots and inspect its output.</li>
      <li>With compute enabled, use /compute for a small CAS job, then Lean: <code>example : (2 : Nat) + 2 = 4 := by decide</code>. Inspect the image, logs, and artifacts. Successful setup is not evidence of execution.</li>
      <li>Ask an agent to read a blog post and save a short draft. Inspect the editor before publishing.</li>
      <li>For a developer bot on your Linux development box, configure <code>make dev-bot-config DEV_REPOSITORY=owner/repository</code>, enable Developer tools and local execution, and restart its runner. Ask it to inspect the deployed release or a specific authorized topic/run and run <code>development_test</code>; click its pending-request indicator in People to approve the exact source snapshot. <code>make dev-bot-test</code> runs the same offline checks locally without inference. GitHub CLI authentication is needed only for publishing an approved issue.</li></ol>
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
