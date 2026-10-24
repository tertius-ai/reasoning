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

test("the tab icon draws exactly the dots of the logo's mark", async () => {
  const icon = await readFile(new URL("../public/reasoning.svg", import.meta.url), "utf8");
  const logo = renderToStaticMarkup(React.createElement(brand.Brand));
  const expected = brand.MARK_DOTS.map(([cx, cy]) => [cx, cy, brand.MARK_RADIUS]);
  assert.deepEqual(circles(icon), expected);
  assert.deepEqual(circles(logo), expected);
  assert.match(logo, /aria-label="reasoning"/);
  assert.doesNotMatch(logo, /∴/, "the mark is drawn, never typed");
});

test("the organization page's tab is titled reasoning and uses the drawn icon", async () => {
  const page = await readFile(new URL("../organization.html", import.meta.url), "utf8");
  assert.match(page, /<title>reasoning<\/title>/);
  assert.match(page, /<link rel="icon" type="image\/svg\+xml" href="\/reasoning\.svg\?v=\d+" \/>/);
});
