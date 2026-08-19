import { useEffect, useState } from "react";
import {
  getCeilingProof,
  getDashboard,
  type CeilingProof,
  type Dashboard as DashboardData,
} from "./api";

const REFRESH_MS = 10000;

function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="border border-gray-200 rounded-lg p-4 bg-white">
      <div className="text-xs uppercase tracking-wide text-gray-500">{label}</div>
      <div className="text-2xl font-bold text-slate-800 mt-1">{value}</div>
      {hint && <div className="text-xs text-gray-400 mt-1">{hint}</div>}
    </div>
  );
}

/**
 * Controller dashboard (F8) and the autonomy-ceiling proof (F10).
 *
 * The auto-approval rate is the headline number because it is the one that says
 * whether the product is doing its job: escalating everything would be perfectly
 * safe and perfectly useless.
 */
export function Dashboard() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [proof, setProof] = useState<CeilingProof | null>(null);
  const [error, setError] = useState("");
  const [windowHours, setWindowHours] = useState(24);

  useEffect(() => {
    let cancelled = false;

    const load = async () => {
      try {
        const [dashboard, ceilingProof] = await Promise.all([
          getDashboard(windowHours),
          getCeilingProof(),
        ]);
        if (!cancelled) {
          setData(dashboard);
          setProof(ceilingProof);
          setError("");
        }
      } catch (e: unknown) {
        if (!cancelled)
          setError(e instanceof Error ? e.message : "Could not load the dashboard");
      }
    };

    load();
    const timer = setInterval(load, REFRESH_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [windowHours]);

  if (error) {
    return (
      <div className="text-red-700 bg-red-50 px-3 py-2 rounded-md text-sm">{error}</div>
    );
  }
  if (!data) {
    return <div className="text-gray-400 py-8 text-center">Loading…</div>;
  }

  const autoRate = (data.rates.auto_approval_rate * 100).toFixed(1);
  const escalationRate = (data.rates.escalation_rate * 100).toFixed(1);

  return (
    <div>
      <div className="flex items-center justify-between flex-wrap gap-2 mb-4">
        <h2 className="text-lg font-bold m-0">Controller dashboard</h2>
        <label className="text-sm text-gray-600">
          Window{" "}
          <select
            value={windowHours}
            onChange={(e) => setWindowHours(Number(e.target.value))}
            className="border border-gray-300 rounded-md px-2 py-1 text-sm"
          >
            <option value={1}>1 hour</option>
            <option value={24}>24 hours</option>
            <option value={168}>7 days</option>
            <option value={720}>30 days</option>
          </select>
        </label>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-6">
        <Stat label="Items" value={String(data.items)} hint="distinct submissions" />
        <Stat
          label="Auto-approved"
          value={`${autoRate}%`}
          hint={`${data.routes.auto_approve} with no human`}
        />
        <Stat
          label="Escalated"
          value={`${escalationRate}%`}
          hint={`${data.routes.human_review} needed a person`}
        />
        <Stat
          label="Avg confidence"
          value={
            data.avg_agent_confidence === null
              ? "n/a"
              : `${(data.avg_agent_confidence * 100).toFixed(0)}%`
          }
          hint="agent self-reported"
        />
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-3 mb-6">
        <Stat
          label="Money auto-approved"
          value={`$${Number(data.money_usd.auto_approved).toLocaleString()}`}
          hint="no human involved"
        />
        <Stat
          label="Money human-approved"
          value={`$${Number(data.money_usd.human_approved).toLocaleString()}`}
          hint="approved by a person"
        />
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="border border-gray-200 rounded-lg p-4 bg-white">
          <h3 className="mt-0 mb-2 text-base font-bold">Routes</h3>
          {Object.entries(data.routes).map(([route, count]) => (
            <div
              key={route}
              className="flex justify-between text-sm py-1 border-b border-gray-100 last:border-b-0"
            >
              <span className="text-gray-600">{route.replace(/_/g, " ")}</span>
              <span className="font-semibold">{count}</span>
            </div>
          ))}
          {data.avg_decision_latency_seconds !== null && (
            <p className="text-xs text-gray-500 mt-3 mb-0">
              Average time from submission to decision:{" "}
              {data.avg_decision_latency_seconds.toFixed(2)}s
            </p>
          )}
        </div>

        <div className="border border-gray-200 rounded-lg p-4 bg-white">
          <h3 className="mt-0 mb-2 text-base font-bold">Most-cited policy rules</h3>
          {data.top_rules.length === 0 ? (
            <p className="text-sm text-gray-400 m-0">No rules cited yet.</p>
          ) : (
            data.top_rules.map((rule) => (
              <div
                key={rule.rule_id}
                className="flex justify-between text-sm py-1 border-b border-gray-100 last:border-b-0"
              >
                <span className="text-gray-600">{rule.rule_id}</span>
                <span className="font-semibold">{rule.count}</span>
              </div>
            ))
          )}
        </div>
      </div>

      {/* F10 — the proof, stated as a claim the auditor can check. */}
      {proof && (
        <div
          className={`mt-6 rounded-lg p-4 border-2 ${
            proof.holds
              ? "border-green-300 bg-green-50"
              : "border-red-400 bg-red-50"
          }`}
        >
          <h3 className="mt-0 mb-2 text-base font-bold">
            {proof.holds
              ? "Autonomy ceiling holds"
              : "CEILING VIOLATION DETECTED"}
          </h3>
          <p className="text-sm m-0 mb-2">
            {proof.auto_approvals_examined} autonomous approvals examined,{" "}
            {proof.ceiling_violations} above the ceiling in force at decision time,{" "}
            {proof.confidence_violations} below the confidence bar,{" "}
            {proof.auto_approvals_not_made_by_router} made by anything other than the
            router.
          </p>
          <p className="text-sm m-0">
            Largest amount ever auto-approved:{" "}
            <strong>${Number(proof.max_auto_approved_amount_usd).toLocaleString()}</strong>{" "}
            against a highest configured ceiling of{" "}
            <strong>${Number(proof.highest_ceiling_in_use_usd).toLocaleString()}</strong>.
          </p>
          {proof.per_category.length > 0 && (
            <table className="w-full text-xs mt-3 border-collapse">
              <thead>
                <tr className="text-left text-gray-500">
                  <th className="py-1">Category</th>
                  <th className="py-1">Auto-approvals</th>
                  <th className="py-1">Max amount</th>
                  <th className="py-1">Lowest ceiling</th>
                </tr>
              </thead>
              <tbody>
                {proof.per_category.map((row) => (
                  <tr key={row.category} className="border-t border-gray-200">
                    <td className="py-1">{row.category}</td>
                    <td className="py-1">{row.auto_approvals}</td>
                    <td className="py-1">${row.max_amount_usd}</td>
                    <td className="py-1">${row.lowest_ceiling_usd}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      <p className="text-xs text-gray-500 mt-4">
        Policy configuration v{data.policy.version} — confidence bar{" "}
        {data.policy.confidence_threshold}, ceilings{" "}
        {Object.entries(data.policy.ceilings_usd)
          .map(([category, ceiling]) => `${category} $${ceiling}`)
          .join(", ")}
        {data.outbox && (
          <>
            {" "}
            · outbox{" "}
            {Object.entries(data.outbox.depth_by_status)
              .map(([status, count]) => `${status} ${count}`)
              .join(", ") || "empty"}
          </>
        )}
      </p>
    </div>
  );
}
