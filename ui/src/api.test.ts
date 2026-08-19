import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  submitInvoice,
  getSubmissionStatus,
  listApprovals,
  approveSubmission,
  rejectSubmission,
  sendBackSubmission,
  provideInfo,
} from "./api";

// Mock global fetch
const mockFetch = vi.fn();
vi.stubGlobal("fetch", mockFetch);

beforeEach(() => {
  mockFetch.mockReset();
});

// ── submitInvoice ────────────────────────────────────────────

describe("submitInvoice", () => {
  it("returns correlation_id and status on 202 response", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      status: 202,
      json: async () => ({ correlation_id: "abc-123", status: "received" }),
    });

    const result = await submitInvoice({
      submitter: "user@example.com",
      department: "Engineering",
      vendor: "Quality Supplies Ltd",
      vendorKnown: true,
      invoiceNumber: "INV-001",
      currency: "ILS",
      total: 1000,
      taxAmount: 170,
      category: "office",
      attendees: 0,
      receiptPresent: true,
      notes: "",
      lineItems: [{ description: "Printer paper", quantity: 10, unit_price: 100 }],
    });

    expect(result).toEqual({ correlation_id: "abc-123", status: "received" });
    expect(mockFetch).toHaveBeenCalledTimes(1);

    const [url, options] = mockFetch.mock.calls[0];
    expect(url).toContain("/api/submissions");
    expect(options.method).toBe("POST");
    expect(options.headers["Content-Type"]).toBe("application/json");
  });

  it("throws error with server message on non-ok response", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 422,
      json: async () => ({ detail: "Invalid payload" }),
    });

    await expect(
      submitInvoice({
        submitter: "",
        department: "",
        vendor: "",
        vendorKnown: false,
        invoiceNumber: "",
        currency: "ILS",
        total: 0,
        taxAmount: 0,
        category: "",
        attendees: 0,
        receiptPresent: false,
        notes: "",
        lineItems: [],
      })
    ).rejects.toThrow("Invalid payload");
  });
});

// ── getSubmissionStatus ──────────────────────────────────────

describe("getSubmissionStatus", () => {
  it("returns submission status for a given correlation_id", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        correlation_id: "abc-123",
        status: "auto_approved",
        vendor: "Quality Supplies Ltd",
        amount_usd: "250.00",
        category: "office",
      }),
    });

    const result = await getSubmissionStatus("abc-123");

    expect(result).toEqual({
      correlation_id: "abc-123",
      status: "auto_approved",
      vendor: "Quality Supplies Ltd",
      amount_usd: "250.00",
      category: "office",
    });
    expect(mockFetch).toHaveBeenCalledTimes(1);
    expect(mockFetch.mock.calls[0][0]).toContain("/api/submissions/abc-123/status");
  });
});

// ── listApprovals ────────────────────────────────────────────

describe("listApprovals", () => {
  it("retrieves all approvals when no status filter", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        approvals: [
          {
            correlation_id: "c1",
            status: "pending",
            vendor: "Quality Supplies Ltd",
            amount_usd: 100,
            category: "office",
            submitter_email: "a@example.com",
          },
        ],
      }),
    });

    const result = await listApprovals();

    expect(result.approvals).toHaveLength(1);
    expect(mockFetch.mock.calls[0][0]).toContain("/api/approvals");
  });

  it("appends status query param when provided", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ approvals: [] }),
    });

    await listApprovals("pending");

    expect(mockFetch.mock.calls[0][0]).toContain("?status=pending");
  });
});

// ── approveSubmission ────────────────────────────────────────

describe("approveSubmission", () => {
  it("sends approve action with approver_email and comment", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ status: "SUCCESS" }),
    });

    const result = await approveSubmission("abc-123", "ravid@example.com", "Looks good");

    expect(result).toEqual({ status: "SUCCESS" });
    expect(mockFetch).toHaveBeenCalledTimes(1);

    const [url, options] = mockFetch.mock.calls[0];
    expect(url).toContain("/api/approvals/abc-123/approve");
    expect(options.method).toBe("POST");
    const body = JSON.parse(options.body);
    expect(body.approver_email).toBe("ravid@example.com");
    expect(body.comment).toBe("Looks good");
  });
});

// ── rejectSubmission ─────────────────────────────────────────

describe("rejectSubmission", () => {
  it("sends reject action with approver_email and comment", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ status: "SUCCESS" }),
    });

    const result = await rejectSubmission("abc-123", "ravid@example.com", "Missing information");

    expect(result).toEqual({ status: "SUCCESS" });

    const [url, options] = mockFetch.mock.calls[0];
    expect(url).toContain("/api/approvals/abc-123/reject");
    expect(options.method).toBe("POST");
    const body = JSON.parse(options.body);
    expect(body.approver_email).toBe("ravid@example.com");
    expect(body.comment).toBe("Missing information");
  });
});

// ── sendBackSubmission ───────────────────────────────────────

describe("sendBackSubmission", () => {
  it("sends the question and the requested fields, not a bare comment", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ status: "SUCCESS" }),
    });

    const result = await sendBackSubmission(
      "abc-123",
      "ravid@example.com",
      "Please attach the receipt and name the client",
      ["receiptPresent", "notes"]
    );

    expect(result).toEqual({ status: "SUCCESS" });

    const [url, options] = mockFetch.mock.calls[0];
    expect(url).toContain("/api/approvals/abc-123/send-back");
    expect(options.method).toBe("POST");
    const body = JSON.parse(options.body);
    expect(body.approver_email).toBe("ravid@example.com");
    expect(body.question).toBe("Please attach the receipt and name the client");
    expect(body.requested_fields).toEqual(["receiptPresent", "notes"]);
  });

  it("defaults requested_fields to an empty list", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ status: "SUCCESS" }),
    });

    await sendBackSubmission("abc-123", "ravid@example.com", "Need more detail");

    const body = JSON.parse(mockFetch.mock.calls[0][1].body);
    expect(body.requested_fields).toEqual([]);
  });
});

// ── provideInfo (F5, submitter side) ─────────────────────────

describe("provideInfo", () => {
  it("posts the answer and the amended fields to the info-response endpoint", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ status: "resumed", revision: 1, rejected_fields: [] }),
    });

    const result = await provideInfo("abc-123", "Receipt attached", {
      receiptPresent: true,
    });

    expect(result.revision).toBe(1);
    const [url, options] = mockFetch.mock.calls[0];
    expect(url).toContain("/api/submissions/abc-123/info-response");
    expect(options.method).toBe("POST");
    const body = JSON.parse(options.body);
    expect(body.answer).toBe("Receipt attached");
    expect(body.updates).toEqual({ receiptPresent: true });
  });
});

// ── Error translation ────────────────────────────────────────

describe("error handling", () => {
  it("asks the user to sign in on 401", async () => {
    mockFetch.mockResolvedValueOnce({ ok: false, status: 401, json: async () => ({}) });
    await expect(getSubmissionStatus("abc-123")).rejects.toThrow("Please sign in");
  });

  it("explains a 403 as a role problem", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 403,
      json: async () => ({ detail: "Requires one of roles: admin" }),
    });
    await expect(getSubmissionStatus("abc-123")).rejects.toThrow(
      "Requires one of roles: admin"
    );
  });

  it("explains gateway rate limiting on 429", async () => {
    mockFetch.mockResolvedValueOnce({ ok: false, status: 429, json: async () => ({}) });
    await expect(getSubmissionStatus("abc-123")).rejects.toThrow("rate-limiting");
  });
});
