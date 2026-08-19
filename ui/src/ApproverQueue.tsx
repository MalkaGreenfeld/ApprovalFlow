import { useEffect, useState, useRef } from "react";
import {
  listApprovals,
  approveSubmission,
  rejectSubmission,
  sendBackSubmission,
  type ApprovalItem,
} from "./api";

const POLL_INTERVAL_MS = 5000;

/** What an approver can ask a submitter to supply. Mirrors amendable_fields. */
const REQUESTABLE_FIELDS: { value: string; label: string }[] = [
  { value: "receiptPresent", label: "Receipt" },
  { value: "attendees", label: "Attendee count" },
  { value: "notes", label: "Business justification / client name" },
  { value: "category", label: "Corrected category" },
  { value: "total", label: "Corrected total" },
];

function confidenceLabel(value: number | string | null | undefined): string {
  if (value === null || value === undefined || value === "") return "n/a";
  return `${(Number(value) * 100).toFixed(0)}%`;
}

export function ApproverQueue() {
  const [approvals, setApprovals] = useState<ApprovalItem[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  // The item whose send-back form is open, plus its draft request.
  const [sendBackFor, setSendBackFor] = useState<string | null>(null);
  const [question, setQuestion] = useState("");
  const [fields, setFields] = useState<string[]>([]);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const load = async () => {
    setError("");
    try {
      const result = await listApprovals("pending");
      setApprovals(result.approvals);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Error loading approvals");
    }
  };

  useEffect(() => {
    load();
    pollRef.current = setInterval(load, POLL_INTERVAL_MS);
    return () => {
      if (pollRef.current !== null) clearInterval(pollRef.current);
    };
  }, []);

  const resetSendBack = () => {
    setSendBackFor(null);
    setQuestion("");
    setFields([]);
  };

  const handleAction = async (
    correlationId: string,
    action: "approve" | "reject"
  ) => {
    setError("");
    setBusy(correlationId);
    try {
      if (action === "approve") await approveSubmission(correlationId, "", "");
      else await rejectSubmission(correlationId, "", "");
      await load();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Error performing action");
    } finally {
      setBusy(null);
    }
  };

  const handleSendBack = async (correlationId: string) => {
    setError("");
    setBusy(correlationId);
    try {
      await sendBackSubmission(correlationId, "", question, fields);
      resetSendBack();
      await load();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Error sending the request");
    } finally {
      setBusy(null);
    }
  };

  if (approvals.length === 0 && !error) {
    return (
      <div className="text-center py-8 text-gray-400 text-base">
        No pending approvals
      </div>
    );
  }

  return (
    <div className="max-w-3xl">
      {error && (
        <div className="text-red-700 bg-red-50 px-3 py-2 rounded-md text-sm mb-3">
          {error}
        </div>
      )}

      <ul className="list-none p-0 m-0 flex flex-col gap-3">
        {approvals.map((item) => (
          <li
            key={item.correlation_id}
            className="border border-gray-200 rounded-lg p-4 bg-white"
            role="listitem"
          >
            <div className="flex justify-between items-center flex-wrap gap-2 mb-2.5">
              <span className="font-bold text-base">{item.vendor}</span>
              <span className="font-semibold text-slate-800">
                ${Number(item.amount_usd).toLocaleString()}
              </span>
            </div>

            <div className="text-xs text-gray-400 flex gap-4 flex-wrap mb-3">
              <span>{item.category}</span>
              <span>{item.submitter_email}</span>
              {item.revision ? <span>revision {item.revision}</span> : null}
              <span dir="ltr">{item.correlation_id}</span>
            </div>

            {/* F4: why this is here, so the approver is not rubber-stamping blind. */}
            <div className="bg-slate-50 rounded-md p-3 text-sm mb-3">
              <div className="font-semibold text-slate-700 mb-1">
                Why this needs you
              </div>
              <div className="text-slate-700">
                {item.escalation_reason || "Escalated by the deterministic router."}
              </div>
              <div className="text-xs text-slate-500 mt-2 flex gap-4 flex-wrap">
                <span>
                  Agent recommended: <strong>{item.agent_recommendation ?? "n/a"}</strong>
                </span>
                <span>
                  Confidence: <strong>{confidenceLabel(item.agent_confidence)}</strong>
                </span>
                {item.rule_ids && item.rule_ids.length > 0 && (
                  <span>
                    Rules cited: <strong>{item.rule_ids.join(", ")}</strong>
                  </span>
                )}
              </div>
              {item.agent_reasoning && (
                <details className="mt-2">
                  <summary className="cursor-pointer text-xs text-slate-600">
                    Agent reasoning
                  </summary>
                  <p className="text-xs text-slate-600 mt-1 mb-0">
                    {item.agent_reasoning}
                  </p>
                </details>
              )}
            </div>

            {/* The thread, when this item has already been sent back once. */}
            {item.info_exchange && item.info_exchange.length > 0 && (
              <details className="mb-3 text-sm">
                <summary className="cursor-pointer font-semibold text-gray-700">
                  Information supplied by the submitter ({item.info_exchange.length})
                </summary>
                <pre className="bg-gray-50 p-3 rounded-md overflow-x-auto text-xs mt-2">
                  {JSON.stringify(item.info_exchange, null, 2)}
                </pre>
              </details>
            )}

            {sendBackFor === item.correlation_id ? (
              <div className="border border-amber-300 bg-amber-50 rounded-md p-3">
                <label
                  htmlFor={`question-${item.correlation_id}`}
                  className="block text-sm font-semibold text-amber-900 mb-1"
                >
                  What do you need from the submitter or vendor?
                </label>
                <textarea
                  id={`question-${item.correlation_id}`}
                  rows={2}
                  value={question}
                  onChange={(e) => setQuestion(e.target.value)}
                  placeholder="e.g. Please attach the receipt and name the client"
                  className="w-full px-3 py-2 border border-amber-300 rounded-md text-sm mb-2"
                />
                <fieldset className="border-none p-0 m-0 mb-2">
                  <legend className="text-xs font-semibold text-amber-900 mb-1">
                    Fields to request
                  </legend>
                  <div className="flex gap-3 flex-wrap">
                    {REQUESTABLE_FIELDS.map((field) => (
                      <label key={field.value} className="text-xs flex items-center gap-1">
                        <input
                          type="checkbox"
                          checked={fields.includes(field.value)}
                          onChange={(e) =>
                            setFields(
                              e.target.checked
                                ? [...fields, field.value]
                                : fields.filter((f) => f !== field.value)
                            )
                          }
                        />
                        {field.label}
                      </label>
                    ))}
                  </div>
                </fieldset>
                <div className="flex gap-2">
                  <button
                    className="px-4 py-1.5 bg-amber-600 text-white rounded-md font-semibold text-sm hover:bg-amber-700 cursor-pointer border-none disabled:opacity-50"
                    disabled={question.trim().length < 3 || busy === item.correlation_id}
                    onClick={() => handleSendBack(item.correlation_id)}
                  >
                    Send request
                  </button>
                  <button
                    className="px-4 py-1.5 bg-gray-200 text-gray-800 rounded-md text-sm cursor-pointer border-none"
                    onClick={resetSendBack}
                  >
                    Cancel
                  </button>
                </div>
              </div>
            ) : (
              <div className="flex gap-2">
                <button
                  className="px-4 py-1.5 bg-green-600 text-white rounded-md font-semibold text-sm hover:bg-green-700 cursor-pointer border-none disabled:opacity-50"
                  disabled={busy === item.correlation_id}
                  onClick={() => handleAction(item.correlation_id, "approve")}
                >
                  Approve
                </button>
                <button
                  className="px-4 py-1.5 bg-red-600 text-white rounded-md font-semibold text-sm hover:bg-red-700 cursor-pointer border-none disabled:opacity-50"
                  disabled={busy === item.correlation_id}
                  onClick={() => handleAction(item.correlation_id, "reject")}
                >
                  Reject
                </button>
                <button
                  className="px-4 py-1.5 bg-amber-500 text-white rounded-md font-semibold text-sm hover:bg-amber-600 cursor-pointer border-none"
                  onClick={() => setSendBackFor(item.correlation_id)}
                >
                  Send Back
                </button>
              </div>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
