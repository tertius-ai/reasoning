// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { realpath } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

async function browserFixture(run) {
  const root = fileURLToPath(new URL("../", import.meta.url));
  const server = await createServer({ root, server: { host: "127.0.0.1", port: 0,
    fs: { allow: [root, await realpath(root + "node_modules")] } } });
  await server.listen();
  const browser = await chromium.launch({ headless: true });
  try { await run(await browser.newPage({ viewport: { width: 1280, height: 900 } }), server.resolvedUrls.local[0]); }
  finally { await browser.close(); await server.close(); }
}
const fulfill = (route, data, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(data) });
const password = "a new private fixture password";

test("forgot password retains retry identity; reset links stay out of URLs and require matching passwords", { timeout: 60000 }, async () => {
  await browserFixture(async (page, base) => {
    const requests = [], resets = [], urls = [], errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.route("**/api/**", async route => {
      const req = route.request(), path = new URL(req.url()).pathname; urls.push(req.url());
      if (path === "/api/health") return fulfill(route, { initialized: true });
      if (path === "/api/auth/me") return fulfill(route, { detail: "sign-in required" }, 401);
      if (path === "/api/auth/password-reset" && req.method() === "GET") return fulfill(route, { enabled: true });
      if (path === "/api/auth/password-reset") {
        requests.push(req.postDataJSON());
        return requests.length === 1 ? fulfill(route, { detail: "Reply lost; try again" }, 503)
          : fulfill(route, { message: "If an account exists for this email address, a password reset link will be sent." }, 202);
      }
      if (path === "/api/auth/password-reset/confirm") {
        const body = req.postDataJSON(); resets.push(body);
        return body.token === "r".repeat(43) ? fulfill(route, { message: "Password changed." })
          : fulfill(route, { detail: "This recovery link is expired, used, or invalid. Request a new link." }, 404);
      }
      return fulfill(route, []);
    });
    await page.goto(base + "organization.html");
    await page.getByRole("button", { name: "Forgot password?", exact: true }).click();
    await page.getByLabel("Email", { exact: true }).fill("researcher@example.org");
    await page.getByRole("button", { name: "Send reset link", exact: true }).click();
    await page.getByRole("alert").filter({ hasText: "Reply lost" }).waitFor();
    await page.getByRole("button", { name: "Send reset link", exact: true }).click();
    await page.getByRole("status").filter({ hasText: "If an account exists" }).waitFor();
    assert.equal(requests.length, 2); assert.equal(requests[0].client_id, requests[1].client_id);
    await page.goto(base + "organization.html#reset=" + "r".repeat(43));
    await page.getByRole("heading", { name: "Choose a new password" }).waitFor();
    await page.getByLabel("New password", { exact: true }).fill(password);
    await page.getByLabel("Confirm new password", { exact: true }).fill("a different fixture password");
    await page.getByRole("button", { name: "Set new password" }).click();
    await page.getByRole("alert").filter({ hasText: "Passwords do not match" }).waitFor();
    assert.equal(resets.length, 0);
    await page.getByLabel("Confirm new password", { exact: true }).fill(password);
    await page.getByRole("button", { name: "Set new password" }).click();
    await page.getByRole("status").filter({ hasText: "Password changed" }).waitFor();
    assert.equal(resets.length, 1); assert.equal(resets[0].token, "r".repeat(43));
    assert.equal(new URL(page.url()).hash, "");
    assert.equal(urls.some(url => url.includes("r".repeat(43))), false);
    assert.equal(await page.evaluate(() => JSON.stringify(localStorage) + JSON.stringify(sessionStorage)).then(value => value.includes("r".repeat(43))), false);
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await page.getByRole("heading", { name: "Sign in to reasoning" }).waitFor();
    await page.goto(base + "organization.html#reset=" + "x".repeat(43));
    await page.getByLabel("New password", { exact: true }).fill(password);
    await page.getByLabel("Confirm new password", { exact: true }).fill(password);
    await page.getByRole("button", { name: "Set new password" }).click();
    await page.getByRole("alert").filter({ hasText: "expired, used, or invalid" }).waitFor();
    await page.getByRole("button", { name: "Request a new recovery link" }).click();
    await page.getByRole("heading", { name: "Reset your password" }).waitFor();
    assert.deepEqual(errors, []);
  });
});

