import { Component, type ReactNode, Suspense } from "react";

class LoadBoundary extends Component<{ label: string; children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() {
    if (!this.state.failed) return this.props.children;
    return <div className="org-error" role="alert">{this.props.label} could not be loaded. Saved work is unchanged.{" "}
      <button type="button" onClick={() => window.location.reload()}>Reload</button></div>;
  }
}

/** A lazily loaded organization view with an explicit loading state and a recoverable load failure. */
export function LazyView({ label, fallback, children }: { label: string; fallback?: ReactNode; children: ReactNode }) {
  return <LoadBoundary label={label}>
    <Suspense fallback={fallback ?? <p role="status" className="org-help">Loading {label.toLocaleLowerCase()}…</p>}>{children}</Suspense>
  </LoadBoundary>;
}
