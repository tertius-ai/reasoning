import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { mkdir, mkdtemp, readFile, realpath, rm } from "node:fs/promises";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { _electron as electron } from "playwright";
import {
  assertDesktopPlatform,
  assertDesktopProfile,
  desktopLaunchOptions,
} from "./helpers/desktop-launch.mjs";

const repository = fileURLToPath(new URL("../../", import.meta.url));
const password = "isolated desktop test password";
async function until(predicate, label) {
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    if (await predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error("Timed out: " + label);
}
async function visible(locator) {
  await locator.waitFor({ state: "visible", timeout: 20000 });
}

test(
  "source-built desktop connects two members to shared chat without native privileges",
  { timeout: 180000 },
  async (context) => {
    assertDesktopPlatform();
    const temporary = await realpath(await mkdtemp(
      path.join(os.tmpdir(), "reasoning-desktop-test-"),
    ));
    let cleanupStarted = false;
    context.after(() => {
      if (!cleanupStarted) return rm(temporary, { recursive: true, force: true });
    });
    const state = path.join(temporary, "server");
    const listener = net.createServer();
    listener.listen(0, "127.0.0.1");
    await once(listener, "listening");
    const port = listener.address().port;
    await new Promise((resolve) => listener.close(resolve));
    const origin = `http://127.0.0.1:${port}`;
    const server = spawn(
      path.join(repository, ".venv/bin/python"),
      [
        "-m",
        "reasoning_ledger.server.cli",
        "--data-dir",
        state,
        "serve",
        "--public-url",
        origin,
        "--port",
        String(port),
        "--frontend-dir",
        path.join(repository, "web/dist"),
      ],
      { cwd: repository },
    );
    let logs = "";
    server.stdout.on("data", (data) => {
      logs += data;
    });
    server.stderr.on("data", (data) => {
      logs += data;
    });
    const applications = [];
    const noSandbox = process.env.REASONING_ELECTRON_TEST_NO_SANDBOX === "1";
    if (noSandbox)
      context.diagnostic(
        "Isolated trusted-fixture run without OS sandbox; this does not verify production sandbox enforcement.",
      );
    async function start(profile) {
      const directory = path.join(temporary, profile);
      await mkdir(directory, { recursive: true, mode: 0o700 });
      const profileDirectory = await realpath(directory);
      const application = await electron.launch(desktopLaunchOptions({
        repository,
        profileDirectory,
      }));
      applications.push(application);
      await assertDesktopProfile(application, profileDirectory, { sandbox: !noSandbox });
      return application;
    }
    async function connect(application, selected = origin) {
      const picker = await application.firstWindow();
      await visible(picker.getByLabel("Server URL"));
      await picker.getByLabel("Server URL").fill(selected);
      await picker
        .getByRole("button", { name: "Connect", exact: true })
        .click();
      await until(
        () =>
          application.windows().some((page) => page.url().startsWith(selected)),
        "server window",
      );
      return application
        .windows()
        .find((page) => page.url().startsWith(selected));
    }
    async function api(route, data, token) {
      const response = await fetch(origin + "/api" + route, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(token ? { Authorization: "Bearer " + token } : {}),
        },
        body: JSON.stringify(data),
      });
      assert.ok(response.ok, `API ${route}: ${response.status}`);
      return response.json();
    }
    try {
      await until(async () => {
        try {
          return (await fetch(origin + "/api/health")).ok;
        } catch {
          return false;
        }
      }, "server readiness");
      const owner = await api("/setup", {
        setup_token: await readFile(path.join(state, "setup-token"), "utf8"),
        email: "owner@example.org",
        name: "Owner",
        password,
        organization_name: "Desktop organization",
        transport: "bearer",
      });
      const root = `/organizations/${owner.organization_id}`;
      const project = await api(
        root + "/projects",
        { name: "Algebra" },
        owner.access_token,
      );
      const channel = await api(
        root + "/channels",
        { name: "research", project_id: project.id },
        owner.access_token,
      );
      await api(
        root + "/topics",
        { name: "Discussion", kind: "alpha", channel_id: channel.id },
        owner.access_token,
      );
      for (const [email, name] of [
        ["first@example.org", "Researcher"],
        ["second@example.org", "Reviewer"],
      ]) {
        const invitation = await api(
          root + "/invitations",
          { email },
          owner.access_token,
        );
        await api("/invitations/accept", {
          token: invitation.token,
          name,
          password,
          transport: "bearer",
        });
      }
      const firstApp = await start("first");
      const first = await connect(firstApp);
      await first
        .getByLabel("Email", { exact: true })
        .fill("first@example.org");
      await first.getByLabel("Password", { exact: true }).fill(password);
      await first.getByRole("button", { name: "Sign in", exact: true }).click();
      await visible(first.getByLabel("Message", { exact: true }));
      const secondApp = await start("second");
      const second = await connect(secondApp);
      await second
        .getByLabel("Email", { exact: true })
        .fill("second@example.org");
      await second.getByLabel("Password", { exact: true }).fill(password);
      await second
        .getByRole("button", { name: "Sign in", exact: true })
        .click();
      await visible(second.getByLabel("Message", { exact: true }));
      for (const page of [first, second]) {
        assert.deepEqual(
          await page.evaluate(() => [
            typeof window.require,
            typeof window.process,
            typeof window.reasoningConnection,
            typeof window.reasoningDesktop,
          ]),
          ["undefined", "undefined", "undefined", "undefined"],
        );
      }
      const preferences = await firstApp.evaluate(({ BrowserWindow }) =>
        BrowserWindow.getAllWindows().map((window) =>
          window.webContents.getLastWebPreferences(),
        ),
      );
      assert.equal(preferences.length, 1);
      assert.equal(preferences[0].preload, undefined);
      assert.equal(preferences[0].sandbox, true);
      assert.equal(preferences[0].nodeIntegration, false);
      assert.equal(preferences[0].contextIsolation, true);
      await first
        .getByLabel("Message", { exact: true })
        .fill("Hello from a source-built desktop.");
      await first.getByRole("button", { name: "Send", exact: true }).click();
      await visible(
        second
          .locator(".org-message")
          .filter({ hasText: "Hello from a source-built desktop." }),
      );
      await second
        .getByLabel("Message", { exact: true })
        .fill("Reply from another desktop.");
      await second.getByRole("button", { name: "Send", exact: true }).click();
      await visible(
        first
          .locator(".org-message")
          .filter({ hasText: "Reply from another desktop." }),
      );
      // A remote page cannot cross the native boundary or probe a foreign network origin.
      assert.equal(
        await first.evaluate(async () => {
          try {
            await fetch("http://127.0.0.1:1/probe");
            return true;
          } catch {
            return false;
          }
        }),
        false,
      );
      await first.evaluate(() => {
        window.location.href = "reasoning-app://connection/index.html";
      });
      await new Promise((resolve) => setTimeout(resolve, 300));
      assert.ok(first.url().startsWith(origin));
      await firstApp.close();
      const reopened = await start("first");
      await until(
        () => reopened.windows().some((page) => page.url().startsWith(origin)),
        "remembered server",
      );
      const resumed = reopened
        .windows()
        .find((page) => page.url().startsWith(origin));
      await visible(resumed.getByRole("button", { name: "Account menu", exact: true }));
      await visible(
        resumed
          .locator(".org-message")
          .filter({ hasText: "Reply from another desktop." }),
      );
      const userData = await reopened.evaluate(({ app }) =>
        app.getPath("userData"),
      );
      assert.deepEqual(
        JSON.parse(
          await readFile(path.join(userData, "connection.json"), "utf8"),
        ),
        { origin },
      );
      await reopened.evaluate(({ Menu }) =>
        Menu.getApplicationMenu()
          .items.find((item) => item.label === "Connection")
          .submenu.items.find((item) => item.label === "Change server…")
          .click(),
      );
      await until(
        () =>
          reopened
            .windows()
            .some(
              (page) => page.url() === "reasoning-app://connection/index.html",
            ),
        "change-server picker",
      );
      const picker = reopened
        .windows()
        .find((page) => page.url() === "reasoning-app://connection/index.html");
      const refused = await picker.evaluate(() =>
        window.reasoningConnection.connect("http://example.org"),
      );
      assert.equal(refused.ok, false);
      assert.ok(resumed.url().startsWith(origin));
      // Native confirmation is stubbed only in the test's main-process debugger.
      await reopened.evaluate(({ dialog, Menu }) => {
        dialog.showMessageBox = async () => ({ response: 1 });
        Menu.getApplicationMenu()
          .items.find((item) => item.label === "Connection")
          .submenu.items.find((item) => item.label === "Forget server session…")
          .click();
      });
      await until(
        async () =>
          !(await reopened.evaluate(({ BrowserWindow }) =>
            BrowserWindow.getAllWindows().some((window) =>
              window.webContents.getURL().startsWith("http:"),
            ),
          )),
        "forget closes server window",
      );
      await until(async () => {
        const status = await picker.evaluate(() =>
          window.reasoningConnection.status(),
        );
        return status.origin === null;
      }, "forgotten connection");
      await picker.getByLabel("Server URL").fill(origin);
      await picker
        .getByRole("button", { name: "Connect", exact: true })
        .click();
      await until(
        () => reopened.windows().some((page) => page.url().startsWith(origin)),
        "reconnect after forgetting",
      );
      await visible(
        reopened
          .windows()
          .find((page) => page.url().startsWith(origin))
          .getByRole("heading", { name: "Sign in to reasoning" }),
      );
      context.diagnostic(
        "Verified two desktop account sessions, alpha exchange, absent native bridge, origin restrictions, restart persistence, server picker, and session forgetting.",
      );
    } finally {
      cleanupStarted = true;
      for (const application of applications.reverse())
        await application.close().catch(() => {});
      const live = () => server.exitCode === null && server.signalCode === null;
      if (live()) {
        const exited = once(server, "exit");
        server.kill("SIGTERM");
        let timer;
        await Promise.race([
          exited,
          new Promise((resolve) => {
            timer = setTimeout(resolve, 5000);
          }),
        ]);
        clearTimeout(timer);
        if (live()) {
          server.kill("SIGKILL");
          await exited;
        }
      }
      assert.ok(!logs.includes(password));
      await rm(temporary, { recursive: true, force: true });
    }
  },
);
