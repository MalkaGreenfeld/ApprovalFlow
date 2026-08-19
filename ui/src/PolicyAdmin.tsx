import { useEffect, useState } from "react";
import { getPolicy, updatePolicy, type PolicyResponse } from "./api";

/**
 * Policy and autonomy configuration (F7 / M13).
 *
 * A controller edits the ceilings, the confidence bar and the rule catalogue here
 * and the change is live within seconds — no rebuild, no restart. The router
 * validates every submission against bounds compiled into its code, so the worst
 * a mistake here can do is make the system *more* conservative.
 */
export function PolicyAdmin() {
  const [policy, setPolicy] = useState<PolicyResponse | null>(null);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [saving, setSaving] = useState(false);

  const load = async () => {
    setError("");
    try {
      const result = await getPolicy();
      setPolicy(result);
      setDraft(JSON.stringify(result.config, null, 2));
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Could not load the policy");
    }
  };

  useEffect(() => {
    load();
  }, []);

  const handleSave = async () => {
    setError("");
    setNotice("");
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(draft);
    } catch {
      setError("That is not valid JSON — fix the syntax before saving.");
      return;
    }

    setSaving(true);
    try {
      const result = await updatePolicy(parsed);
      setNotice(
        `Saved as version ${result.version}. Every service picks it up within a few seconds — no redeploy.`
      );
      await load();
    } catch (e: unknown) {
      // The router refuses an unsafe document and says why; show that verbatim.
      setError(e instanceof Error ? e.message : "The policy was rejected");
    } finally {
      setSaving(false);
    }
  };

  if (error && !policy) {
    return <div className="text-red-700 bg-red-50 px-3 py-2 rounded-md text-sm">{error}</div>;
  }
  if (!policy) {
    return <div className="text-gray-400 py-8 text-center">Loading…</div>;
  }

  const autonomy = (policy.config.autonomy ?? {}) as Record<string, unknown>;
  const ceilings = (autonomy.category_ceilings_usd ?? {}) as Record<string, string>;

  return (
    <div>
      <h2 className="text-lg font-bold mt-0 mb-1">Policy &amp; autonomy configuration</h2>
      <p className="text-sm text-gray-600 mt-0 mb-4">
        Version {String(policy.config.version)} · last changed by{" "}
        {String(policy.config.updated_by)} · {String(policy.config.updated_at || "—")}
      </p>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-5">
        <div className="border border-gray-200 rounded-lg p-4 bg-white">
          <h3 className="mt-0 mb-2 text-base font-bold">Current posture</h3>
          <div className="text-sm flex justify-between py-1 border-b border-gray-100">
            <span className="text-gray-600">Confidence bar</span>
            <span className="font-semibold">
              {String(autonomy.confidence_threshold)}
            </span>
          </div>
          {Object.entries(ceilings).map(([category, ceiling]) => (
            <div
              key={category}
              className="text-sm flex justify-between py-1 border-b border-gray-100 last:border-b-0"
            >
              <span className="text-gray-600">{category} ceiling</span>
              <span className="font-semibold">${ceiling}</span>
            </div>
          ))}
        </div>

        <div className="border border-amber-200 rounded-lg p-4 bg-amber-50">
          <h3 className="mt-0 mb-2 text-base font-bold text-amber-900">
            Limits configuration cannot cross
          </h3>
          <p className="text-sm text-amber-900 m-0">
            Maximum ceiling{" "}
            <strong>${policy.hard_limits.absolute_max_ceiling_usd}</strong>, minimum
            confidence bar <strong>{policy.hard_limits.min_confidence_threshold}</strong>.
          </p>
          <p className="text-xs text-amber-800 mt-2 mb-0">{policy.hard_limits.note}</p>
        </div>
      </div>

      <div className="border border-gray-200 rounded-lg p-4 bg-white mb-5">
        <h3 className="mt-0 mb-2 text-base font-bold">Rule catalogue</h3>
        <table className="w-full text-sm border-collapse">
          <thead>
            <tr className="text-left text-gray-500 text-xs">
              <th className="py-1">Rule</th>
              <th className="py-1">Outcome</th>
              <th className="py-1">Applies to</th>
              <th className="py-1">Why</th>
            </tr>
          </thead>
          <tbody>
            {policy.rules.map((rule) => (
              <tr key={rule.rule_id} className="border-t border-gray-100">
                <td className="py-1 font-mono text-xs">{rule.rule_id}</td>
                <td className="py-1">{rule.outcome.replace(/_/g, " ")}</td>
                <td className="py-1 text-xs">{rule.categories.join(", ")}</td>
                <td className="py-1 text-xs text-gray-600">{rule.reason}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <label htmlFor="policy-json" className="block text-sm font-semibold mb-1">
        Full document
      </label>
      <textarea
        id="policy-json"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        spellCheck={false}
        rows={22}
        className="w-full font-mono text-xs p-3 border border-gray-300 rounded-md"
      />

      {error && (
        <div className="text-red-700 bg-red-50 px-3 py-2 rounded-md text-sm mt-2">
          {error}
        </div>
      )}
      {notice && (
        <div className="text-green-800 bg-green-50 px-3 py-2 rounded-md text-sm mt-2">
          {notice}
        </div>
      )}

      <div className="flex gap-2 mt-3">
        <button
          className="px-4 py-2 bg-slate-800 text-white rounded-md font-semibold text-sm hover:bg-slate-700 cursor-pointer border-none disabled:opacity-50"
          disabled={saving}
          onClick={handleSave}
        >
          {saving ? "Saving…" : "Save new version"}
        </button>
        <button
          className="px-4 py-2 bg-gray-200 text-gray-800 rounded-md text-sm cursor-pointer border-none"
          onClick={load}
        >
          Discard changes
        </button>
      </div>
    </div>
  );
}
