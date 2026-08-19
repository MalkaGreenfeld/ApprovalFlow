import { useState, useEffect, useRef } from "react";
import { getSubmissionStatus, provideInfo, type SubmissionStatus } from "./api";

const POLL_INTERVAL_MS = 5000;

const TERMINAL_STATUSES = new Set([
  "paid",
  "rejected",
  "human_rejected",
  "duplicate",
  "payment_failed",
  "compensated",
]);

/** Fields a submitter can be asked for, and how to render each one. */
const FIELD_LABELS: Record<string, string> = {
  receiptPresent: "Receipt attached",
  attendees: "Number of attendees",
  notes: "Additional explanation",
  category: "Corrected category",
  total: "Corrected total",
  taxAmount: "Corrected tax amount",
};

function statusBadgeClass(status: string): string {
  const base =
    "inline-block px-2 py-0.5 rounded-full text-xs font-semibold capitalize";
  switch (status) {
    case "auto_approved":
    case "human_approved":
    case "paid":
      return `${base} bg-green-100 text-green-800`;
    case "human_review":
    case "info_requested":
    case "received":
    case "reanalyzing":
      return `${base} bg-yellow-100 text-yellow-800`;
    case "rejected":
    case "human_rejected":
    case "duplicate":
    case "payment_failed":
    case "compensated":
      return `${base} bg-red-100 text-red-800`;
    default:
      return base;
  }
}