test("unusable invitations give next steps; an existing account can recover its password without recreating it", { timeout: 60000 }, async () => {
  await browserFixture(async (page, base) => {
    let state = "used", existing = false, acceptCalls = 0;
    const errors = []; page.on("pageerror", error => errors.push(error.message));
    await page.route("**/api/**", async route => {
      const req = route.request(), path = new URL(req.url()).pathname;
      if (path === "/api/health") return fulfill(route, { initialized: true });
      if (path === "/api/auth/me") return fulfill(route, { detail: "sign-in required" }, 401);
      if (path === "/api/invitations/inspect") return fulfill(route, state === "pending"
        ? { state, email: "invited@example.org", existing_account: existing } : { state });
      if (path === "/api/invitations/accept") { acceptCalls++; return fulfill(route, {}); }
      if (path === "/api/auth/password-reset") return fulfill(route, { enabled: true });
      return fulfill(route, []);
    });
    for (const next of ["used", "expired", "revoked", "invalid"]) {
      state = next; await page.goto(base + "organization.html#invite=" + "i".repeat(43));
      await page.getByRole("status").filter({ hasText: next === "used" ? "already been used" : next }).waitFor();
      assert.equal(await page.getByLabel("Choose a password", { exact: true }).count(), 0);
      assert.equal(await page.getByRole("button", { name: "Accept invitation" }).count(), 0);
    }
    state = "pending"; existing = true;
    await page.goto(base + "organization.html#invite=" + "i".repeat(43));
    await page.getByRole("status").filter({ hasText: "already have an account" }).waitFor();
    await page.getByRole("button", { name: "Forgot password?", exact: true }).click();
    assert.equal(await page.getByLabel("Email", { exact: true }).inputValue(), "invited@example.org");
    assert.equal(acceptCalls, 0);
    state = "pending"; existing = false;
    await page.goto(base + "organization.html#invite=" + "i".repeat(43));
    await page.getByLabel("Choose a password", { exact: true }).waitFor();
    assert.equal(new URL(page.url()).hash, "");
    await page.getByLabel("Your name", { exact: true }).fill("Invited researcher");
    await page.getByLabel("Choose a password", { exact: true }).fill(password);
    await page.getByLabel("Confirm password", { exact: true }).fill("a different fixture password");
    await page.getByRole("button", { name: "Accept invitation" }).click();
    await page.getByRole("alert").filter({ hasText: "Passwords do not match" }).waitFor();
    assert.equal(acceptCalls, 0); assert.deepEqual(errors, []);
  });
});

