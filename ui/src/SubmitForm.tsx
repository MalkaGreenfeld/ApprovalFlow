import { useState } from "react";
import { submitInvoice, type SubmissionPayload } from "./api";

const EMPTY: SubmissionPayload = {
  submitter: "",
  department: "",
  vendor: "",
  vendorKnown: true,
  invoiceNumber: "",
  currency: "USD",
  total: 0,
  taxAmount: 0,
  category: "",
  attendees: 0,
  receiptPresent: false,
  notes: "",
  lineItems: [],
};

const INPUT_CLASS =
  "w-full px-3 py-2 border border-gray-300 rounded-md text-sm focus:border-slate-800 focus:outline-none focus:ring-2 focus:ring-slate-800/15";
const LABEL_CLASS = "text-sm font-semibold text-gray-700";

export function SubmitForm() {
  const [form, setForm] = useState<SubmissionPayload>(EMPTY);
  const [error, setError] = useState("");
  const [result, setResult] = useState<{
    correlation_id: string;
    status: string;
  } | null>(null);
  const [loading, setLoading] = useState(false);

  const set = (field: keyof SubmissionPayload, value: string | number | boolean) => {
    setForm((prev) => ({ ...prev, [field]: value }));
  };

  /** Numeric fields (amount, tax) never make sense negative. */
  const setNonNegative = (field: keyof SubmissionPayload, raw: string) => {
    set(field, Math.max(0, Number(raw) || 0));
  };

  const handleSubmit = async () => {
    setError("");

    if (!form.submitter || !form.department || !form.vendor || !form.invoiceNumber || !form.total || !form.category) {
      setError("Please fill in all required fields");
      return;
    }

    setLoading(true);
    try {
      const res = await submitInvoice(form);
      setResult(res);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Error submitting request");
    } finally {
      setLoading(false);
    }
  };

  if (result) {
    return (
      <div className="p-4 bg-green-50 text-green-700 rounded-lg">
        <h3 className="text-lg font-bold mb-2">Submission received successfully!</h3>
        <p className="text-sm text-left break-all">Tracking ID: {result.correlation_id}</p>
        <p className="text-sm">Status: {result.status}</p>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4 max-w-xl">
      <Field label="Submitter Email*" id="submitter" type="email" value={form.submitter} onChange={(v) => set("submitter", v)} />
      <Field label="Department*" id="department" value={form.department} onChange={(v) => set("department", v)} />
      <Field label="Vendor*" id="vendor" value={form.vendor} onChange={(v) => set("vendor", v)} />
      <Field label="Invoice Number*" id="invoiceNumber" value={form.invoiceNumber} onChange={(v) => set("invoiceNumber", v)} />

      <div className="flex flex-col gap-1">
        <label htmlFor="currency" className={LABEL_CLASS}>Currency</label>
        <select
          id="currency"
          value={form.currency}
          onChange={(e) => set("currency", e.target.value)}
          className={INPUT_CLASS}
        >
          <option value="USD">USD</option>
          <option value="ILS">ILS</option>
          <option value="EUR">EUR</option>
        </select>
      </div>

      <Field label="Amount*" id="total" type="number" min={0} value={form.total || ""} onChange={(v) => setNonNegative("total", v)} />
      <Field label="Tax" id="taxAmount" type="number" min={0} value={form.taxAmount || ""} onChange={(v) => setNonNegative("taxAmount", v)} />
      <Field label="Category*" id="category" value={form.category} onChange={(v) => set("category", v)} />
      <Field label="Notes" id="notes" value={form.notes} onChange={(v) => set("notes", v)} />

      <label className="flex items-center gap-2 text-sm font-semibold text-gray-700">
        <input
          type="checkbox"
          checked={form.receiptPresent}
          onChange={(e) => set("receiptPresent", e.target.checked)}
        />
        Receipt present
      </label>
      {/* vendorKnown is deliberately not submitter-editable: it's a finance-controlled
          fact (see services/ingestion/app/validation.py AMENDABLE_COLUMNS comment) —
          exposing it here would let a submitter self-certify past GLOBAL-VENDOR. */}

      {error && <div className="text-red-700 bg-red-50 px-3 py-2 rounded-md text-sm">{error}</div>}

      <button
        className="self-start px-5 py-2 bg-slate-800 text-white rounded-md font-semibold text-sm hover:bg-slate-700 disabled:opacity-60 disabled:cursor-not-allowed cursor-pointer border-none"
        onClick={handleSubmit}
        disabled={loading}
      >
        {loading ? "Submitting..." : "Submit for Approval"}
      </button>
    </div>
  );
}

/* ── internal field helper ── */

function Field({
  label,
  id,
  value,
  onChange,
  type = "text",
  min,
}: {
  label: string;
  id: string;
  value: string | number;
  onChange: (v: string) => void;
  type?: string;
  min?: number;
}) {
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className={LABEL_CLASS}>{label}</label>
      <input
        id={id}
        type={type}
        min={min}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className={INPUT_CLASS}
      />
    </div>
  );
}
