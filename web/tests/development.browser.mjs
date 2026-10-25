import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { access } from 'node:fs/promises';
import net from 'node:net';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { chromium } from 'playwright';

const repository = fileURLToPath(new URL('../../', import.meta.url));
async function freePort() {
  const listener = net.createServer();
  listener.listen(0, '127.0.0.1');
  await once(listener, 'listening');
  const port = listener.address().port;
  await new Promise(resolve => listener.close(resolve));
  return port;
}

test('local hot-reload workflow signs in, streams a simulated beta answer, and cleans up', { timeout: 60000 }, async () => {
  const frontend = await freePort();
  let backend;
  do { backend = await freePort(); } while (backend === frontend);
  const origin = `http://127.0.0.1:${frontend}`;
  let logs = '', browser, page;
  const server = spawn(path.join(repository, '.venv/bin/python'), ['-m', 'reasoning_ledger.development', 'serve',
    '--frontend-port', String(frontend), '--backend-port', String(backend)],
  // Without the simulated development bot, which advertises local execution; tests/bot-direct.browser.mjs covers it.
  { cwd: repository, env: { ...process.env, REASONING_DEV_BOT: '0' } });
  server.stdout.on('data', chunk => { logs += chunk; });
  server.stderr.on('data', chunk => { logs += chunk; });
  const exited = once(server, 'exit');
  try {
    const deadline = Date.now() + 20000;
    let ready = false;
    while (Date.now() < deadline && server.exitCode === null) {
      try {
        const response = await fetch(origin + '/api/health');
        ready = response.ok && response.headers.get('X-Reasoning-Development') === 'simulated-no-charges';
        if (ready) break;
      } catch { /* Startup retry, not a provider request. */ }
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    assert.ok(ready, logs);
    browser = await chromium.launch({ headless: true });
    page = await browser.newPage();
    const errors = [];
    const messageIds = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => {
      if (request.method() === 'POST' && /\/topics\/[^/]+\/messages$/.test(request.url())) {
        messageIds.push(request.postDataJSON().client_id);
      }
    });
    page.on('response', async response => {
      if (response.status() >= 400 && response.url().includes('/api/') && !response.url().endsWith('/api/auth/me')) {
        console.error('Fixture API failure:', response.status(), response.url(), await response.text());
      }
    });
    await page.goto(origin + '/organization.html');
    await page.getByLabel('Email', { exact: true }).fill('owner@example.test');
    await page.getByLabel('Password', { exact: true }).fill('Reasoning local development only');
    await page.getByRole('button', { name: 'Sign in', exact: true }).click();
    await page.getByRole('button', { name: /Simulated research/ }).first().click();
    await page.getByPlaceholder('Message development/fast…', { exact: true }).waitFor();
    assert.equal(Math.round((await page.locator('.org-appbar').boundingBox()).height), 44);
    assert.equal(await page.locator('.org-organization-name').innerText(), 'Local development');
    assert.match(await page.getByRole('navigation', { name: 'Current topic' }).innerText(), /#playground.*Simulated research/s);
    assert.equal(await page.locator('.org-topic-header').count(), 0);
    assert.equal(await page.getByRole('button', { name: /Pin.*research context/i }).count(), 0);
    assert.equal(await page.locator('.org-composer').getByRole('button', { name: 'Ledger', exact: true }).count(), 0);
    await page.getByLabel('Topic playback', { exact: true }).waitFor();
    assert.equal(await page.getByLabel('Topic playback', { exact: true }).getByRole('button').count(), 1, 'one button in the box');
    await page.getByRole('button', { name: 'Toggle project navigation' }).click();
    assert.equal(await page.getByRole('navigation', { name: 'Projects and channels' }).isVisible(), false);
    await page.getByRole('button', { name: 'Toggle project navigation' }).click();
    const composer = page.getByLabel('Message', { exact: true });
    const command = async id => {
      await composer.fill('/' + id);
      await page.getByRole('listbox', { name: 'Topic commands' }).waitFor();
      await composer.press('Enter');
      assert.equal(await composer.inputValue(), '', 'commands do not become human or provider messages');
    };
    await command('tex');
    const tex = page.getByRole('dialog', { name: 'Compile TeX to PDF', exact: true });
    await tex.waitFor();
    await tex.getByText('No connected bot advertises local execution.', { exact: false }).waitFor();
    assert.equal(await tex.getByRole('button', { name: 'Prepare bot message' }).isDisabled(), true);
    await tex.getByRole('button', { name: 'Close compile tex to pdf', exact: true }).click();
    assert.equal(messageIds.length, 0, 'opening the TeX editor does not enqueue inference');
    const chooser = page.waitForEvent('filechooser');
    await command('attach');
    await chooser;
    await composer.fill('/le');
    await page.getByRole('listbox', { name: 'Topic commands' }).waitFor();
    await composer.press('Escape');
    assert.equal(await page.getByRole('listbox', { name: 'Topic commands' }).count(), 0);
    assert.equal(await composer.inputValue(), '/le');
    const small = (await composer.boundingBox()).height;
    assert.ok(small < 60);
    assert.equal(await composer.evaluate(element => getComputedStyle(element).resize), 'none');
    await composer.fill(Array(18).fill('The composer grows automatically.').join('\n'));
    assert.ok((await composer.boundingBox()).height > small * 2);
    await composer.fill('');
    assert.equal((await composer.boundingBox()).height, small);
    await page.getByLabel('Message', { exact: true }).fill('Exercise the local development streaming path.');
    await composer.press('Tab');
    assert.equal(await page.getByRole('button', { name: 'Send', exact: true }).evaluate(element => element === document.activeElement), true);
    await page.keyboard.press('Enter');
    await page.locator('.org-message').filter({ hasText: 'Simulated development response.' }).waitFor({ timeout: 20000 });
    // The system prompt opens from the topic's details card in the top bar, in a dialog; the card shows its version.
    const systemPrompt = async () => {
      await page.locator('.org-topic-menu > summary').click();
      await page.getByRole('group', { name: 'Topic details', exact: true }).getByRole('button', { name: /^System prompt v\d+$/ }).click();
    };
    await systemPrompt();
    await page.getByText(/Ordinary questions and discussion need a useful reply/).waitFor();
    await page.waitForTimeout(2200); // Polling must not close or replace the open dialog.
    assert.ok(await page.getByRole('dialog', { name: 'System prompt', exact: true }).locator('.org-system-message').isVisible());
    await page.getByRole('button', { name: 'Close dialog', exact: true }).click();
    const history = page.getByLabel('Message history', { exact: true });
    // A short transcript can fit after prompt expansion. The jump control is
    // intentionally absent at the bottom; establish a real scrolled state
    // before exercising it rather than depending on viewport/font geometry.
    if (await history.evaluate(element => element.scrollHeight - element.clientHeight > 64)) {
      await history.evaluate(element => { element.scrollTop = 0; element.dispatchEvent(new Event('scroll', { bubbles: true })); });
      await page.getByRole('button', { name: 'Jump to newest message' }).click();
    }
    await command('tools');
    assert.equal(await page.getByLabel('Fresh-context review', { exact: true }).isChecked(), true);
    assert.ok(await page.getByRole('button', { name: 'Reviewer override', exact: true }).filter({ hasText: 'Automatic reviewer' }).isVisible());
    await page.getByLabel('Research strategy', { exact: true }).selectOption('blueprint');
    await page.getByLabel('Additional research instructions', { exact: true }).fill('Keep the test hypotheses exact.');
    await page.getByRole('button', { name: 'Save tools', exact: true }).click();
    await systemPrompt();
    await page.getByText(/Maintain one evolving proof blueprint/).waitFor();
    await page.locator('.org-system-message').getByText(/Keep the test hypotheses exact/).waitFor();
    await page.getByRole('button', { name: 'Close dialog', exact: true }).click();
    await page.context().grantPermissions(['clipboard-read', 'clipboard-write']);
    const copyTopic = async kind => {
      await page.getByRole('button', { name: 'Copy topic JSON', exact: true }).click();
      await page.locator('.org-copy-topic[data-copied="true"]').waitFor();
      const text = await page.evaluate(() => navigator.clipboard.readText());
      const data = JSON.parse(text);
      assert.equal(data.schema, 'reasoning.topic-summary.v2');
      assert.ok(text.split('\n').length < 100);
      assert.equal('captured_contexts' in data, false);
      assert.equal(data.topic.kind, kind);
      assert.ok(data.messages.length);
      assert.equal(await page.getByText('Topic JSON copied.', { exact: true }).count(), 0);
      assert.equal(await page.getByText(/Export includes private/).count(), 0);
      return data;
    };
    const beta = await copyTopic('beta');
    assert.ok(beta.current_instructions.system_prompt.includes('Ordinary questions and discussion need a useful reply'));
    await page.getByRole('button', { name: 'Copy topic JSON', exact: true }).click({ modifiers: ['Shift'] });
    await page.locator('.org-copy-topic[data-copied="true"]').waitFor();
    const trace = JSON.parse(await page.evaluate(() => navigator.clipboard.readText()));
    assert.equal(trace.schema, 'reasoning.topic-trace.v2');
    const decode = item => {
      if (Array.isArray(item)) return item.map(decode);
      if (item && typeof item === 'object') {
        if ('$ref' in item) return decode(trace.blobs[item.$ref]);
        if ('$object' in item) return Object.fromEntries(item.$object.map(([key, child]) => [key, decode(child)]));
        return Object.fromEntries(Object.entries(item).map(([key, child]) => [key, decode(child)]));
      }
      return item;
    };
    const full = decode(trace.data);
    assert.deepEqual(full.messages.map(message => message.content), beta.messages.map(message => message.content));
    assert.ok(full.captured_contexts.length && full.runs[0].latest_session);
    await page.getByRole('button', { name: 'Choose reply model', exact: true }).click();
    const picker = page.locator('.org-shared-model-picker[open]');
    await picker.getByRole('radio', { name: 'development/slow', exact: true }).click();
    assert.ok(await page.getByRole('button', { name: 'Choose reply model', exact: true }).filter({ hasText: 'development/slow' }).isVisible());
    await page.getByRole('button', { name: 'Choose reply model', exact: true }).click();
    await picker.getByRole('radio', { name: 'development/fast', exact: true }).click();
    await command('goal');
    await page.getByRole('button', { name: 'Goal model', exact: true }).click();
    assert.ok(await page.getByRole('dialog', { name: 'Start a research goal' }).locator('.org-model-line').first().isVisible());
    await page.keyboard.press('Escape'); // Close the picker without dismissing the goal dialog.
    await page.getByRole('button', { name: 'Close goal', exact: true }).click();
    await page.getByRole('button', { name: /Human chat/ }).first().click();
    await copyTopic('alpha');
    await composer.fill('@Development');
    await page.getByRole('listbox', { name: 'Mention a member' }).waitFor();
    await composer.press('Tab');
    assert.equal(await composer.evaluate(element => element === document.activeElement), true);
    assert.match(await composer.inputValue(), /^@Development.+ $/);
    await page.keyboard.press('Enter');
    await page.locator('.org-message').filter({ hasText: '@Development' }).waitFor();
    await page.getByRole('button', { name: /Workflow playground/ }).first().click();
    await composer.fill('Keep the workflow discussion with its runs.');
    await composer.press('Tab'); await page.keyboard.press('Enter');
    await page.locator('.org-message').filter({ hasText: 'Keep the workflow discussion with its runs.' }).waitFor();
    await copyTopic('gamma');
    await command('compare');
    await page.getByRole('button', { name: 'Model 1', exact: true }).click();
    assert.ok(await page.locator('.org-workflow-editor .org-shared-model-picker[open] .org-model-line').first().isVisible());
    assert.equal(await page.locator('.org-workflow-editor .org-shared-model-picker[open]').getByRole('radio', { name: 'development/slow', exact: true }).count(), 0);
    await page.keyboard.press('Escape');
    await page.getByRole('dialog', { name: 'Compare models', exact: true }).getByLabel('Question', { exact: true }).fill('Compare this synthetic development fixture.');
    await page.getByRole('button', { name: 'Run comparison', exact: true }).click();
    await page.locator('.org-run-message').first().waitFor();
    await page.locator('.org-run-message').getByText('Simulated development response.', { exact: true }).first().waitFor({ timeout: 20000 });
    await page.waitForFunction(() => document.querySelector('.org-comparison-message')?.dataset.status === 'succeeded');
    assert.equal(await page.getByLabel('Workflow run status', { exact: true }).count(), 0);
    const gammaTrace = await copyTopic('gamma');
    assert.equal(gammaTrace.messages.filter(message => message.workflow_run_id === gammaTrace.workflow_runs[0].id).length, 2);
    assert.equal(await page.locator('[data-activity-id^="message:"]').filter({ hasText: 'Simulated development response.' }).count(), 0);
    assert.equal(await page.locator('.org-workflow-list, .org-workflow-run-list, .org-compute-jobs').count(), 0);
    if (process.env.REASONING_SCREENSHOT_DIR) await page.screenshot({ path: path.join(process.env.REASONING_SCREENSHOT_DIR, 'gamma-stream.png') });
    await page.evaluate(() => Object.defineProperty(navigator.clipboard, 'writeText', { configurable: true, value: async () => { throw new Error('Clipboard unavailable'); } }));
    await page.getByRole('button', { name: 'Copy topic JSON', exact: true }).click();
    const fallback = page.getByRole('dialog', { name: 'Topic JSON' });
    await fallback.waitFor();
    assert.equal(JSON.parse(await fallback.getByLabel('Topic diagnostic JSON').inputValue()).topic.kind, 'gamma');
    const downloaded = page.waitForEvent('download');
    await fallback.getByRole('button', { name: 'Download JSON' }).click();
    assert.match((await downloaded).suggestedFilename(), /^reasoning-topic-.*\.json$/);
    await fallback.getByRole('button', { name: 'Close JSON' }).click();
    await page.getByRole('button', { name: 'Account menu', exact: true }).click();
    await page.getByRole('button', { name: 'Model capabilities', exact: true }).click();
    const capabilities = page.getByRole('dialog', { name: 'Model capabilities' });
    await capabilities.locator('.org-model-card').first().waitFor();
    await capabilities.locator('.org-model-card > summary').first().click();
    await capabilities.locator('.org-model-card[open] .org-model-detail').first().waitFor();
    if (process.env.REASONING_SCREENSHOT_DIR) await page.screenshot({ path: path.join(process.env.REASONING_SCREENSHOT_DIR, 'model-capabilities.png') });
    await capabilities.getByRole('button', { name: 'Close dialog', exact: true }).click();
    await page.getByRole('button', { name: /Simulated research/ }).first().click();
    await page.getByRole('heading', { name: 'β-Simulated research', exact: true }).waitFor();
    await page.getByRole('link', { name: 'Open ledger', exact: true }).click();
    const ledger = page;
    await ledger.getByRole('heading', { name: 'Ledger', exact: true }).waitFor();
    assert.match(ledger.url(), /view=ledger/);
    await ledger.getByRole('button', { name: 'Record statement', exact: true }).click();
    const longStatement = Array(75).fill('This is an interface fixture, not a mathematical assertion.').join('\n\n');
    await ledger.getByLabel('Precise statement', { exact: true }).fill(longStatement);
    await ledger.getByRole('button', { name: 'Save statement', exact: true }).click();
    await ledger.getByRole('button', { name: 'Zoom in', exact: true }).waitFor();
    await ledger.locator('.graph-node').first().waitFor();
    await ledger.locator('.org-mathematics article').getByRole('heading', { name: 'Proof attempts', exact: true }).waitFor();
    await ledger.locator('.org-mathematics article').getByText('No proof attempts recorded.', { exact: true }).waitFor();
    assert.ok(await ledger.locator('.org-ledger-inspector').evaluate(element => element.scrollHeight > element.clientHeight));
    await ledger.locator('.org-ledger-inspector').evaluate(element => { element.scrollTop = element.scrollHeight; });
    assert.ok(await ledger.locator('.org-ledger-inspector').evaluate(element => element.scrollTop > 0));
    if (process.env.REASONING_SCREENSHOT_DIR) await ledger.screenshot({ path: path.join(process.env.REASONING_SCREENSHOT_DIR, 'ledger-workspace.png') });
    await ledger.reload();
    await ledger.getByRole('heading', { name: 'Ledger', exact: true }).waitFor();
    await page.getByRole('button', { name: /Simulated research/ }).first().click();
    const orgs = await page.evaluate(async () => (await fetch('/api/organizations')).json());
    const human = await page.evaluate(async () => (await fetch('/api/auth/me')).json());
    const tree = await page.evaluate(async org => (await fetch(`/api/organizations/${org}/hierarchy`)).json(), orgs[0].id);
    const botBetaTopic = tree.topics.find(topic => topic.kind === 'beta');
    const botToken = 'rnb_' + 'b'.repeat(43), device = 'a'.repeat(43), code = 'ABCDEFGH2345';
    const connection = await fetch(origin + '/api/bot/connect', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ device_secret: device, user_code: code, credential_hash: createHash('sha256').update(botToken).digest('hex'),
        name: 'Fixture Codex', engine: 'codex' }) });
    assert.equal(connection.status, 200);
    await page.goto(origin + '/organization.html?bot_connect=' + code);
    const pairing = page.getByRole('dialog', { name: 'My bots', exact: true });
    await pairing.getByRole('button', { name: 'Approve connection', exact: true }).waitFor();
    assert.equal(await pairing.getByLabel('Approve in organization').inputValue(), orgs[0].id);
    assert.ok(await pairing.getByLabel('Follow my access, including private projects and new topics', { exact: true }).isChecked());
    assert.equal(await pairing.locator(`input[type="checkbox"][value="${botBetaTopic.id}"]`).count(), 0,
      'following access does not show redundant effective topic lists');
    await pairing.getByRole('button', { name: 'Approve connection', exact: true }).click();
    await pairing.waitFor({ state: 'hidden' });
    const bot = await (await fetch(origin + '/api/bot/identity', { headers: { Authorization: 'Bearer ' + botToken } })).json();
    assert.equal(bot.public_access, true); assert.ok(bot.topic_ids.includes(botBetaTopic.id));
    await page.locator(`[data-bot-id="${bot.id}"][data-owner-id="${human.id}"]`).waitFor();
    await page.getByRole('button', { name: /Simulated research/ }).first().click();
    await composer.fill('@Fixture');
    await page.getByRole('option', { name: 'Fixture Codex', exact: true }).waitFor({ timeout: 15000 });
    await composer.press('Enter');
    assert.equal(await composer.inputValue(), '@Fixture Codex ');
    const botInference = [];
    const watchBotInference = request => { if (request.method() === 'POST' && request.url().endsWith('/inference-runs')) botInference.push(request.url()); };
    page.on('request', watchBotInference);
    await composer.press('End');
    await composer.pressSequentially('Answer through the CLI.'); await composer.press('Enter');
    await page.locator('.org-message').filter({ hasText: '@Fixture Codex Answer through the CLI.' }).waitFor();
    await new Promise(resolve => setTimeout(resolve, 1000));
    page.off('request', watchBotInference);
    assert.deepEqual(botInference, []);
    for (const width of [1440, 920, 390, 320]) {
      await page.setViewportSize({ width, height: 844 });
      const more = page.getByLabel('More topic actions', { exact: true });
      if (width <= 600) await more.click();
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth), false, `Overflow at ${width}px`);
      assert.ok(await page.getByRole('button', { name: 'Copy topic JSON', exact: true }).isVisible());
      assert.equal(await page.locator('.org-composer').getByRole('button', { name: 'Ledger', exact: true }).count(), 0);
      assert.ok(await page.getByLabel('Topic playback', { exact: true }).isVisible());
      const search = page.getByRole('searchbox', { name: 'Search organization messages' });
      await search.focus();
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth), false, `Search overflow at ${width}px`);
      await search.blur();
      if (width <= 600) await more.click();
      await page.getByRole('button', { name: 'Choose reply model', exact: true }).click();
      const menu = page.locator('.org-shared-model-picker[open] .org-model-options');
      const bounds = await menu.boundingBox();
      assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= width, `Picker width at ${width}px`);
      assert.ok(bounds.y >= 0 && bounds.y + bounds.height <= 844, `Picker height at ${width}px`);
      const card = page.locator('.org-shared-model-picker[open] .org-model-card').first();
      if (!await card.evaluate(element => element.open)) await card.locator('.org-model-chevron').click();
      await page.locator('.org-shared-model-picker[open] .org-model-card[open] .org-model-detail').first().waitFor();
      if (process.env.REASONING_SCREENSHOT_DIR) await page.screenshot({ path: path.join(process.env.REASONING_SCREENSHOT_DIR, `model-picker-${width}.png`) });
      await page.keyboard.press('Escape');
    }
    assert.deepEqual(errors, []);
    assert.equal(messageIds.length, 4);
    for (const id of messageIds) assert.match(id, /^[0-9a-f-]{36}$/);
    assert.notEqual(messageIds[0], messageIds[1]);
    assert.match(logs, /WatchFiles/);
  } catch (error) {
    console.error('Development browser failure:', page ? (await page.locator('body').innerText()).slice(-8000) : 'no page', logs);
    throw error;
  } finally {
    if (browser) await browser.close();
    if (server.exitCode === null) server.kill('SIGTERM');
    await Promise.race([exited, new Promise((_, reject) => {
      setTimeout(() => reject(new Error('Development cleanup timed out: ' + logs)), 15000).unref();
    })]);
  }
  assert.equal(server.exitCode, 0, logs);
  const state = /State: (.+)\n/.exec(logs)?.[1];
  assert.ok(state, logs);
  await assert.rejects(access(state));
  for (const port of [frontend, backend]) {
    await assert.rejects(fetch(`http://127.0.0.1:${port}/api/health`));
  }
});