test("administrators search thirty members and invitations, safely retry recovery mail, and manage their own sessions", { timeout: 60000 }, async () => {
  await browserFixture(async (page, base) => {
    const user = "a".repeat(32), org = "b".repeat(32), errors = [], recoveryRequests = [];
    const members = [{ id: user, name: "Owner", email: "owner@example.org", role: "owner" }, ...Array.from({ length: 30 }, (_, i) =>
      ({ id: "c" + i.toString(16).padStart(31, "0"), name: "Researcher " + i, email: `researcher${i}@example.org`, role: "member" }))];
    const invitations = [{ id: "d".repeat(32), email: "pending@example.org", role: "member", expires_at: 2_000_000_000,
      accepted_at: null, revoked_at: null, email_state: "accepted" }, { id: "e".repeat(32), email: "old@example.org", role: "member",
      expires_at: 2_000_000_000, accepted_at: null, revoked_at: 1_700_000_000, email_state: "cancelled" }];
    const recoveries = [], passwordChanges = []; let sessionRevocations = 0, authenticated = true;
    page.on("pageerror", error => errors.push(error.message));
    page.on("dialog", dialog => void dialog.accept());
    await page.route("**/api/**", async route => {
      const req = route.request(), path = new URL(req.url()).pathname; let data = [];
      if (path === "/api/health") data = { initialized: true };
      else if (path === "/api/auth/me") return authenticated
        ? fulfill(route, { id: user, name: "Owner", email: "owner@example.org" }) : fulfill(route, { detail: "sign-in required" }, 401);
      else if (path === "/api/organizations") data = [{ id: org, name: "Research", role: "owner" }];
      else if (path.endsWith("/hierarchy")) data = { projects: [], channels: [], topics: [], topic_state: [] };
      else if (path.endsWith("/members")) data = members;
      else if (path.endsWith("/presence")) data = { humans: [], bots: [] };
      else if (path.endsWith("/navigation-usage")) data = { projects: {}, channels: {}, topics: {} };
      else if (path.endsWith("/user-administration")) data = { members, invitations, mail: { enabled: true, worker_running: true }, recoveries, events: [] };
      else if (path.endsWith("/password-reset")) {
        recoveryRequests.push(req.postDataJSON());
        if (recoveryRequests.length === 1) return fulfill(route, { detail: "Reply lost; try again" }, 503);
        recoveries.push({ user_id: members[30].id, state: "queued", created_at: 1_790_000_000 }); data = { message: "Requested" };
      } else if (path === "/api/auth/email-verification") data = { enabled: true, email_verified_at: 1, delivery: null };
      else if (path === "/api/auth/sessions/revoke-others") { sessionRevocations++; data = { revoked_sessions: 2 }; }
      else if (path === "/api/auth/password") {
        passwordChanges.push(req.postDataJSON()); authenticated = false;
        data = { message: "Password changed. Sign in with your new password." };
      }
      return fulfill(route, data);
    });
    await page.goto(base + "organization.html");
    const openMenu = async () => {
      const toggle = page.getByLabel("Toggle project navigation", { exact: true }); await toggle.waitFor();
      if (await toggle.getAttribute("aria-expanded") === "false") await toggle.click();
      await page.getByRole("button", { name: "Account menu", exact: true }).click();
    };
    await openMenu(); await page.getByRole("button", { name: "Administration", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "Research administration", exact: true });
    const search = dialog.getByLabel("Search users and invitations", { exact: true });
    await search.fill("researcher29@example.org");
    await dialog.getByText("Researcher 29", { exact: true }).waitFor();
    assert.equal(await dialog.locator(".org-admin-list li").count(), 1);
    await dialog.getByRole("button", { name: "Send password reset", exact: true }).click();
    await dialog.getByRole("alert").filter({ hasText: "Reply lost" }).waitFor();
    await dialog.getByRole("button", { name: "Send password reset", exact: true }).click();
    await dialog.getByRole("status").filter({ hasText: "Recovery email requested" }).waitFor();
    assert.equal(recoveryRequests.length, 2); assert.equal(recoveryRequests[0].client_id, recoveryRequests[1].client_id);
    await search.fill("pending@example.org");
    await dialog.getByText("member · Pending", { exact: true }).waitFor();
    assert.equal(await dialog.locator(".org-admin-list li").count(), 1);
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await dialog.evaluate(node => node.scrollWidth <= node.clientWidth + 1), true);
    await search.fill(""); await dialog.getByLabel("Show users and invitations", { exact: true }).selectOption("revoked");
    await dialog.getByText("old@example.org", { exact: true }).waitFor();
    assert.equal(await dialog.getByRole("button", { name: "Revoke", exact: true }).count(), 0);
    await dialog.getByRole("button", { name: "Close dialog", exact: true }).click();
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.locator("#org-project-navigation").waitFor({ state: "visible" });
    await openMenu(); await page.getByRole("button", { name: "User information", exact: true }).click();
    await page.getByRole("button", { name: "Sign out other sessions", exact: true }).click();
    await page.getByRole("status").filter({ hasText: "other sessions have been signed out" }).waitFor();
    assert.equal(sessionRevocations, 1);
    await page.getByLabel("Current password", { exact: true }).fill("the old fixture password");
    await page.getByLabel("New password", { exact: true }).fill(password);
    await page.getByLabel("Confirm new password", { exact: true }).fill(password);
    await page.getByRole("button", { name: "Change password and sign out", exact: true }).click();
    await page.getByRole("heading", { name: "Sign in to reasoning" }).waitFor();
    await page.getByRole("status").filter({ hasText: "Sign in with your new password" }).waitFor();
    assert.deepEqual(passwordChanges, [{ current_password: "the old fixture password", password }]);
    assert.deepEqual(errors, []);
  });
});
