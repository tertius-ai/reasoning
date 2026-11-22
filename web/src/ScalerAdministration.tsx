import React, { useEffect, useRef, useState } from "react";
import { ApiError, aborted, errorMessage, organizationPath, request } from "./organization-api";
import { ComputeImage, ComputeResources as Limits, ComputeScaler, DEFAULT_COMPUTE_RESOURCES, captureScalerGrant } from "./compute-api";
import { ComputeResources } from "./ComputeResources";

export function ScalerAdministration({ organizationId, images, onAuthLost }: { organizationId: string; images: ComputeImage[]; onAuthLost: () => void }) {
  const [records, setRecords] = useState<ComputeScaler[]>([]);
  const [name, setName] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [limits, setLimits] = useState<Limits>({ ...DEFAULT_COMPUTE_RESOURCES });
  const [maximum, setMaximum] = useState(2), [days, setDays] = useState(30);
  const [credential, setCredential] = useState<{ id: string; token: string } | null>(null);
  const [show, setShow] = useState(false), [uncertain, setUncertain] = useState(false);
  const [error, setError] = useState(""), [busy, setBusy] = useState(false), [refresh, setRefresh] = useState(0);
  const alive = useRef(true), working = useRef(false);
  const root = organizationPath(organizationId) + "/compute-scalers";
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => {
    if (!credential && !uncertain) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", warn); return () => window.removeEventListener("beforeunload", warn);
  }, [credential, uncertain]);
  useEffect(() => {
    const controller = new AbortController();
    void request<ComputeScaler[]>(root, { signal: controller.signal }).then((value) => {
      if (!controller.signal.aborted) setRecords(value);
    }).catch((cause) => {
      if (aborted(cause) || controller.signal.aborted) return;
      if (cause instanceof ApiError && cause.status === 401) onAuthLost(); else setError(errorMessage(cause));
    });
    return () => controller.abort();
  }, [root, refresh, onAuthLost]);
  const action = async (work: () => Promise<void>) => {
    if (working.current) return;
    working.current = true; setBusy(true); setError("");
    try { await work(); }
    catch (cause) { if (alive.current) { if (cause instanceof ApiError && cause.status === 401) onAuthLost(); else setError(errorMessage(cause)); } }
    finally { working.current = false; if (alive.current) { setBusy(false); setRefresh((value) => value + 1); } }
  };
  const receive = (result: ComputeScaler & { token: string }, id?: string) => {
    if (!result || !/^[a-f0-9]{32}$/.test(result.id) || result.organization_id !== organizationId || (id && result.id !== id) || !/^rns_[A-Za-z0-9_-]{43}$/.test(result.token)) {
      throw new Error("Unacknowledged scaler credential. Inspect grants and rotate the exact existing grant; do not create a replacement blindly.");
    }
    if (alive.current) { setCredential({ id: result.id, token: result.token }); setShow(false); }
  };
  return <section className="org-scaler-admin" aria-label="Scoped compute automation">
    <h3>Scoped compute automation</h3>
    <p className="org-help">A scaler credential observes organization capacity and enrolls trusted workers for selected images. Those workers can read their assigned programs and execute jobs. The scaler cannot read chat/provider keys or create cloud hardware. Worker counts are not cloud spending limits.</p>
    {error && <p role="alert" className="org-error">{error}</p>}
    {credential && <div className="org-compute-enrollment">
      <label>One-time scaler credential<input type={show ? "text" : "password"} readOnly autoComplete="off" value={credential.token} /></label>
      <p className="org-help">Grant {credential.id}. Copy into a private scaler-token file before closing settings. It is not saved in browser storage or returned again. Use reasoning-deploy worker-enroll to recover the same worker after a lost enrollment response.</p>
      <button type="button" onClick={() => {
        if (!navigator.clipboard) { setError("Clipboard unavailable. Show the credential and copy it manually."); return; }
        void navigator.clipboard.writeText(credential.token).catch(() => { if (alive.current) setError("Clipboard unavailable. Show the credential and copy it manually."); });
      }}>Copy scaler credential</button>
      <button type="button" onClick={() => setShow(!show)}>{show ? "Hide scaler credential" : "Show scaler credential"}</button>
      <button type="button" onClick={() => { if (window.confirm("Dismiss this one-time scaler token? If it was not saved privately, rotate this exact grant to obtain another token.")) { setCredential(null); setShow(false); } }}>Dismiss scaler credential</button>
    </div>}
    {uncertain && <div role="alert"><p>Scaler creation was not acknowledged. Inspect the grants below and rotate or revoke the inaccessible grant before creating another.</p>
      <button type="button" disabled={busy} onClick={() => { if (window.confirm("Have you inspected grants and rotated or revoked any lost scaler credential? Another creation makes a separate grant.")) setUncertain(false); }}>I reviewed uncertain scaler creation</button></div>}
    <form onSubmit={(event) => { event.preventDefault(); if (credential || uncertain) return; void action(async () => {
      const body = captureScalerGrant(name, selected, limits, maximum, days, images);
      try { receive(await request<ComputeScaler & { token: string }>(root, { method: "POST", body })); }
      catch (cause) { if (alive.current && !(cause instanceof ApiError && cause.status >= 400 && cause.status < 500 && cause.status !== 408)) setUncertain(true); throw cause; }
    }); }}>
      <fieldset disabled={busy || !!credential || uncertain}><legend>New scaler grant</legend>
        <label>Scaler name<input required maxLength={200} value={name} onChange={(event) => setName(event.target.value)} /></label>
        <fieldset><legend>Scaler-approved images</legend>{images.map((image) => <label key={image.id} className="org-compute-choice">
          <input type="checkbox" checked={selected.includes(image.id)} onChange={(event) => setSelected((current) => event.target.checked ? [...current, image.id] : current.filter((id) => id !== image.id))} />{image.name}
        </label>)}</fieldset>
        <label>Maximum scaler-enrolled workers<input type="number" required min={1} max={64} step={1} value={Number.isNaN(maximum) ? "" : maximum} onChange={(event) => setMaximum(event.target.value === "" ? NaN : Number(event.target.value))} /></label>
        <label>Scaler lifetime (days)<input type="number" required min={1} max={365} step={1} value={Number.isNaN(days) ? "" : days} onChange={(event) => setDays(event.target.value === "" ? NaN : Number(event.target.value))} /></label>
        <ComputeResources value={limits} onChange={setLimits} disabled={busy || !!credential || uncertain} />
        <button className="org-primary" disabled={!selected.length || !name.trim()}>Create scaler credential</button>
      </fieldset>
    </form>
    <ul className="org-admin-list">{records.map((record) => <li key={record.id}><div><strong>{record.name}</strong>
      <small>{record.id} · {record.revoked_at !== null ? "Revoked" : record.enabled ? "Enabled" : "Expired or sponsor unavailable"}</small>
      <small>Up to {record.maximum_workers} workers · {record.image_ids.join(", ")} · Expires {new Date(record.expires_at * 1000).toLocaleString()}</small>
      <small>Each worker at most {record.resources.cpu} cores · {record.resources.memory_bytes} memory bytes</small>
      <small>{record.enrolled_workers} of {record.maximum_workers} enrolled{record.enrolled_workers >= record.maximum_workers ? " · Full: adds no capacity beyond its workers" : ""}</small></div>
      {record.revoked_at === null && <>
        <button disabled={busy || !!credential} onClick={() => { if (window.confirm("Rotate this exact scaler grant? The old token stops working immediately; existing worker credentials remain valid. You become its sponsor for 30 days.")) void action(async () => {
          receive(await request<ComputeScaler & { token: string }>(root + "/" + record.id + "/rotate", { method: "POST", body: { lifetime_seconds: 2592000 } }), record.id);
        }); }}>Rotate scaler credential</button>
        <button disabled={busy} onClick={() => { if (window.confirm("Permanently revoke this scaler? This stops enrollment/capacity access, but does not revoke existing workers, terminate jobs, or delete cloud hardware.")) void action(async () => {
          await request(root + "/" + record.id, { method: "DELETE" }); if (alive.current && credential?.id === record.id) { setCredential(null); setShow(false); }
        }); }}>Revoke scaler</button>
      </>}
    </li>)}</ul>
    <p className="org-help">At most eight current grants. Bounds are immutable; rotation preserves worker enrollment request IDs. Expiry, revocation, sponsor demotion/removal, and backup restore disable automation. Cloud provisioning uses separate operator-owned credentials and budget controls.</p>
  </section>;
}
