import React, { useEffect, useState } from "react";
import { ApiError, aborted, organizationPath, request, usd } from "./organization-api";

export type DailySpend = {
  status: "ready" | "no_key" | "no_daily_limit" | "unavailable";
  spent_microusd: number | null; limit_microusd: number | null; remaining_microusd: number | null;
  checked_at: number | null; resets_at: number | null;
};

function compactMoney(value: number): string {
  return value > 0 && value < 10000 ? "<$0.01" : "$" + (value / 1000000).toLocaleString("en-US", { maximumFractionDigits: 2 });
}

export function ProviderDailySpend({ organizationId, onAuthLost }: { organizationId: string; onAuthLost: () => void }) {
  const [value, setValue] = useState<DailySpend | null>(null);
  useEffect(() => {
    const controller = new AbortController(); setValue(null);
    let sequence = 0;
    const refresh = () => {
      const current = ++sequence;
      void request<DailySpend>(organizationPath(organizationId) + "/provider-daily-spend", { signal: controller.signal })
      .then(result => { if (!controller.signal.aborted && current === sequence) setValue(result); })
      .catch(cause => {
        if (aborted(cause) || current !== sequence) return;
        if (cause instanceof ApiError && cause.status === 401) onAuthLost();
        if (!controller.signal.aborted) setValue({ status: "unavailable", spent_microusd: null, limit_microusd: null,
          remaining_microusd: null, checked_at: null, resets_at: null });
      });
    };
    const visible = () => { if (document.visibilityState === "visible") refresh(); };
    refresh(); const timer = setInterval(visible, 30000);
    window.addEventListener("focus", refresh); document.addEventListener("visibilitychange", visible);
    return () => { controller.abort(); clearInterval(timer); window.removeEventListener("focus", refresh);
      document.removeEventListener("visibilitychange", visible); };
  }, [organizationId, onAuthLost]);
  const ready = value?.status === "ready";
  const fraction = ready ? value.limit_microusd! > 0 ? Math.min(1, value.spent_microusd! / value.limit_microusd!) : 1 : 0;
  const caption = ready ? `${compactMoney(value.spent_microusd!)} / ${compactMoney(value.limit_microusd!)}`
    : value?.status === "no_daily_limit" ? "Daily cap unset" : value?.status === "no_key" ? "No AI key"
    : value ? "Spend unavailable" : "Daily spend…";
  const description = ready ? `OpenRouter daily spend: ${usd(value.spent_microusd!)} of ${usd(value.limit_microusd!)}; ${usd(value.remaining_microusd!)} remaining`
    : caption;
  return <details className="org-daily-spend">
    <summary aria-label={description} title={description}>
      <span className={"org-daily-meter" + (fraction >= 1 ? " org-daily-meter-full" : "") + (!ready ? " org-daily-meter-unknown" : "")}
        role="img" aria-label={ready ? `${Math.round(fraction * 100)}% of daily limit used` : caption}>
        <span style={{ width: `${fraction * 100}%` }} /></span>
      <small>{caption}</small>
    </summary>
    <div className="org-daily-spend-detail">
      <strong>Daily spending · OpenRouter</strong>
      {ready ? <p>{usd(value.spent_microusd!)} spent · {usd(value.limit_microusd!)} daily limit
        {value.remaining_microusd === 0 && <><br />Daily key limit reached.</>}</p>
        : <p>{value?.status === "no_daily_limit" ? "Set a daily spending limit on this organization's active key in OpenRouter."
          : value?.status === "no_key" ? "An administrator can connect an OpenRouter key in Administration → Inference."
          : value ? "The provider's current spending information is unavailable. Inference still uses OpenRouter's limits."
          : "Reading the provider's daily spending information."}</p>}
      {value?.checked_at != null && <p>Checked {new Date(value.checked_at * 1000).toLocaleTimeString()}.
        {value.resets_at != null && <> Resets {new Date(value.resets_at * 1000).toLocaleString()} (00:00 UTC).</>}</p>}
      <p>OpenRouter enforces the limit. This shows all spending on the active key. Automatic top-ups replenish account credits;
        they do not reset the key's daily allowance. Running calls can finish after the allowance is reached.</p>
      <a href="https://openrouter.ai/settings/keys" target="_blank" rel="noreferrer">OpenRouter key settings ↗</a>
    </div>
  </details>;
}
