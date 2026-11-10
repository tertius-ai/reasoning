// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import React from "react";

/**
 * The +, × and ⋯ of the sidebar's and People's 24px header squares, drawn the size of the 14px glyphs they replace. They are
 * drawn because a font puts + and × on its math axis, below the middle of the line: as text they sat up to 0.7px low, by an
 * amount that changed with the square's sub-pixel position.
 */
export function HeaderIcon({ name }: { name: "plus" | "close" | "more" }) {
  return <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1" strokeLinecap="round"
    aria-hidden="true">
    {name === "plus" ? <path d="M6 3.1v5.8M3.1 6h5.8" />
      : name === "close" ? <path d="m3.5 3.5 5 5m0-5-5 5" />
        : <g fill="currentColor" stroke="none"><circle cx="1.3" cy="6" r=".95" /><circle cx="6" cy="6" r=".95" /><circle cx="10.7" cy="6" r=".95" /></g>}
  </svg>;
}
