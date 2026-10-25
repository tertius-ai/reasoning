// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { mkdtemp, readFile, realpath, rm } from "node:fs/promises";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { chromium } from "playwright";

const repository = fileURLToPath(new URL("../../", import.meta.url));
const password = "long direct message test password";

async function visible(locator) { await locator.waitFor({ state: "visible", timeout: 15000 }); }

test("a member messages a colleague from People, who finds, reads and replies, then blocks", { timeout: 180000 }, async (context) => {
  const temporary = await realpath(await mkdtemp(path.join(os.tmpdir(), "reasoning-direct-browser-")));
  const state = path.join(temporary, "state");
  const listener = net.createServer(); listener.listen(0, "127.0.0.1"); await once(listener, "listening");
  const port = listener.address().port; await new Promise((resolve) => listener.close(resolve));
  const url = `http://127.0.0.1:${port}`;
  const server = spawn(path.join(repository, ".venv/bin/python"), ["-m", "reasoning_ledger.server.cli", "--data-dir", state,
    "serve", "--public-url", url, "--port", String(port), "--frontend-dir", path.join(repository, "web/dist")], { cwd: repository });
  let logs = "";
  server.stdout.on("data", (chunk) => { logs += chunk; }); server.stderr.on("data", (chunk) => { logs += chunk; });
  let browser;
  try {
    const deadline = Date.now() + 15000;
    while (Date.now() < deadline) {
      try { if ((await fetch(url + "/api/health")).ok) break; } catch { /* Not listening yet. */ }
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    browser = await chromium.launch({ headless: true });
    const pageErrors = [];
    const owner = await (await browser.newContext({ viewport: { width: 1440, height: 920 } })).newPage();
    owner.on("pageerror", (error) => pageErrors.push(error.message));
    await owner.goto(url);
    await owner.getByLabel("Setup credential").fill(await readFile(path.join(state, "setup-token"), "utf8"));
    await owner.getByLabel("Email", { exact: true }).fill("owner@example.org");
    await owner.getByLabel("Your name").fill("Owner");
    await owner.getByLabel("Password", { exact: true }).fill(password);
    await owner.getByLabel("Organization name").fill("Mathematics");
    await owner.getByRole("button", { name: "Create organization", exact: true }).click();
    await visible(owner.getByRole("button", { name: "Account menu", exact: true }));
    // People doubles as the inbox: there is no separate conversation list, and + messages any member.
    await visible(owner.getByRole("complementary", { name: "Online people and bots" }).getByRole("button", { name: "New direct message", exact: true }));
    assert.equal(await owner.getByRole("navigation", { name: "Direct messages" }).count(), 0);

    await owner.getByRole("button", { name: "Account menu", exact: true }).click();
    await owner.getByRole("button", { name: "Administration" }).click();
    const dialog = owner.getByRole("dialog");
    await dialog.getByRole("button", { name: "Invitations", exact: true }).click();
    await dialog.getByLabel("Invite email").fill("member@example.org");
    await dialog.getByRole("button", { name: "Create invitation", exact: true }).click();
    const invitation = dialog.getByLabel("Invitation link", { exact: false });
    for (const stop = Date.now() + 15000; !(await invitation.inputValue().catch(() => "")).includes("#invite=") && Date.now() < stop;)
      await new Promise((resolve) => setTimeout(resolve, 100));
    const link = await invitation.inputValue();
    await dialog.getByRole("button", { name: "Close dialog" }).click();

    const member = await (await browser.newContext({ viewport: { width: 1280, height: 860 } })).newPage();
    member.on("pageerror", (error) => pageErrors.push(error.message));
    await member.goto(link); await visible(member.getByRole("heading", { name: "Join an organization" }));
    await member.getByLabel("Your name").fill("Researcher"); await member.getByLabel("Password", { exact: true }).fill(password);
    await member.getByRole("button", { name: "Accept invitation", exact: true }).click();
    await visible(member.getByRole("button", { name: "Account menu", exact: true }));

    // Anyone can report a bug from the bug button at the right of their name; owners and administrators read it in Administration.
    const bug = member.getByRole("button", { name: "Report a bug or send feedback", exact: true });
    const [nameBox, bugBox] = await Promise.all([member.getByRole("button", { name: "Account menu", exact: true }).boundingBox(), bug.boundingBox()]);
    assert.ok(bugBox.x > nameBox.x + 120, "the bug button sits at the right edge of the footer");
    await bug.click();
    const feedback = member.getByRole("dialog", { name: "Send feedback" });
    await feedback.getByLabel("What happened").fill("The reply pointer stays highlighted.");
    // Preview shows exactly what will be sent, including the diagnostics, and can send from there.
    await feedback.getByRole("button", { name: "Preview", exact: true }).click();
    const preview = member.getByRole("dialog", { name: "Report preview" });
    await visible(preview.getByText("The reply pointer stays highlighted."));
    await visible(preview.getByRole("heading", { name: /Failed requests/ }));
    assert.match(await preview.innerText(), /organization [a-f0-9]{32}/);
    await preview.getByRole("button", { name: "Close", exact: true }).click();
    await preview.waitFor({ state: "detached" });
    await feedback.getByRole("button", { name: "Preview", exact: true }).click();
    await preview.getByRole("button", { name: "Send", exact: true }).click();
    await visible(feedback.getByRole("status").filter({ hasText: "Your report went to the administrators of Mathematics" }));
    await owner.getByRole("button", { name: "Account menu", exact: true }).click();
    await owner.getByRole("button", { name: "Administration" }).click();
    const administration = owner.getByRole("dialog");
    await administration.getByRole("button", { name: "Feedback", exact: true }).click();
    const report = administration.getByRole("listitem").filter({ hasText: "The reply pointer stays highlighted." });
    await visible(report);
    assert.match(await report.innerText(), /Researcher/);
    await report.getByRole("button", { name: "Resolve", exact: true }).click();
    await visible(report.getByRole("button", { name: "Reopen", exact: true }));
    await administration.getByRole("button", { name: "Close dialog" }).click();

    // The owner clicks the member's name in People and sends a private message.
    const people = owner.getByRole("complementary", { name: "Online people and bots" });
    await people.getByRole("button", { name: "Researcher", exact: true }).click();
    const conversation = owner.getByRole("region", { name: "Direct messages with Researcher" });
    await visible(conversation);
    assert.equal(await people.getByRole("button", { name: "Owner", exact: true }).count(), 0, "people do not message themselves");
    await owner.getByLabel("Message", { exact: true }).fill("Could you read section 3 before Friday?");
    await owner.keyboard.press("Enter");
    await visible(conversation.locator(".org-message").filter({ hasText: "Could you read section 3 before Friday?" }));
    await visible(people.locator('button[aria-current="page"]').filter({ hasText: "Researcher" }));

    // The recipient finds the conversation by its unread count, reads it, and replies.
    const memberPeople = member.getByRole("complementary", { name: "Online people and bots" });
    const ownerRow = memberPeople.locator(".org-presence-row").filter({ has: member.getByRole("button", { name: "Owner", exact: true }) });
    await visible(ownerRow.getByLabel("1 unread direct messages"));
    assert.equal(await member.getByRole("navigation", { name: "Direct messages" }).count(), 0, "no separate conversation list");
    assert.equal((await memberPeople.locator(".org-presence-row strong").first().textContent())?.trim(), "Owner",
      "conversation partners are listed first");
    // The People toggle carries the count, so it stays visible while the panel is closed.
    await visible(member.getByRole("button", { name: "Show online people and bots, 1 unread direct messages", exact: true }));
    // The whole row opens the conversation, including the unread count at its right edge.
    const rowBox = await ownerRow.boundingBox(), countBox = await ownerRow.getByLabel("1 unread direct messages").boundingBox();
    await ownerRow.click({ position: { x: countBox.x + countBox.width / 2 - rowBox.x, y: countBox.y + countBox.height / 2 - rowBox.y } });
    const reply = member.getByRole("region", { name: "Direct messages with Owner" });
    await visible(reply.locator(".org-message").filter({ hasText: "section 3" }));
    await ownerRow.getByLabel("1 unread direct messages").waitFor({ state: "detached", timeout: 15000 });
    await member.getByLabel("Message", { exact: true }).fill("Yes, I will send comments tomorrow.");
    await member.getByRole("button", { name: "Send", exact: true }).click();
    await visible(conversation.locator(".org-message").filter({ hasText: "comments tomorrow" }));

    // The same picker works in a private conversation and changes reach the other browser.
    const received = reply.locator(".org-message").filter({ hasText: "section 3" });
    // "Mark unread from here" brings the count back and shows where; the open conversation does not clear it again.
    await received.hover(); await received.getByRole("button", { name: "Message actions", exact: true }).click();
    await member.getByRole("menuitem", { name: "Mark unread from here", exact: true }).click();
    await visible(reply.getByRole("separator", { name: "New messages" }));
    await visible(ownerRow.getByLabel("1 unread direct messages"));
    await member.waitForTimeout(1500);
    assert.equal(await ownerRow.getByLabel("1 unread direct messages").count(), 1, "the open conversation keeps it unread");
    await received.hover(); await received.getByRole("button", { name: "Add reaction", exact: true }).click();
    await member.getByRole("menuitemcheckbox", { name: "Heart", exact: true }).click();
    const original = conversation.locator(".org-message").filter({ hasText: "section 3" });
    await visible(original.getByRole("button", { name: "Heart: Researcher", exact: true }));
    await received.getByRole("button", { name: "Heart: Researcher", exact: true }).click();
    await original.getByRole("button", { name: "Heart: Researcher", exact: true }).waitFor({ state: "detached" });

    // Replies work as in topics: Reply sets the target, the reply points back, and the pointer opens the whole thread.
    await received.hover(); await received.getByRole("button", { name: "Reply", exact: true }).click();
    await visible(reply.locator(".org-reply-bar").filter({ hasText: "Replying to Owner" }));
    await member.getByLabel("Message", { exact: true }).fill("Section 3 looks right to me.");
    await member.keyboard.press("Enter");
    const answered = conversation.locator(".org-message").filter({ hasText: "Section 3 looks right" });
    await visible(answered.locator(".org-reply-pointer").filter({ hasText: "Could you read section 3" }));
    await answered.locator(".org-reply-pointer").click();
    const thread = owner.getByRole("dialog", { name: "Thread", exact: true });
    await visible(thread.getByText("2 messages", { exact: true }));
    await owner.keyboard.press("Escape");
    await thread.waitFor({ state: "detached" });

    // Your own groups are mirrored: dot and line on the right, toolbar on the left; each person keeps one color in both browsers.
    // The reply quotes "section 3" in its pointer, so match only the messages without one.
    const ownOriginal = conversation.locator(".org-message").filter({ hasText: "Could you read section 3", hasNot: owner.locator(".org-reply-pointer") });
    const theirCopy = reply.locator(".org-message").filter({ hasText: "Could you read section 3", hasNot: member.locator(".org-reply-pointer") });
    const mine = ownOriginal.locator("xpath=..");
    assert.match(await mine.getAttribute("class"), /org-message-group-mine/);
    assert.doesNotMatch(await answered.locator("xpath=..").getAttribute("class"), /org-message-group-mine/);
    await ownOriginal.hover();
    const [box, launcher] = await Promise.all([ownOriginal.boundingBox(), ownOriginal.getByRole("button", { name: "Add reaction", exact: true }).boundingBox()]);
    assert.ok(launcher.x + launcher.width / 2 < box.x + box.width / 2, "the toolbar of your own message is on the left");
    const tone = async (locator) => /org-person-tone-\d+/.exec(await locator.locator("xpath=..").getAttribute("class"))?.[0];
    assert.ok(await tone(ownOriginal));
    assert.equal(await tone(ownOriginal), await tone(theirCopy), "Owner has the same color in both browsers");
    assert.notEqual(await tone(ownOriginal), await tone(answered), "the two people in a conversation never share a color");
    assert.equal(await tone(answered), await tone(reply.locator(".org-message").filter({ hasText: "Section 3 looks right" })));

    // Files arrive as media bubbles: pictures in a grid that opens full size, PDFs as rows with Open and Download.
    const pixel = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "base64");
    await conversation.locator("input[type=file]").setInputFiles([
      { name: "board.png", mimeType: "image/png", buffer: pixel },
      { name: "notes.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.4\ntrailer << >>\n%%EOF\n") },
    ]);
    const tray = conversation.getByRole("list", { name: "Attachments to send" });
    await visible(tray.getByRole("button", { name: "Remove board.png", exact: true }));
    await conversation.getByLabel("Message", { exact: true }).fill("The board after the seminar");
    const sendFiles = conversation.getByRole("button", { name: "Send", exact: true });
    await sendFiles.waitFor({ state: "visible" });
    for (const stop = Date.now() + 15000; await sendFiles.isDisabled() && Date.now() < stop;) await new Promise((resolve) => setTimeout(resolve, 100));
    // As in a human chat, the plus and the return button sit inside the box, at its left and right ends.
    const composerBox = await conversation.locator(".org-composer-box").boundingBox(), sendBox = await sendFiles.boundingBox();
    const plusBox = await conversation.getByRole("button", { name: "Attach pictures or PDFs", exact: true }).boundingBox();
    assert.ok(plusBox.x - composerBox.x < 12 && composerBox.x + composerBox.width - sendBox.x - sendBox.width < 12, "plus left, return right, inside the box");
    assert.equal(plusBox.y, sendBox.y); assert.equal(plusBox.height, 28);
    await sendFiles.click();
    await tray.waitFor({ state: "detached" });
    const delivered = reply.locator(".org-message").filter({ hasText: "The board after the seminar" });
    await visible(delivered.locator(".direct-media-tile img"));
    await visible(delivered.locator(".direct-file").filter({ hasText: "notes.pdf" }).getByRole("link", { name: "Download", exact: true }));
    assert.equal(await delivered.locator(".direct-media-tile").count(), 1, "the PDF is a file row, not a picture tile");
    assert.doesNotMatch(await delivered.innerText(), /\/api\/organizations/, "references never show as text");
    await delivered.getByRole("button", { name: "Open board.png", exact: true }).click();
    const lightbox = member.getByRole("dialog", { name: "board.png" });
    await visible(lightbox.locator("img"));
    await member.keyboard.press("Escape");
    await lightbox.waitFor({ state: "detached" });

    // Scrolled up, the reader gets the newest-message button of topic chats; it counts the other person's new messages.
    await member.setViewportSize({ width: 1280, height: 480 });
    await reply.getByLabel("Message history", { exact: true }).evaluate((view) => { view.scrollTop = 0; view.dispatchEvent(new Event("scroll")); });
    const newest = reply.getByRole("button", { name: "Jump to newest message", exact: true });
    await visible(newest);
    await owner.getByLabel("Message", { exact: true }).fill("One more thought on section 3.");
    await owner.keyboard.press("Enter");
    await visible(reply.getByRole("button", { name: "Jump to newest message, 1 new", exact: true }));
    await reply.getByRole("button", { name: "Jump to newest message, 1 new", exact: true }).click();
    await visible(reply.locator(".org-message").filter({ hasText: "One more thought on section 3." }));
    await reply.locator(".org-jump").waitFor({ state: "detached" });
    await member.setViewportSize({ width: 1280, height: 860 });

    // The title in the top bar opens the conversation's details: who can read it, Mute, the shared files and Block.
    assert.equal(await reply.getByText("Only you and Owner can read this conversation", { exact: false }).count(), 0, "no header strip");
    const title = member.getByRole("button", { name: /Direct messages · Owner/ });
    await title.click();
    const details = member.getByRole("dialog", { name: "Conversation details" });
    await visible(details.getByText("Only you and Owner can read this conversation. Bots and models never take part."));
    await details.getByRole("button", { name: "Files, 2", exact: true }).click();
    const files = member.getByRole("dialog", { name: "Files with Owner" });
    await visible(files.getByRole("link", { name: "notes.pdf", exact: true }));
    await files.getByRole("button", { name: /^PDFs/ }).click();
    assert.equal(await files.getByRole("link", { name: "board.png", exact: true }).count(), 0, "the PDF filter hides pictures");
    await files.getByRole("button", { name: /^All/ }).click();
    await files.getByRole("listitem").filter({ hasText: "board.png" }).getByRole("button", { name: "Show in chat", exact: true }).click();
    await files.waitFor({ state: "detached" });
    await visible(reply.locator(".org-message-linked").filter({ hasText: "The board after the seminar" }));

    // Muting hides the unread count; blocking stops the other person's messages with a neutral refusal.
    await title.click();
    await details.getByRole("switch", { name: "Mute" }).click();
    await visible(ownerRow.getByLabel("Muted"));
    await visible(details.getByRole("switch", { name: "Mute", checked: true }));
    await details.getByRole("button", { name: "Block Owner", exact: true }).click();
    await details.getByRole("group", { name: "Block Owner?" }).getByRole("button", { name: "Block", exact: true }).click();
    await visible(reply.getByText("You blocked Owner."));
    await member.keyboard.press("Escape");
    assert.equal(await member.getByLabel("Message", { exact: true }).isDisabled(), true);
    await owner.getByLabel("Message", { exact: true }).fill("Thanks!");
    await owner.keyboard.press("Enter");
    await visible(conversation.getByRole("alert").filter({ hasText: "direct messages to this person are unavailable" }));
    assert.equal(await conversation.getByText(/blocked/i).count(), 0, "the refusal does not reveal the block");
    await title.click();
    await details.getByRole("button", { name: "Unblock Owner", exact: true }).click();
    await visible(details.getByRole("button", { name: "Block Owner", exact: true }));
    assert.deepEqual(pageErrors, []);
    context.diagnostic("Verified People-initiated direct messages, unread navigation, reading, replying, files, muting and blocking.");
  } finally {
    await browser?.close();
    server.kill("SIGTERM");
    const live = () => server.exitCode === null && server.signalCode === null;
    if (live()) await Promise.race([once(server, "exit"), new Promise((resolve) => setTimeout(resolve, 5000))]);
    if (live()) { server.kill("SIGKILL"); await once(server, "exit"); }
    assert.ok(!logs.includes(password));
    await rm(temporary, { recursive: true, force: true });
  }
});