export function StatusView() {
  const [correlationId, setCorrelationId] = useState("");
  const [status, setStatus] = useState<SubmissionStatus | null>(null);
  const [error, setError] = useState("");
  const [answer, setAnswer] = useState("");
  const [updates, setUpdates] = useState<Record<string, string | boolean>>({});
  const [sending, setSending] = useState(false);
  const [sent, setSent] = useState("");
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const stopPolling = () => {
    if (pollRef.current !== null) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  };

  useEffect(() => stopPolling, []);

  const fetchStatus = async (id: string) => {
    try {
      const result = await getSubmissionStatus(id);
      setStatus(result);
      if (TERMINAL_STATUSES.has(result.status)) stopPolling();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Error looking up status");
      stopPolling();
    }
  };

  const startPolling = (id: string) => {
    stopPolling();
    fetchStatus(id);
    pollRef.current = setInterval(() => fetchStatus(id), POLL_INTERVAL_MS);
  };

  const handleLookup = async () => {
    setError("");
    setSent("");
    setStatus(null);
    stopPolling();

    if (!correlationId.trim()) {
      setError("Please enter a Correlation ID");
      return;
    }
    startPolling(correlationId.trim());
  };

  const handleProvideInfo = async () => {
    if (!status) return;
    setError("");
    setSending(true);
    try {
      const result = await provideInfo(status.correlation_id, answer, updates);
      setSent(`Sent — the workflow resumed at revision ${result.revision}.`);
      setAnswer("");
      setUpdates({});
      await fetchStatus(status.correlation_id);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Could not send the information");
    } finally {
      setSending(false);
    }
  };

  const infoRequest = status?.info_request;
  const requestedFields = infoRequest?.requested_fields ?? [];

  return (
    <div className="max-w-2xl">
      <div className="flex gap-2 mb-4">
        <input
          placeholder="Enter Correlation ID"
          value={correlationId}
          onChange={(e) => setCorrelationId(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && handleLookup()}
          className="flex-1 px-3 py-2 border border-gray-300 rounded-md text-sm focus:border-slate-800 focus:outline-none focus:ring-2 focus:ring-slate-800/15"
        />
        <button
          className="px-4 py-2 bg-slate-800 text-white rounded-md font-semibold text-sm hover:bg-slate-700 cursor-pointer border-none"
          onClick={handleLookup}
        >
          Check Status
        </button>
      </div>

      {error && (
        <div className="text-red-700 bg-red-50 px-3 py-2 rounded-md text-sm mb-3">
          {error}
        </div>
      )}
      {sent && (
        <div className="text-green-800 bg-green-50 px-3 py-2 rounded-md text-sm mb-3">
          {sent}
        </div>
      )}

      {status && (
        <div className="border border-gray-200 rounded-lg p-4 bg-white mb-4">
          <h3 className="mt-0 mb-3 text-base font-bold">Submission Status</h3>
          {[
            ["ID", status.correlation_id],
            ["Status", status.status],
            ["Vendor", status.vendor],
            ["Amount", `$${status.amount_usd}`],
            ["Category", status.category],
          ].map(([label, value]) => (
            <div
              key={label}
              className="flex justify-between py-1.5 border-b border-gray-100 text-sm last:border-b-0"
            >
              <span className="font-semibold text-gray-500">{label}</span>
              <span className={label === "Status" ? statusBadgeClass(String(value)) : ""}>
                {label === "ID" ? <span dir="ltr">{value}</span> : value}
              </span>
            </div>
          ))}

          {/* F2: the plain-language reason, not just a status code. */}
          {status.reason && (
            <p className="mt-3 mb-0 text-sm text-gray-700 bg-gray-50 rounded-md p-3">
              {status.reason}
              {status.rule_ids && status.rule_ids.length > 0 && (
                <span className="block mt-1 text-xs text-gray-500">
                  Policy rules applied: {status.rule_ids.join(", ")}
                </span>
              )}
            </p>
          )}
        </div>
      )}

      {/* F5: what the approver asked for, and the form to answer it. */}
      {infoRequest && (
        <div className="border-2 border-amber-300 rounded-lg p-4 bg-amber-50">
          <h3 className="mt-0 mb-1 text-base font-bold text-amber-900">
            More information needed
          </h3>
          <p className="text-sm text-amber-900 mb-3">
            {infoRequest.question || "The approver needs more detail before deciding."}
            {infoRequest.requested_by && (
              <span className="block text-xs mt-1 opacity-75">
                Requested by {infoRequest.requested_by}
              </span>
            )}
          </p>

          {requestedFields.map((field) => (
            <div key={field} className="mb-3">
              <label
                htmlFor={`field-${field}`}
                className="block text-sm font-semibold text-amber-900 mb-1"
              >
                {FIELD_LABELS[field] || field}
              </label>
              {field === "receiptPresent" ? (
                <input
                  id={`field-${field}`}
                  type="checkbox"
                  checked={Boolean(updates[field])}
                  onChange={(e) =>
                    setUpdates({ ...updates, [field]: e.target.checked })
                  }
                />
              ) : (
                <input
                  id={`field-${field}`}
                  value={String(updates[field] ?? "")}
                  onChange={(e) => setUpdates({ ...updates, [field]: e.target.value })}
                  className="w-full px-3 py-2 border border-amber-300 rounded-md text-sm"
                />
              )}
            </div>
          ))}

          <label
            htmlFor="info-answer"
            className="block text-sm font-semibold text-amber-900 mb-1"
          >
            Your reply
          </label>
          <textarea
            id="info-answer"
            rows={3}
            value={answer}
            onChange={(e) => setAnswer(e.target.value)}
            placeholder="Explain or provide what was requested"
            className="w-full px-3 py-2 border border-amber-300 rounded-md text-sm mb-3"
          />

          <button
            className="px-4 py-2 bg-amber-600 text-white rounded-md font-semibold text-sm hover:bg-amber-700 cursor-pointer border-none disabled:opacity-50"
            disabled={sending || (!answer.trim() && Object.keys(updates).length === 0)}
            onClick={handleProvideInfo}
          >
            {sending ? "Sending…" : "Send and resume"}
          </button>
        </div>
      )}

      {/* The full question/answer thread, so the submitter can see the history. */}
      {status?.info_exchange && status.info_exchange.length > 0 && (
        <details className="mt-4 text-sm">
          <summary className="cursor-pointer font-semibold text-gray-700">
            Information history ({status.info_exchange.length})
          </summary>
          <pre className="bg-gray-50 p-3 rounded-md overflow-x-auto text-xs mt-2">
            {JSON.stringify(status.info_exchange, null, 2)}
          </pre>
        </details>
      )}
    </div>
  );
}
