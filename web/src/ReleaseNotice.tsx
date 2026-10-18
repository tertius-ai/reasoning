// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import { useSyncExternalStore } from "react";
import { ReleaseWatch, releaseWatch } from "./release";

/**
 * Offers a reload once the server reports a newer release than the one that served this page. It never reloads by
 * itself: drafts live only in this page, so the reader chooses when to reload.
 */
export function ReleaseNotice({ watch = releaseWatch }: { watch?: ReleaseWatch }) {
  const available = useSyncExternalStore(watch.subscribe, () => watch.available, () => null);
  if (!available) return null;
  return <div className="org-banner org-release-notice" role="status">
    <span>A new version of Reasoning is available. Reloading discards unsent drafts.</span>
    <span className="org-release-notice-actions"><button type="button" className="org-primary" onClick={() => window.location.reload()}>Reload</button>
      <button type="button" aria-label="Dismiss update notice" title="Dismiss" onClick={() => watch.dismiss()}>×</button></span>
  </div>;
}
