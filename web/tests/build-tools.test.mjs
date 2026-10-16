import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import checks from '../check-node.cjs';

const range = JSON.parse(readFileSync(new URL('../package.json', import.meta.url))).engines.node;

test('dependency-free Node preflight follows the frontend engine range', () => {
  for (const version of ['18.19.1', '20.18.9', '21.7.3', '22.11.9', 'not-a-version', '24.0.0-rc.1']) {
    assert.equal(checks.supportsNode(version, range), false, version);
  }
  for (const version of ['20.19.0', '20.19.1', '22.12.0', 'v24.15.0', '26.0.0']) {
    assert.equal(checks.supportsNode(version, range), true, version);
  }
});

test('Node preflight fails closed for unsupported engine syntax', () => {
  assert.throws(() => checks.supportsNode('24.15.0', '~24.0.0'));
});

test('entry pages leave their script policy to the serving response', () => {
  for (const page of ['index.html', 'organization.html']) {
    const html = readFileSync(new URL(`../${page}`, import.meta.url), 'utf8');
    // A <meta> policy cannot enforce frame-ancestors, and the servers already
    // send the policy as a header.
    assert.doesNotMatch(html, /http-equiv=["']?Content-Security-Policy/i, page);
    for (const tag of html.match(/<script\b[^>]*>/gi) ?? []) {
      assert.match(tag, /\bsrc=/, `${page}: ${tag}`);
    }
  }
  // The production build fails if an emitted page contains inline script.
  const config = readFileSync(new URL('../vite.config.ts', import.meta.url), 'utf8');
  assert.match(config, /name: "reasoning-no-inline-scripts",\s*enforce: "post"/);
});
