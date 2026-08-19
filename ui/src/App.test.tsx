import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "./App";

// The whole api module is mocked. App mounts a screen per tab, and two of those
// screens fetch on mount, so without this the tab-switching tests would depend on
// something answering on localhost:8080: green on a machine with the stack up and
// red in CI.
vi.mock("./api", () => ({
  currentIdentity: vi.fn(() => null),
  signIn: vi.fn(),
  signOut: vi.fn(),
  submitInvoice: vi.fn(),
  getSubmissionStatus: vi.fn(),
  provideInfo: vi.fn(),
  listApprovals: vi.fn(),
  approveSubmission: vi.fn(),
  rejectSubmission: vi.fn(),
  sendBackSubmission: vi.fn(),
  getDashboard: vi.fn(),
  getCeilingProof: vi.fn(),
  getPolicy: vi.fn(),
  updatePolicy: vi.fn(),
  getAuditTrail: vi.fn(),
}));

import { listApprovals, getDashboard, getCeilingProof, getPolicy } from "./api";

const mockList = listApprovals as ReturnType<typeof vi.fn>;
const mockDashboard = getDashboard as ReturnType<typeof vi.fn>;
const mockProof = getCeilingProof as ReturnType<typeof vi.fn>;
const mockPolicy = getPolicy as ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.clearAllMocks();
  mockList.mockResolvedValue({ approvals: [], count: 0 });
  mockDashboard.mockResolvedValue({
    window_hours: 24,
    items: 0,
    decisions: 0,
    routes: {},
    rates: { auto_approval_rate: 0, escalation_rate: 0, router_rounds: 0 },
    money_usd: { auto_approved: "0.00", human_approved: "0.00" },
    avg_agent_confidence: null,
    avg_decision_latency_seconds: null,
    top_rules: [],
    throughput_per_hour: [],
    policy: {
      version: 1,
      confidence_threshold: "0.85",
      ceilings_usd: { meals: "750" },
      default_ceiling_usd: "350",
    },
  });
  mockProof.mockResolvedValue({
    auto_approvals_examined: 0,
    ceiling_violations: 0,
    confidence_violations: 0,
    auto_approvals_not_made_by_router: 0,
    max_auto_approved_amount_usd: "0.00",
    highest_ceiling_in_use_usd: "750.00",
    per_category: [],
    offending_items: [],
    holds: true,
    method: "",
  });
  mockPolicy.mockResolvedValue({
    config: {
      version: 1,
      updated_by: "bootstrap",
      updated_at: "",
      autonomy: {
        confidence_threshold: "0.85",
        default_ceiling_usd: "350",
        category_ceilings_usd: { meals: "750" },
      },
    },
    rules: [],
    hard_limits: {
      absolute_max_ceiling_usd: "2000",
      min_confidence_threshold: "0.50",
      note: "Compiled into the router; configuration cannot cross these.",
    },
  });
});

describe("App", () => {
  it("renders the layout with New Submission tab active by default", () => {
    render(<App />);

    expect(screen.getByText("ApprovalFlow")).toBeInTheDocument();
    expect(screen.getByText("Submit for Approval")).toBeInTheDocument();
  });

  it("shows SubmitForm content on New Submission tab", () => {
    render(<App />);

    expect(screen.getByLabelText("Submitter Email*")).toBeInTheDocument();
    expect(screen.getByLabelText("Vendor*")).toBeInTheDocument();
  });

  it("switches to Track Status tab", async () => {
    const user = userEvent.setup();
    render(<App />);

    await user.click(screen.getByText("Track Status"));

    expect(screen.getByPlaceholderText("Enter Correlation ID")).toBeInTheDocument();
    expect(screen.getByText("Check Status")).toBeInTheDocument();
  });

  it("switches to Approval Queue tab and shows the empty state", async () => {
    const user = userEvent.setup();
    render(<App />);

    await user.click(screen.getByText("Approval Queue"));

    expect(await screen.findByText("No pending approvals")).toBeInTheDocument();
    expect(mockList).toHaveBeenCalled();
  });

  it("switches to the Dashboard tab", async () => {
    const user = userEvent.setup();
    render(<App />);

    await user.click(screen.getByText("Dashboard"));

    expect(await screen.findByText("Controller dashboard")).toBeInTheDocument();
    expect(mockDashboard).toHaveBeenCalled();
  });

  it("switches to the Policy tab", async () => {
    const user = userEvent.setup();
    render(<App />);

    await user.click(screen.getByText("Policy"));

    expect(await screen.findByText("Current posture")).toBeInTheDocument();
    expect(mockPolicy).toHaveBeenCalled();
  });
});
