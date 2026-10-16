# Using Reasoning

Open **?** at the right of the top bar for in-app help. Outside a text input,
type `?` for keyboard shortcuts and slash commands.

## Conversations

- Alpha topics: human discussion, with explicit bot mentions.
- Beta topics: research with a selected OpenRouter model or connected CLI bot.
- Gamma topics: comparisons, workflows, and isolated computations.

The message box has action controls on the left and model/effort controls on the
bottom right. Enter sends; Shift+Enter inserts a newline. The model picker has
search and person-specific favorites saved on the server across devices and
organizations. Effort choices depend on the advertised
model capabilities; default leaves the provider's effort unchanged.
Markdown, code blocks, tables, and TeX render in messages. Raw HTML is not executed.
The fork icon opens beta-topic forking and available message actions. Forked
conversations have a captured history, not access to future origin messages.

If a beta reply request is sent but the server's answer is lost, the
conversation shows **Reply request not confirmed**: the message is saved, and
the request may or may not have started. **Retry same request** resends it
without creating a second request if the first was accepted. **Dismiss pending
retry** only clears that retry from this browser; it does not stop an accepted
reply or its charges. Use **Stop current work** to cancel running work.

## Agents and bots

A beta coordinator works through the same conversation tools as its delegates.
Workers investigate subproblems, critics review arguments, librarians retrieve
sources, formalizers prepare formal statements/code, and verifiers check evidence.
Model agreement is not formal certification. The system prompt is expandable at
the top of a beta topic. `/goal` specifies an objective and acceptance criteria;
ordinary beta messages do not require a goal to use the agent harness.

From a source checkout, run `make bot-connect` to pair and start a local Codex or
Claude Code runner. Run `make bot-doctor` for read-only setup diagnostics, and
`make bot-run` to restart a saved pairing. Only the bot's owner can invoke it unless
the runner is started with `make bot-run BOT_ALLOW_OTHER_USERS=1`; another member's
mention of an owner-only bot shows a bot activity explaining that it was not run.
The account menu's **My bots** controls access. Requests awaiting your approval appear as **!** beside the bot in **People**; click the
bot's name or **!** to review the exact command, computation, or GitHub issue.
Closing the dialog leaves the request pending. Type `@` for a bot, or `@Bot-` to select one of its
advertised models. Workspace and context boundaries are described in [Bots](bots.md).

Bot-requested local execution, shared computation, and GitHub issue publication
need the bot owner's approval for that exact action once. A bot owner can instead
let chosen members approve their own GitHub issue requests, up to a daily limit;
those issues name the requesting member and bot. There is no short
application decision timeout, but the native CLI transport can still end. An
interrupted unconsumed request cannot be approved: reject it, stop its retained
invocation in the topic, restart `make bot-run`, and submit a new request. Nothing
is automatically retried. See [Bots](bots.md) for consumed-action reconciliation.
Never paste passwords, provider tokens, or SSH keys into messages.

## Shared work

The graph, book, and blog icons open the ledger, bibliography, and published
research blog. A source can have exact statements with theorem/page locators;
those statements are ledger claims, usable as proof dependencies. Its source
revision can be pinned. Human checks, model reviews, and formal execution
evidence remain distinct. See [Organization ledger](organization-ledger.md).

The published blog links to its editor. Agent edits become drafts and do not
silently replace the current publication. Inspect a draft before publishing.

## Small acceptance tests

Use a disposable project. Inference and Modal execution may cost money.

1. Ask a beta agent to record a short claim and scholarly reference. Inspect the
   statement, author, source locator, and dependencies in the ledger.
2. Ask a local bot to factor a small polynomial with its installed CAS. Approve
   the exact command through its **!** indicator in People. Check the output,
   not just the bot's assertion.
3. With compute configured, use `/compute` for a small CAS job. Inspect its image,
   logs, and artifacts. Then compile a Lean example:
   `example : (2 : Nat) + 2 = 4 := by decide`. [Compute](compute.md) explains the
   stronger target/type and axiom audit. These reports are not automatically
   trusted proof attestations.
4. Ask an agent to read a blog post, propose a change, and save a draft. Check
   the editor, publish deliberately, and inspect the published version.
5. Enable Developer tools for a bot and authenticate `gh` on its host. Request
   sanitized diagnostics and a test GitHub issue. Review and approve its exact
   publication, then verify the issue URL. No production shell access is implied.

A configured worker or successful setup wizard is not evidence a live job ran.
Record actual run IDs, tool outputs, and artifacts when reporting acceptance.

## Navigation and lifecycle

Ctrl/⌘+B toggles navigation; Ctrl/⌘+Shift+P toggles people and bots;
Ctrl/⌘+K searches; Alt+↑/↓ changes topics. `?` toggles help outside text fields.
`@` and `/` open autocomplete; arrows and Enter/Tab select; Escape dismisses menus.
On a phone, **More topic actions** reveals the compact toolbar.
Hover or keyboard-focus a message for reaction, fork/edit, and single-message
copy controls. The upward triangle attaches a picture or PDF.

Deleting a project or channel includes its children in one confirmation. Active
work is cancelled, pending approvals revoked, and late effects fenced. Unknown
charges and execution receipts remain retained; deletion cannot erase provider
bills or guarantee an external process has stopped.

For installation and recovery, use [Deployment](deployment.md) and
[Operations](operations.md). For a fresh source checkout, start at [README](../README.md).
