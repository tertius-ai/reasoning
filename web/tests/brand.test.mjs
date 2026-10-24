// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { after, before, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

let server, brand;
before(async () => {
  server = await createServer({ appType: "custom", logLevel: "silent", server: { middlewareMode: true } });
  brand = await server.ssrLoadModule("/src/Brand.tsx");
});
after(async () => { await server?.close(); });

const circles = (svg) => [...svg.matchAll(/<circle cx="([\d.]+)" cy="([\d.]+)" r="([\d.]+)"/g)].map(match => match.slice(1).map(Number));

test("both pages' tab icons draw exactly the dots of the logo's mark", async () => {
  const logo = renderToStaticMarkup(React.createElement(brand.Brand));
  const expected = brand.MARK_DOTS.map(([cx, cy]) => [cx, cy, brand.MARK_RADIUS]);
  for (const icon of ["reasoning.svg", "favicon.svg"])
    assert.deepEqual(circles(await readFile(new URL("../public/" + icon, import.meta.url), "utf8")), expected, icon);
  assert.deepEqual(circles(logo), expected);
  assert.match(logo, /aria-label="reasoning"/);
  assert.doesNotMatch(logo, /∴/, "the mark is drawn, never typed");
});

test("both pages' tabs are titled reasoning and use the drawn icon", async () => {
  const organization = await readFile(new URL("../organization.html", import.meta.url), "utf8");
  assert.match(organization, /<title>reasoning<\/title>/);
  // Exactly /reasoning.svg: the release installer accepts no other non-asset reference (deployment/release.py).
  assert.match(organization, /<link rel="icon" type="image\/svg\+xml" href="\/reasoning\.svg" \/>/);
  const workbench = await readFile(new URL("../index.html", import.meta.url), "utf8");
  assert.match(workbench, /<title>reasoning<\/title>/);
  assert.match(workbench, /<link rel="icon" type="image\/svg\+xml" href="\/favicon\.svg\?v=\d+" \/>/);
  // The .ico fallback holds PNG renderings of the same drawing at 16, 32 and 48 pixels.
  const ico = await readFile(new URL("../public/favicon.ico", import.meta.url));
  assert.deepEqual([...ico.subarray(0, 6)], [0, 0, 1, 0, 3, 0]);
  assert.deepEqual([0, 1, 2].map(index => ico[6 + 16 * index]), [16, 32, 48]);
});
