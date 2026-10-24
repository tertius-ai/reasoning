// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0

/**
 * The "therefore" mark's three dots, in the 32-unit square of the tab icon (web/public/reasoning.svg draws the same
 * circles). They are drawn rather than typed: the bundled IBM Plex Mono has no ∴, so a typed mark would take each
 * computer's own fallback font. The proportions are those of SF Mono's ∴: an equilateral triangle of equal dots whose
 * diameter is 0.35 of their spacing.
 */
export const MARK_DOTS = [[16, 7.03], [5.63, 25], [26.37, 25]] as const;
export const MARK_RADIUS = 3.63;

/** The mark alone, cropped to its dots; CSS sizes it like the 16px ∴ glyph it replaces. */
export function BrandMark() {
  return <svg className="org-brand-mark" viewBox="2 3.4 28 25.23" aria-hidden="true" focusable="false">
    {MARK_DOTS.map(([cx, cy]) => <circle key={cx} cx={cx} cy={cy} r={MARK_RADIUS} fill="currentColor" />)}
  </svg>;
}

/** The product wordmark: the therefore mark and the name in lowercase monospace. */
export function Brand() {
  return <span className="org-brand" aria-label="reasoning"><BrandMark /><span aria-hidden="true">reasoning</span></span>;
}
