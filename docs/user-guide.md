# Using Reasoning

Open **?** at the right of the top bar for in-app help. Outside a text input,
type `?` for keyboard shortcuts and slash commands.
The bottom of the help page summarizes the server when you report a problem:
the deployed commit as a short link to its source, the release ID prefix, server
status (initialization, the mail worker when configured, database schema, and
uptime), and when this was checked. The page checks once when it opens and again
on **Refresh** or when the device reconnects; it does not poll. A server without
a verified release manifest shows the commit as **development build** or
**unknown** rather than a guess, and **with uncommitted changes** marks a release
built from modified source. A failed check shows **Unavailable** with the reason.

## Sign in, invitations and passwords

Use your invitation to join once, then open the website normally to sign in.
The invitation page checks the link before asking for a password. A used link
offers sign-in and recovery; an expired or replaced link asks you to use the
newest invitation or contact your inviter. If your email already has an account,
sign in to accept the invitation. A new invitation does not reset your password.

Choose **Forgot password?** on the sign-in page to request a private recovery
email. Its link lasts 30 minutes and works once. Choose and confirm your new
password, then sign in normally; your previous sessions are signed out. If you
are joining an organization, reopen your invitation after signing in. The same
message is shown for registered and unregistered addresses. Check spam, and
contact your administrator if email is unavailable or does not arrive. If a
password submission loses its response, try signing in with the new password
before requesting another link.

**Account menu → User information** lets you change your password using your
current password, or **Sign out other sessions**. Changing the password signs
out every session and requires a new sign-in. **Sign out other sessions** keeps
this browser signed in.

Owners and administrators can open **Administration → Users** and search by
name or email across members and invitations. **Show** filters membership and
invitation states. A pending invitation is unused; it does not prove that the
person has no account. Member rows provide **Send password reset**, which emails
a recovery link for the recipient to choose a password. Invitation rows provide
**New link and email** and **Revoke**. The **Invitations** tab creates invitations
and sends batches. Email acceptance by the provider does not confirm inbox
delivery. **Recent user administration activity** shows account, membership and
invitation events. An administrator cannot view a user's password or private
recovery link.

An open tab keeps running the version it loaded. After the server is upgraded,
the next update the tab receives highlights the **What's new** button (✦) in
the top bar with a dot. It opens the changes merged since the tab's version and
**Reload to update**. The tab never reloads by itself: unsent drafts exist only
in the open tab and are discarded by a reload, so send or copy them first. After
a reload, the dot stays until you open the notes once; otherwise the button
lists recent changes. A development server without a release identity never
offers a new version.

## Bug reports and ideas

The bug button at the right of your name in the sidebar footer sends a bug or
an idea to your organization's owners and administrators. **Include
diagnostics** attaches the current view, release, browser, window size and this
tab's recent failed requests and page errors; **Preview** shows exactly what
will be sent.

Owners and administrators read reports under **Administration › Feedback**.
Reports are grouped into **Open** and **Resolved**; Resolved starts collapsed,
and **All**, **Bugs** and **Ideas** filter both groups. Each report shows its
first line until expanded. An expanded report shows the full message, the
reporter, the diagnostics, **Resolve** (or **Reopen**), and **Copy report**,
which copies the report as Markdown for an issue tracker: the diagnostics are
folded into a `<details>` block and times are in UTC. The copy includes the
reporter's name and the view, which names projects and topics, so review it
before pasting it anywhere public.

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
The fork icon opens the message actions: **Copy link**, and where allowed beta-topic
forking, editing and version history. Forked conversations have a captured
history, not access to future origin messages.

A copied message link opens the topic at that message and highlights it, also in
long topics and in another member's browser. The topic title menu's **Copy topic
link** links to the whole topic. Type `#` in the message box to reference a topic
or project you can open. A message stores the reference by identifier, so it
shows the target's current name after a rename. Members who cannot open the
target see *unavailable* instead of its name. A link to a removed or inaccessible
message, topic or project opens with a notice instead. References also render in
knowledge entries and the published research blog.

If a beta reply request is sent but the server's answer is lost, the
conversation shows **Reply request not confirmed**: the message is saved, and
the request may or may not have started. **Retry same request** resends it
without creating a second request if the first was accepted. **Dismiss pending
retry** only clears that retry from this browser; it does not stop an accepted
reply or its charges. Each running reply, bot reply, computation, and workflow has a **Stop** button
in its card header; the message box button always sends.

## Direct messages

Click a person's name in **People** to send them a private message, or use `+`
in the People header to choose any member, including people who are offline. The
conversation opens in place of the topic. **People** doubles as your inbox: everyone
you have a conversation with is listed first, most recent first, with a message
icon and the unread count beside their name, even when they are offline. Your
recipient finds you there and can reply by clicking your name. The People button in
the toolbar shows the unread total while the panel is closed. Only the two of you can read
it: organization owners and administrators cannot, and it never appears in
search, topic exports, shared knowledge, or bot and model context. Bots cannot be
messaged or mentioned there. Viewers can receive and read direct messages but
cannot start a conversation, reply or react. Use the message's reaction button
for the same emoji picker as topic chat; choose another emoji to replace your
reaction or press your selected emoji to remove it. Only the two participants
can see these reactions.

