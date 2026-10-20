// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0

/** The product wordmark: a "therefore" mark and the name in lowercase monospace. */
export function Brand() {
  return <span className="org-brand" aria-label="Reasoning"><span className="org-brand-mark" aria-hidden="true">∴</span>
    <span aria-hidden="true">reasoning</span></span>;
}
