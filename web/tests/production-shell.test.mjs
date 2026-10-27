import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const source = (path) =>
  readFile(new URL(`../src/${path}`, import.meta.url), "utf8");

test("uses a chat-first split shell with adjacent desktop panes", async () => {
  const [app, appearance, css] = await Promise.all([
    source("App.tsx"),
    source("appearance.ts"),
    source("styles.css"),
  ]);

  assert.match(app, /PRODUCTION_SHELL = "chat-first"/);
  assert.match(app, /explorerOpen: true/);
  assert.match(app, /ledgerOpen: false/);
  assert.match(app, /className="app app-shell-chat-first" data-shell={PRODUCTION_SHELL}/);
  assert.match(app, /Ask, open, or run anything…/);
  assert.match(appearance, /swatches: \["#101112", "#1a1c1e", "#70c5ad"\]/);
  assert.match(css, /\.appbar-main\s*{[^}]*grid-template-columns:\s*minmax\(0, 1fr\) minmax\(18rem, 32rem\) minmax\(0, 1fr\)/s);
  assert.match(css, /\.activity-bar\s*{[^}]*width:\s*3\.3rem[^}]*background:\s*#090a0b/s);
  assert.match(css, /\.sidebar-col\s*{[^}]*position:\s*relative[^}]*width:\s*var\(--explorer-width, 15rem\)/s);
  assert.match(css, /\.ledger-col\s*{[^}]*position:\s*relative[^}]*width:\s*30rem/s);
  assert.doesNotMatch(css, /\.app-shell-chat-first \.sidebar-col\s*{[^}]*position:\s*absolute/s);
  assert.doesNotMatch(css, /\.app-shell-chat-first \.ledger-col[^}]*{[^}]*position:\s*absolute/s);
  assert.match(css, /@media \(max-width: 768px\)[\s\S]*\.sidebar-col\s*{[^}]*position:\s*absolute/s);
  assert.match(css, /@media \(max-width: 1180px\)[\s\S]*\.ledger-overlay \.ledger-col[^}]*position:\s*absolute/s);
  assert.match(css, /\.sidebar-resizer\s*{[^}]*cursor:\s*col-resize/s);
  assert.match(css, /\.ledger-resizer\s*{[^}]*cursor:\s*col-resize/s);
  assert.match(css, /\.chat-inner\s*{[^}]*width:\s*min\(48rem, 100%\)/s);
  assert.match(css, /\.chat-input-box\s*{[^}]*width:\s*min\(48rem, 100%\)/s);
  // A sent message is a right-aligned bubble, not a ruled block.
  assert.match(css, /\.msg-user\s*{[^}]*align-self:\s*flex-end/s);
  assert.match(css, /\.msg-user\s*{[^}]*max-width:\s*75%/s);
  assert.match(
    css,
    /\.msg-user\s*{[^}]*border-radius:\s*var\(--radius-composer\) 0\.375rem var\(--radius-composer\)/s,
  );
  assert.match(css, /--radius-composer:\s*1\.25rem/);
  // The app bar keeps one line down to the 920px minimum: the command field
  // is the track that gives way, not the wordmark.
  assert.match(css, /\.appbar \.title\s*{[^}]*white-space:\s*nowrap/s);
  assert.match(
    css,
    /@media \(max-width: 1180px\)[\s\S]*?\.appbar-main\s*{[^}]*grid-template-columns:\s*minmax\(12rem, 1fr\) minmax\(16rem, 22rem\)/s,
  );
});

test("sizes composer controls from the chat pane instead of the viewport", async () => {
  const css = await source("styles.css");

  assert.match(css, /\.chat-col\s*{[^}]*container:\s*chat-surface\s*\/\s*inline-size/s);
  assert.match(css, /@container chat-surface \(max-width: 50rem\)[^{]*{\s*\.run-configuration-controls/s);
  assert.match(css, /@container chat-surface \(max-width: 36rem\)[^{]*{\s*\.run-configuration-controls/s);
  assert.match(css, /@container chat-surface \(max-width: 28rem\)[^{]*{\s*\.run-configuration-controls/s);
  assert.doesNotMatch(css, /@media[^}]+\.run-configuration-controls/s);
});

test("defers secondary workbench surfaces behind recoverable boundaries", async () => {
  const [app, boundary, pricePicker] = await Promise.all([
    source("App.tsx"),
    source("AsyncSurface.tsx"),
    source("PriceModelPickerDialog.tsx"),
  ]);

  for (const surface of [
    "Artifact",
    "Bibliography",
    "PriceBrowser",
    "WorkflowStudio",
  ]) {
    assert.match(app, new RegExp(`import\\(\"\\./${surface}\"\\)`));
  }
  assert.match(app, /<AsyncSurface label=/);
  assert.match(pricePicker, /lazy\(\(\) =>\s*import\("\.\/PriceBrowser"\)/s);
  assert.match(pricePicker, /<AsyncSurface label="model prices">/);
  assert.match(boundary, /getDerivedStateFromError/);
  assert.match(boundary, /Your project data is unchanged\./);
  assert.match(boundary, /<Suspense fallback=/);
});

test("defers rich-text parsing until a transcript needs it", async () => {
  const [markdown, renderer] = await Promise.all([
    source("Markdown.tsx"),
    source("MarkdownRenderer.tsx"),
  ]);

  assert.match(markdown, /import\("\.\/MarkdownRenderer"\)/);
  assert.match(markdown, /Rich-text rendering failed; showing plain text/);
  assert.doesNotMatch(markdown, /from "react-markdown"/);
  // rehypeMath, which wraps rehype-katex, lives in markdownMath.
  assert.doesNotMatch(markdown, /from "\.\/markdownMath"/);
  assert.match(renderer, /from "react-markdown"/);
  assert.match(renderer, /rehypePlugins=\{\[rehypeMath\]\}/);
  assert.match(renderer, /remarkPlugins=/);
});

test("keeps chat utility controls native and named", async () => {
  const chat = await source("Chat.tsx");

  assert.match(chat, /<button\s+type="button"\s+className="load-earlier"/s);
  assert.match(chat, /className="attach-btn"[^>]+type="button"[^>]+aria-label="Attach files"/s);
  assert.match(chat, /className="jump-bottom"[^>]+aria-label="Jump to the latest message"/s);
});

test("uses stacked accessible confirmations for consequential actions", async () => {
  const [app, confirmation, modal, settings] = await Promise.all([
    source("App.tsx"),
    source("ConfirmationDialog.tsx"),
    source("useModalDialog.ts"),
    source("Settings.tsx"),
  ]);

  assert.doesNotMatch(`${app}${settings}`, /window\.confirm/);
  assert.match(confirmation, /role="alertdialog"/);
  assert.match(confirmation, /aria-describedby="confirm-dialog-message"/);
  assert.match(settings, /title: `Run setup for \$\{workspace\.name\}\?`/);
  assert.match(modal, /modalStack\.at\(-1\) !== ref/);
  assert.match(modal, /!ref\.current\.contains\(document\.activeElement\)/);
});
