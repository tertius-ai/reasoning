import { MessageUsage, usd } from "./organization-api";
import { estimateLabel } from "./usageFormat";

export function MessageUsageDetails({ usage }: { usage?: MessageUsage | null }) {
  return <small className="org-message-usage" title={usage?.assumptions?.source}>
    {usage?.prompt_tokens?.toLocaleString() ?? "—"} in / {usage?.completion_tokens?.toLocaleString() ?? "—"} out tokens
    {usage?.actual_microusd != null && ` · ${usd(usage.actual_microusd)}`}
    {usage?.estimated_kwh != null && ` · ≈ ${estimateLabel(usage.estimated_kwh * 1000)} Wh${usage.estimate_kind === "reference" ? "*" : ""}`}
    {usage?.estimated_kg_co2e != null && ` · ≈ ${estimateLabel(usage.estimated_kg_co2e * 1000)} g CO₂`}
  </small>;
}