**Mute** hides a conversation's unread count. **Block** stops the other person
from starting or continuing a conversation with you, and stops you from sending
until you unblock them; they are told only that direct messages to you are
unavailable. History stays readable to both of you and is retained like topic
messages.

## Agents and bots

A beta coordinator works through the same conversation tools as its delegates.
Ordinary questions receive a conversational answer; reusable ledger records and
independent review are optional. `/tools` selects **Conversation and research**
or a focused proof strategy. Fresh-context review enables requests for review;
automatic reviewer selection happens when a review is requested.
Workers investigate subproblems, critics review arguments, librarians retrieve
sources, formalizers prepare formal statements/code, and verifiers check evidence.
Model agreement is not formal certification. The system prompt is expandable at
the top of a beta topic. `/goal` specifies an objective and acceptance criteria;
ordinary beta messages do not require a goal to use the agent harness.

Above the message box, the capability summary distinguishes source readers from
general web search. Native gamma comparisons have no research tools; use a beta
topic with a tool-capable model for source research. Connected bots use their
runner's actual grants.

Use **Research account** or `/account` for a sustained question: keep its exact
hypotheses, current argument, sources, failed approaches, gaps and next steps in
one versioned place. Earlier candidates remain readable; conflicting edits retain
your unsaved text. Link exact reusable knowledge through **Working knowledge**.
**Prepare independent critique** drafts a request in your composer; sending it
uses the normal model/budget controls. `/reviews` identifies the account version
and earlier candidates. Account critiques do not certify mathematics or promote
ledger status. Two critique requests are allowed per approach; a person can
describe a changed strategy to start another bounded cycle.

In `/tools`, **Default knowledge scope** selects the linked account working set
(the topic until an account exists), this topic, or authorized project knowledge.
On-demand readers can expand that scope. **Automatic knowledge records (0 disables)**
accepts 0–20, with eight by default, shared across relevant ledger statements, scoped critiques and
shared knowledge references. Zero disables automatic knowledge injection without
removing the pinned account or granted readers. Model activity
details can show **Latest answer checkpoint**, its remaining gap and sources.
Retained checkpoints for interrupted work or review handoffs also appear in the
conversation. A waiting review/delegate handoff remains an incomplete answer;
partial replies retain their stopping reason.

From a source checkout, run `make bot-connect` to pair and start a local Codex or
Claude Code runner. Run `make bot-doctor` for read-only setup diagnostics, and
`make bot-run` to restart a saved pairing. Only the bot's owner can invoke it until
the owner changes **Who can use this bot** in My bots to specific members or to
everyone who can write in its topics; another member's request outside that setting
shows a bot activity explaining that it was not run. Making sure you may share a bot,
for example under your CLI subscription or API terms, is your responsibility;
Reasoning does not check how a bot authenticates.
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

**Project files** or `/files` opens saved project text and the current topic's
attachments. TeX displays as source. **Open source**, **Copy complete source**,
and **Download complete file** retrieve the complete saved revision rather than
only its preview. A reply that saved files shows a chip for each one under its
text; its download button saves that revision, and its open button shows it in
**Project files**. Bot workspace files must be published before their links are
shared. `/attach` uploads pictures and PDFs. PDF text extraction reports coverage
and failures; topic writers can **Retry text extraction**, including for existing
uploads. Scans need OCR or a TeX/text source, and extracted text can omit diagrams.
See [Pictures, PDFs and TeX](media.md).

In gamma topics, **Workflow activity** in the topic's details card (click the
topic's name) shows the organization's allowance of 64 unfinished workflow runs,
with named workflows, topics and requesters. Filters separate runs in use, waiting
for approval, needing inspection and released, and a search finds a workflow or
topic. Waiting for approval or confirmation also reserves a place; actual model
calls and computations have separate execution limits. Released places are listed
under their own filter and are excluded from the occupied count. Administrators can inspect
retained operational records from deleted topics. After checking an inactive
uncertain run, **Release workflow slot** makes its place available while keeping
the unresolved result and any held charges. This does not retry or settle the
operation. For a bot with an unknown retained
outcome, stop its runner and follow the printed inspection/recovery commands;
see [Bots](bots.md). Inspection does not change the paired state.

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
`@` (people and bots), `#` (topics and projects) and `/` open autocomplete;
arrows and Enter/Tab select; Escape dismisses menus.
On a phone, **More topic actions** reveals the compact toolbar.
Hover or keyboard-focus a message for reaction, message actions (copy link,
fork, edit), and single-message copy controls. The upward triangle attaches a picture or PDF.

A private project is visible to its roster and to every organization owner and
admin, who can also open its topics. If you see a project marked
`private · visible to you as admin`, you are not on its roster and see it through your
organization role; its settings list everyone who has access that way.

Deleting a project or channel includes its children in one confirmation. Active
work is cancelled, pending approvals revoked, and late effects fenced. Unknown
charges and execution receipts remain retained; deletion cannot erase provider
bills or guarantee an external process has stopped.

For installation and recovery, use [Deployment](deployment.md) and
[Operations](operations.md). For a fresh source checkout, start at [README](../README.md).
