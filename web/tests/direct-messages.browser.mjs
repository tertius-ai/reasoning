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
    const ownerDirect = owner.getByRole("navigation", { name: "Direct messages" });
    await visible(ownerDirect.getByText("Click a name in People to send a message."));

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

    // The owner clicks the member's name in People and sends a private message.
    const people = owner.getByRole("complementary", { name: "Online people and bots" });
    await people.getByRole("button", { name: "Researcher", exact: true }).click();
    const conversation = owner.getByRole("region", { name: "Direct messages with Researcher" });
    await visible(conversation);
    assert.equal(await people.getByRole("button", { name: "Owner", exact: true }).count(), 0, "people do not message themselves");
    await owner.getByLabel("Message", { exact: true }).fill("Could you read section 3 before Friday?");
    await owner.keyboard.press("Enter");
    await visible(conversation.locator(".org-message").filter({ hasText: "Could you read section 3 before Friday?" }));
    await visible(ownerDirect.getByRole("button", { name: /Researcher/ }));

    // The recipient finds the conversation by its unread count, reads it, and replies.
    const memberDirect = member.getByRole("navigation", { name: "Direct messages" });
    const row = memberDirect.getByRole("button", { name: /Owner/ });
    await visible(row.getByLabel("1 unread direct messages"));
    await row.click();
    const reply = member.getByRole("region", { name: "Direct messages with Owner" });
    await visible(reply.locator(".org-message").filter({ hasText: "section 3" }));
    await row.getByLabel("1 unread direct messages").waitFor({ state: "detached", timeout: 15000 });
    await member.getByLabel("Message", { exact: true }).fill("Yes, I will send comments tomorrow.");
    await member.getByRole("button", { name: "Send", exact: true }).click();
    await visible(conversation.locator(".org-message").filter({ hasText: "comments tomorrow" }));

    // Muting hides the unread count; blocking stops the other person's messages with a neutral refusal.
    await reply.getByRole("button", { name: "Mute", exact: true }).click();
    await visible(memberDirect.getByLabel("Muted"));
    await reply.getByRole("button", { name: "Block", exact: true }).click();
    await reply.getByRole("group", { name: "Block Owner?" }).getByRole("button", { name: "Block", exact: true }).click();
    await visible(reply.getByText("You blocked Owner."));
    assert.equal(await member.getByLabel("Message", { exact: true }).isDisabled(), true);
    await owner.getByLabel("Message", { exact: true }).fill("Thanks!");
    await owner.keyboard.press("Enter");
    await visible(conversation.getByRole("alert").filter({ hasText: "direct messages to this person are unavailable" }));
    assert.equal(await conversation.getByText(/blocked/i).count(), 0, "the refusal does not reveal the block");
    await reply.getByRole("button", { name: "Unblock", exact: true }).click();
    await visible(reply.getByRole("button", { name: "Block", exact: true }));
    assert.deepEqual(pageErrors, []);
    context.diagnostic("Verified People-initiated direct messages, unread navigation, reading, replying, muting and blocking.");
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
