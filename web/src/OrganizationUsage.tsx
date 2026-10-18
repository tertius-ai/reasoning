import React, { useEffect, useMemo, useState } from "react";
import { ModelRows } from "./ModelBrowser";
import { chatModel, usefulChatModel } from "./modelRecommendations";
import { InferenceLedger } from "./InferenceLedger";
import { RetainedWork } from "./RetainedWork";
import { ModelFeedbackPanel } from "./ChatFeedback";
import { ApiError, ImpactProfile, ModelBenchmarks, OrganizationPrices, OrganizationUsage as Usage, InferenceConfiguration, RoutingPolicy,
  UsageTotal, CATALOG_MAX_AGE, aborted, errorMessage, forgetSharedRead, modelCatalog, organizationPath, request, sharedRequest, usd } from "./organization-api";

import { estimateLabel } from "./usageFormat";
export { estimateLabel } from "./usageFormat";

export { MessageUsageDetails } from "./MessageUsageDetails";

const noModels: string[] = [];

export function OrganizationModelBrowser({ organizationId, canManage = false, onAuthLost }: { organizationId: string; canManage?: boolean; onAuthLost: () => void }) {
  const [catalog, setCatalog] = useState<OrganizationPrices | null>(null);
  const [query, setQuery] = useState("");
  const [provider, setProvider] = useState("");
  const [capability, setCapability] = useState<"all" | "tools" | "reasoning" | "images">("all");
  const [refresh, setRefresh] = useState(0);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [showAll, setShowAll] = useState(false);
  const [benchmarks, setBenchmarks] = useState<ModelBenchmarks>();
  useEffect(() => {
    const controller = new AbortController(); setBenchmarks(undefined);
    const benchmarksPath = organizationPath(organizationId) + "/model-benchmarks";
    if (refresh) forgetSharedRead(benchmarksPath);
    void sharedRequest<ModelBenchmarks>(benchmarksPath, { maxAge: CATALOG_MAX_AGE, durable: true, signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) setBenchmarks(value); })
      .catch(cause => { if (!aborted(cause) && cause instanceof ApiError && cause.status === 401) onAuthLost(); });
    return () => controller.abort();
  }, [organizationId, refresh, onAuthLost]);
  useEffect(() => {
    const controller = new AbortController(); setLoading(true); setError("");
    // "Refresh catalog" asks the server again; otherwise reopening the dialog reuses the shared catalog.
    if (refresh) forgetSharedRead(organizationPath(organizationId) + "/model-prices");
    void modelCatalog(organizationId, controller.signal)
      .then((value) => { if (!controller.signal.aborted) setCatalog(value); }).catch((cause) => {
        if (aborted(cause)) return;
        if (cause instanceof ApiError && cause.status === 401) onAuthLost(); else setError(errorMessage(cause));
      }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [organizationId, refresh, onAuthLost]);
  const providers = useMemo(() => [...new Set((catalog?.models ?? []).map((model) => model.provider))].sort(), [catalog]);
  const rows = useMemo(() => (catalog?.models ?? []).filter((model) => chatModel(model.id) && (showAll || usefulChatModel(model.id, model))
    && (model.id + " " + model.name + " " + model.description).toLowerCase().includes(query.toLowerCase())
    && (!provider || model.provider === provider)
    && (capability === "all" || capability === "tools" && model.supports_tools || capability === "reasoning" && model.supports_reasoning
      || capability === "images" && model.input_modalities.includes("image"))).map(model => model.id), [catalog, showAll, query, provider, capability]);
  const scored = useMemo(() => catalog ? { ...catalog, benchmarks } : null, [catalog, benchmarks]);
  const allowed = catalog?.allowed_models ?? noModels;
  return <section aria-label="Model capabilities" className="org-capability-browser">
    <OrganizationRouting organizationId={organizationId} canManage={canManage} onAuthLost={onAuthLost} />
    <header><label>Search<input type="search" placeholder="Name, ID, or capability" value={query} onChange={(event) => setQuery(event.target.value)} /></label>
      <button disabled={loading} onClick={() => setRefresh((value) => value + 1)}>Refresh catalog</button></header>
    <details><summary>Filters</summary><div className="org-capability-filters">
      <label className="org-model-choice"><input type="checkbox" checked={showAll} onChange={event => setShowAll(event.target.checked)} />All chat models</label>
      <label>Provider<select value={provider} onChange={(event) => setProvider(event.target.value)}><option value="">All providers</option>
        {providers.map((value) => <option key={value} value={value}>{value}</option>)}</select></label>
      <label>Capability<select value={capability} onChange={(event) => setCapability(event.target.value as typeof capability)}>
        <option value="all">All capabilities</option><option value="tools">Tool use</option><option value="reasoning">Reasoning</option><option value="images">Image input</option></select></label>
      </div></details>
    {error && <p role="alert" className="org-error">{error}</p>}
    {loading && <p role="status">Loading model catalog…</p>}
    {catalog && <p className="org-catalog-status">{rows.length.toLocaleString()} models · refreshed {new Date(catalog.fetched_at * 1000).toLocaleString()}</p>}
    {benchmarks?.meta && <p className="org-catalog-status"><a href={benchmarks.meta.source_url} target="_blank" rel="noreferrer">{benchmarks.meta.citation}</a>
      {" "}As of {new Date(benchmarks.meta.as_of).toLocaleString()} · {benchmarks.status}. Licensed under CC BY 4.0.</p>}
    <ModelRows ids={rows} catalog={scored} allowedModels={allowed} grouped={false} />
    {!loading && !error && !rows.length && <p>No matching models.</p>}
  </section>;
}

function OrganizationRouting({ organizationId, canManage, onAuthLost }: { organizationId: string; canManage: boolean; onAuthLost: () => void }) {
  const [policy, setPolicy] = useState<RoutingPolicy | null>(null), [error, setError] = useState(""), [busy, setBusy] = useState(false);
  useEffect(() => {
    const abort = new AbortController();
    void sharedRequest<InferenceConfiguration>(organizationPath(organizationId) + "/inference-configuration", { maxAge: 2000, signal: abort.signal })
      .then(value => { if (!abort.signal.aborted) setPolicy(value.routing ?? { strategy: "recommended", zdr: false, region: "global" }); })
      .catch(cause => { if (!aborted(cause)) { if (cause instanceof ApiError && cause.status === 401) onAuthLost(); else setError(errorMessage(cause)); } });
    return () => abort.abort();
  }, [organizationId, onAuthLost]);
  return <details className="org-routing"><summary>Provider routing{policy ? ` · ${policy.strategy}${policy.zdr ? " · ZDR" : ""} · ${policy.region}` : ""}</summary>
    {policy && <form onSubmit={event => {
      event.preventDefault(); setBusy(true); setError("");
      void request(organizationPath(organizationId) + "/routing", { method: "PUT", body: policy })
        .catch(cause => { if (cause instanceof ApiError && cause.status === 401) onAuthLost(); else setError(errorMessage(cause)); }).finally(() => setBusy(false));
    }}>
      <label>Strategy<select disabled={!canManage || busy} value={policy.strategy} onChange={event => setPolicy({ ...policy, strategy: event.target.value as RoutingPolicy["strategy"] })}>
        <option value="recommended">OpenRouter recommended</option><option value="cheapest">Cheapest compatible</option>
        <option value="latency">Lowest latency</option><option value="throughput">Highest throughput</option></select></label>
      <label className="org-model-choice"><input type="checkbox" checked={policy.zdr} disabled={!canManage || busy} onChange={event => setPolicy({ ...policy, zdr: event.target.checked })} />Require ZDR; deny data collection</label>
      <label>Inference region<select value={policy.region} disabled={!canManage || busy} onChange={event => setPolicy({ ...policy, region: event.target.value as RoutingPolicy["region"] })}>
        <option value="global">Global</option><option value="us">USA</option><option value="eu">EU</option></select></label>
      <p className="org-help">Regional API routing requires an eligible OpenRouter plan. No silent regional or ZDR fallback. Badges indicate availability, not your enforced policy.
        Recommended and speed routing permit eligible providers at their prices; cheapest keeps the quoted price ceiling. Speed preferences are not guarantees.
        Holds are estimates, not charges or invoice caps; use OpenRouter key spending limits.</p>
      {canManage && <button disabled={busy}>Save routing</button>}
    </form>}{error && <p role="alert">{error}</p>}
  </details>;
}

function UsageRow({ name, total }: { name: string; total: UsageTotal }) {
  return <tr><th scope="row">{name}</th><td>{total.runs.toLocaleString()}</td>
    <td title={`${total.input_known_runs}/${total.runs} calls reported`}>{total.prompt_tokens.toLocaleString()}</td>
    <td title={`${total.output_known_runs}/${total.runs} calls reported`}>{total.completion_tokens.toLocaleString()}</td>
    <td title={`${total.cost_known_runs}/${total.runs} calls reported`}>{usd(total.actual_microusd)}</td><td>{usd(total.outstanding_microusd)}</td>
    <td title={`${total.energy_known_runs}/${total.runs} covered; ${total.reference_energy_runs ?? 0} reference scenarios`}>{estimateLabel(total.estimated_kwh)}</td>
    <td title={`${total.carbon_known_runs}/${total.runs} covered`}>{estimateLabel(total.estimated_kg_co2e)}</td></tr>;
}

export function OrganizationUsagePanel({ organizationId, canManage, onAuthLost }: {
  organizationId: string; canManage: boolean; onAuthLost: () => void;
}) {
  const [usage, setUsage] = useState<Usage | null>(null);
  const [profiles, setProfiles] = useState<ImpactProfile[]>([]);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [busy, setBusy] = useState(false);
  const [model, setModel] = useState("");
  const [input, setInput] = useState("");
  const [output, setOutput] = useState("");
  const [location, setLocation] = useState("");
  const [intensity, setIntensity] = useState("");
  const [source, setSource] = useState("");
  const root = organizationPath(organizationId);
  const fail = (cause: unknown) => { if (cause instanceof ApiError && cause.status === 401) onAuthLost(); else setError(errorMessage(cause)); };
  useEffect(() => {
    const controller = new AbortController(); setError("");
    void Promise.all([request<Usage>(root + "/usage", { signal: controller.signal }),
      request<{ profiles: ImpactProfile[] }>(root + "/impact-profiles", { signal: controller.signal })])
      .then(([value, configured]) => { if (!controller.signal.aborted) { setUsage(value); setProfiles(configured.profiles); } })
      .catch((cause) => { if (!aborted(cause)) fail(cause); });
    return () => controller.abort();
  }, [root, refresh, onAuthLost]);
  const save = async (values: ImpactProfile[]) => {
    setBusy(true); setError("");
    try {
      const value = await request<{ profiles: ImpactProfile[] }>(root + "/impact-profiles", { method: "PUT", body: { profiles: values } });
      setProfiles(value.profiles); setModel(""); setInput(""); setOutput(""); setLocation(""); setIntensity(""); setSource("");
      setRefresh((value) => value + 1);
    } catch (cause) { fail(cause); } finally { setBusy(false); }
  };
  return <section aria-label="Organization and project usage">
    <button onClick={() => setRefresh((value) => value + 1)}>Refresh usage</button>
    {error && <p role="alert" className="org-error">{error}</p>}
    {!usage && <p role="status">Loading usage…</p>}
    {usage && <><div className="org-usage-table-wrap"><table className="org-usage-table" aria-label="Organization and project usage">
      <thead><tr><th scope="col">Scope</th><th scope="col">Calls</th><th scope="col">Input tokens</th><th scope="col">Output tokens</th>
        <th scope="col">Charge USD</th><th scope="col">Held USD</th><th scope="col">≈ kWh</th><th scope="col">≈ kg CO₂e</th></tr></thead>
      <tbody><UsageRow name="Entire organization" total={usage.total} />{usage.projects.map(project => <UsageRow key={project.project_id} name={project.project_name} total={project} />)}</tbody>
    </table></div><details><summary>Coverage and estimation method</summary><p className="org-help">{usage.scope} {usage.method}</p>
      <p className="org-help">Energy covers {usage.total.energy_known_runs}/{usage.total.runs} calls, including {usage.total.reference_energy_runs ?? 0} reference scenarios.
        Carbon covers {usage.total.carbon_known_runs}/{usage.total.runs}. Blank values are unavailable, not zero.
        The unconfigured baseline assumes 1 J per processed token; it is not a measured model footprint. <a href="https://arxiv.org/abs/2603.20224" target="_blank" rel="noreferrer">Energy-per-token methodology</a>.</p></details>
      {!usage.projects.length && <p>No LLM calls recorded.</p>}</>}
    <InferenceLedger organizationId={organizationId} canManage={canManage} onAuthLost={onAuthLost} />
    {canManage && <RetainedWork organizationId={organizationId} />}
    <ModelFeedbackPanel organizationId={organizationId} />
    <details className="org-impact-settings"><summary>Energy and data-center assumptions</summary>
      <p className="org-help">OpenRouter does not establish physical data-center location. Do not use the chat server's location.
        Set per-model operational energy coefficients (including data-center cooling/PUE if intended) and an assumed inference location's grid intensity.
        Include the reference, year, and method in the source field. Leave carbon intensity blank if unknown.
        These are scenario estimates, not measured footprints or a life-cycle assessment. Profiles are captured when a run is queued;
        changes affect new runs only. Unconfigured runs use the labeled 1 J/token reference scenario, without a carbon estimate.
        Assumptions are visible to organization members; do not include secrets.</p>
      <ul>{profiles.map((profile) => <li key={profile.model}><strong>{profile.model}</strong> · {profile.location} · {profile.source}
        {canManage && <><button disabled={busy} onClick={() => { setModel(profile.model); setInput(String(profile.input_kwh_per_million));
          setOutput(String(profile.output_kwh_per_million)); setLocation(profile.location); setIntensity(profile.grid_grams_co2e_per_kwh == null ? "" : String(profile.grid_grams_co2e_per_kwh)); setSource(profile.source); }}>Edit</button>
          <button disabled={busy} onClick={() => void save(profiles.filter((item) => item.model !== profile.model))}>Remove</button></>}</li>)}</ul>
      {canManage && <form onSubmit={(event) => { event.preventDefault(); void save([...profiles.filter((profile) => profile.model !== model), {
        model, input_kwh_per_million: Number(input), output_kwh_per_million: Number(output), location,
        grid_grams_co2e_per_kwh: intensity.trim() ? Number(intensity) : null, source,
      }]); }}>
        <label>Model identifier<input required maxLength={200} value={model} onChange={(event) => setModel(event.target.value)} /></label>
        <div className="org-model-limits"><label>Input kWh per million tokens<input required type="number" min={0} max={1000000} step="any" value={input} onChange={(event) => setInput(event.target.value)} /></label>
          <label>Output kWh per million tokens<input required type="number" min={0} max={1000000} step="any" value={output} onChange={(event) => setOutput(event.target.value)} /></label></div>
        <label>Assumed inference data-center location<input required maxLength={200} value={location} onChange={(event) => setLocation(event.target.value)} /></label>
        <label>Location grid intensity (g CO₂e/kWh)<input type="number" min={0} max={2000} step="any" value={intensity} onChange={(event) => setIntensity(event.target.value)} /></label>
        <label>Source, year, and estimation assumptions<textarea required maxLength={1000} value={source} onChange={(event) => setSource(event.target.value)} /></label>
        <button disabled={busy}>Save model assumptions</button>
      </form>}
    </details>
  </section>;
}
