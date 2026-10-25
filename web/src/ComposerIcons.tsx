// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import React from "react";

/** The plus of every upload button: 18px beside a message box, smaller inside a labelled button. */
export function PlusIcon({ size = 18 }: { size?: number }) {
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={size < 16 ? 2 : 1.7} strokeLinecap="round" aria-hidden="true">
    <path d="M12 4.5v15M4.5 12h15" /></svg>;
}

/** The return symbol of a message box's one button: it sends, or with an empty box starts or resumes work. */
export function ReturnIcon() {
  return <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.1" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M20 5v6a4 4 0 0 1-4 4H5" /><path d="m9 10-5 5 5 5" /></svg>;
}

export function StopIcon() {
  return <svg width="10" height="10" viewBox="0 0 12 12" aria-hidden="true"><rect x="1" y="1" width="10" height="10" rx="2" fill="currentColor" /></svg>;
}
