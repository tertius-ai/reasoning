import React, { useEffect, useRef, useState } from "react";
import { ApiError, aborted, errorMessage, organizationPath, request } from "./organization-api";
import { ComputeConfiguration, ComputeResources as Limits, ComputeWorker, DEFAULT_COMPUTE_RESOURCES, computeWorkerStatus, validateComputeResources } from "./compute-api";
import { ComputeResources } from "./ComputeResources";
import { LeanEnvironments } from "./LeanEnvironments";
import { ScalerAdministration } from "./ScalerAdministration";

export function ComputeAdministration({ organizationId, onAuthLost }: { organizationId: string; onAuthLost: () => void }) {
  const [configuration, setConfiguration] = useState<ComputeConfiguration | null>(null);
  const [workers, setWorkers] = useState<ComputeWorker[]>([]);
  const [allowed, setAllowed] = useState<string[]>([]);
  const [quota, setQuota] = useState(0);
  const [concurrency, setConcurrency] = useState(2);
  const [pendingLimit, setPendingLimit] = useState<number | null>(null);
  const [requestLimit, setRequestLimit] = useState<number | null>(null);
  const [name, setName] = useState("");
  const [images, setImages] = useState<string[]>([]);
  const [limits, setLimits] = useState<Limits>({ ...DEFAULT_COMPUTE_RESOURCES });
  const [registration, setRegistration] = useState<(ComputeWorker & { token: string }) | null>(null);
  const [show, setShow] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const initialized = useRef(false), alive = useRef(true), working = useRef(false);
  const root = organizationPath(organizationId);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => {
    if (!registration && !uncertain) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [registration, uncertain]);
  useEffect(() => {
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const [config, records] = await Promise.all([
          request<ComputeConfiguration>(root + "/compute-configuration", { signal: controller.signal }),
          request<ComputeWorker[]>(root + "/compute-workers", { signal: controller.signal }),
        ]);
        if (controller.signal.aborted) return;
        setConfiguration(config); setWorkers(records);
        if (!initialized.current) { initialized.current = true; setAllowed(config.policy.allowed_images); setQuota(config.policy.monthly_cpu_seconds); setConcurrency(config.policy.max_active_jobs);
          setPendingLimit(config.policy.max_pending_jobs ?? null); setRequestLimit(config.policy.requests_per_user_15_minutes ?? null); }
      } catch (cause) {
        if (aborted(cause)) return;
        if (cause instanceof ApiError && cause.status === 401) { onAuthLost(); return; }
        setError(errorMessage(cause));
      }
      if (!controller.signal.aborted) timer = setTimeout(poll, 5000);
    };
    void poll(); return () => { controller.abort(); clearTimeout(timer); };
  }, [root, refresh, onAuthLost]);
  const fail = (cause: unknown) => { if (cause instanceof ApiError && cause.status === 401) onAuthLost(); else setError(errorMessage(cause)); };
  const action = async (work: () => Promise<void>) => {
    if (working.current) return;
    working.current = true; setBusy(true); setError("");
    try { await work(); if (alive.current) setRefresh((value) => value + 1); }
    catch (cause) { if (alive.current) fail(cause); }
    finally { working.current = false; if (alive.current) setBusy(false); }
  };
  return <section className="org-compute-admin" aria-label="Compute administration">
    <p className="org-help">Compute runs on separate Linux/Docker hosts, not the chat server. Registering a credential does not provision a machine, install a service, or pull an image.</p>
    {error && <p role="alert" className="org-error">{error}</p>}
    {!configuration ? <p role="status">Loading compute administration…</p> : <>
      <p className="org-budget">Available {configuration.balance.available_cpu_seconds} · Used {configuration.balance.used_cpu_seconds} · Held {configuration.balance.held_cpu_seconds} CPU-seconds</p>
      <form onSubmit={(event) => { event.preventDefault(); void action(async () => {
        if (!Number.isSafeInteger(quota) || quota < 0 || quota > 10 ** 12 || !Number.isInteger(concurrency) || concurrency < 1 || concurrency > 4096) throw new Error("Choose a bounded integer CPU quota and 1–4096 active jobs.");
        if ([pendingLimit, requestLimit].some((value) => value !== null && (!Number.isInteger(value) || value < 1 || value > 1000000))) throw new Error("Optional limits must be blank or integers from 1 to 1,000,000.");
        await request(root + "/compute-policy", { method: "PUT", body: { allowed_images: allowed, monthly_cpu_seconds: quota, max_active_jobs: concurrency,
          max_pending_jobs: pendingLimit, requests_per_user_15_minutes: requestLimit } });
      }); }}>
        <h3>Organization compute policy</h3><p className="org-help">Quota reserves allocated cores × maximum wall time. It does not cap DigitalOcean/Modal bills. Set separate provider limits and account for idle hardware, storage, and traffic.</p>
        <fieldset disabled={busy}><legend>Allowed compute images</legend>{configuration.images.length ? configuration.images.map((image) => <label key={image.id} className="org-compute-choice">
          <input type="checkbox" checked={allowed.includes(image.id)} onChange={(event) => setAllowed((current) => event.target.checked ? [...current, image.id] : current.filter((id) => id !== image.id))} />
          {image.name} · {image.languages.join(", ")}
        </label>) : <p>No operator-approved images. The host operator must supply an immutable image catalog.</p>}</fieldset>
        <label>Monthly CPU-time quota (seconds)<input required type="number" min={0} max={10 ** 12} step={1} value={Number.isNaN(quota) ? "" : quota} disabled={busy}
          onChange={(event) => setQuota(event.target.value === "" ? NaN : Number(event.target.value))} /></label>
        <label>Maximum concurrent jobs<input required type="number" min={1} max={4096} step={1} value={Number.isNaN(concurrency) ? "" : concurrency} disabled={busy}
          onChange={(event) => setConcurrency(event.target.value === "" ? NaN : Number(event.target.value))} /></label>
        <button disabled={busy} className="org-primary">Save compute policy</button>
        <label>Pending-job limit (blank: unlimited)<input type="number" min={1} max={1000000} step={1} value={pendingLimit ?? ""} disabled={busy}
          onChange={(event) => setPendingLimit(event.target.value === "" ? null : Number(event.target.value))} /></label>
        <label>Requests per user / 15 minutes (blank: unlimited)<input type="number" min={1} max={1000000} step={1} value={requestLimit ?? ""} disabled={busy}
          onChange={(event) => setRequestLimit(event.target.value === "" ? null : Number(event.target.value))} /></label>
        <button type="button" disabled={busy} onClick={() => { setAllowed(configuration.policy.allowed_images); setQuota(configuration.policy.monthly_cpu_seconds); setConcurrency(configuration.policy.max_active_jobs);
          setPendingLimit(configuration.policy.max_pending_jobs ?? null); setRequestLimit(configuration.policy.requests_per_user_15_minutes ?? null); }}>Load current saved policy</button>
      </form>
      <h3>Register a separate worker</h3><p className="org-help">The token authorizes job claims, output delivery, and access to submitted programs. Keep enrollment JSON in a private, owner-only file on the worker host; never give it to a job container. Declared limits must fit the actual host.</p>
      {registration && <div className="org-compute-enrollment"><label>One-time worker token<input type={show ? "text" : "password"} readOnly value={registration.token} autoComplete="off" /></label>
        <button type="button" onClick={() => {
          if (!navigator.clipboard) { setError("Clipboard unavailable. Show enrollment JSON and copy it into a private file manually."); return; }
          void navigator.clipboard.writeText(JSON.stringify(registration, null, 2)).catch(() => setError("Clipboard unavailable. Show enrollment JSON and copy it into a private file manually."));
        }}>Copy enrollment JSON</button>
        <button type="button" onClick={() => setShow(!show)}>{show ? "Hide enrollment JSON" : "Show enrollment JSON"}</button>
        <button type="button" onClick={() => { if (window.confirm("Dismiss this one-time credential? It cannot be recovered. If it was not saved privately, revoke this worker and register a replacement.")) { setRegistration(null); setShow(false); } }}>Dismiss enrollment</button>
        {show && <><label htmlFor={`worker-enrollment-${organizationId}`}>Private worker enrollment JSON</label>
          <textarea id={`worker-enrollment-${organizationId}`} rows={8} readOnly value={JSON.stringify(registration, null, 2)} /></>}
        <p className="org-help">Use this JSON with reasoning-compute-worker configure --registration. Copy before closing settings or switching tabs. It is not saved in browser storage or returned again.</p>
      </div>}
      {uncertain && <div role="alert"><p>Worker registration was not acknowledged. It may have succeeded. Inspect the worker list and revoke any inaccessible new credential before trying another registration.</p>
        <button disabled={busy} type="button" onClick={() => { if (window.confirm("Have you inspected registrations and revoked any worker whose one-time credential was lost? A new registration creates a separate identity.")) setUncertain(false); }}>I reviewed uncertain registration</button></div>}
      <form onSubmit={(event) => { event.preventDefault(); if (uncertain || registration) return; void action(async () => {
        const resources = validateComputeResources(limits);
        try {
          const result = await request<ComputeWorker & { token: string }>(root + "/compute-workers", { method: "POST", body: { name, image_ids: images, resources } });
          if (!result.token || result.name !== name || result.organization_id !== organizationId) throw new Error("Invalid one-time worker registration receipt.");
          if (alive.current) { setRegistration(result); setShow(false); setName(""); }
        } catch (cause) {
          if (alive.current && !(cause instanceof ApiError && cause.status >= 400 && cause.status < 500 && cause.status !== 408)) setUncertain(true);
          throw cause;
        }
      }); }}>
        <label>Worker name<input required maxLength={200} value={name} disabled={busy || uncertain || !!registration} onChange={(event) => setName(event.target.value)} /></label>
        <fieldset disabled={busy || uncertain || !!registration}><legend>Worker images</legend>{configuration.images.map((image) => <label className="org-compute-choice" key={image.id}>
          <input type="checkbox" checked={images.includes(image.id)} onChange={(event) => setImages((current) => event.target.checked ? [...current, image.id] : current.filter((id) => id !== image.id))} />{image.name}
        </label>)}</fieldset>
        <ComputeResources value={limits} disabled={busy || uncertain || !!registration} onChange={setLimits} />
        <button className="org-primary" disabled={busy || uncertain || !!registration || !images.length || !name.trim()}>Register worker credential</button>
      </form>
      <h3>Enrolled workers</h3><ul className="org-admin-list">{workers.map((worker) => <li key={worker.id}><div><strong>{worker.name}</strong>
        <small>{worker.id} · {computeWorkerStatus(worker)}</small>
        <small>Last seen {worker.last_seen_at === null ? "never" : new Date(worker.last_seen_at * 1000).toLocaleString()} · Not proof of current host readiness</small>
        <small>{worker.image_ids.join(", ")} · {worker.resources.cpu} cores · {worker.resources.memory_bytes} memory bytes</small></div>
        {worker.revoked_at === null && <>
          <button disabled={busy} onClick={() => void action(async () => { await request(root + "/compute-workers/" + worker.id, { method: "PATCH", body: { draining: !worker.draining } }); })}>{worker.draining ? "Resume claims" : "Drain worker"}</button>
          <button disabled={busy} onClick={() => { if (window.confirm("Permanently revoke this worker? This denies all protocol access, but does not stop a remote container or delete its hardware. Drain first for orderly retirement.")) void action(async () => {
            await request(root + "/compute-workers/" + worker.id, { method: "DELETE" }); if (registration?.id === worker.id) { setRegistration(null); setShow(false); }
          }); }}>Revoke worker</button>
        </>}
      </li>)}</ul><p className="org-help">Draining blocks fresh claims while existing work finishes. Revocation is not physical cleanup; cloud destruction must wait for verified termination and result delivery.</p>
      <ScalerAdministration key={organizationId} organizationId={organizationId} images={configuration.images} onAuthLost={onAuthLost} />
      <LeanEnvironments key={"lean-" + organizationId} organizationId={organizationId} onAuthLost={onAuthLost} />
    </>}
  </section>;
}
