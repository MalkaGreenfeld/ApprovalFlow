import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApproverQueue } from "./ApproverQueue";

vi.mock("./api", () => ({
  listApprovals: vi.fn(),
  approveSubmission: vi.fn(),
  rejectSubmission: vi.fn(),
  sendBackSubmission: vi.fn(),
}));

import { listApprovals, approveSubmission, rejectSubmission, sendBackSubmission } from "./api";

const mockList = listApprovals as ReturnType<typeof vi.fn>;
const mockApprove = approveSubmission as ReturnType<typeof vi.fn>;
const mockReject = rejectSubmission as ReturnType<typeof vi.fn>;
const mockSendBack = sendBackSubmission as ReturnType<typeof vi.fn>;

beforeEach(() => {
  mockList.mockReset();
  mockApprove.mockReset();
  mockReject.mockReset();
  mockSendBack.mockReset();
});

const sampleApprovals = [
  {
    correlation_id: "c1",
    status: "pending",
    vendor: "Quality Supplier Ltd.",
    amount_usd: 500,
    category: "office",
    submitter_email: "user@example.com",
  },
  {
    correlation_id: "c2",
    status: "pending",
    vendor: "Tech Parts Inc.",
    amount_usd: 1200,
    category: "equipment",
    submitter_email: "dev@example.com",
  },
];

describe("ApproverQueue", () => {
  it("loads and displays pending approvals on mount", async () => {
    mockList.mockResolvedValueOnce({ approvals: sampleApprovals });

    render(<ApproverQueue />);

    expect(await screen.findByText("Quality Supplier Ltd.")).toBeInTheDocument();
    expect(await screen.findByText("Tech Parts Inc.")).toBeInTheDocument();
  });

  it("shows approve/reject/send-back buttons per item", async () => {
    mockList.mockResolvedValueOnce({ approvals: sampleApprovals });

    render(<ApproverQueue />);

    await screen.findByText("Quality Supplier Ltd.");

    const approveBtns = screen.getAllByText("Approve");
    const rejectBtns = screen.getAllByText("Reject");
    const sendBackBtns = screen.getAllByText("Send Back");

    expect(approveBtns).toHaveLength(2);
    expect(rejectBtns).toHaveLength(2);
    expect(sendBackBtns).toHaveLength(2);
  });

  it("calls approveSubmission when Approve is clicked", async () => {
    const user = userEvent.setup();
    mockList.mockResolvedValueOnce({ approvals: sampleApprovals });
    mockApprove.mockResolvedValueOnce({ status: "SUCCESS" });
    mockList.mockResolvedValueOnce({ approvals: [] });

    render(<ApproverQueue />);

    await screen.findByText("Quality Supplier Ltd.");

    const rows = screen.getAllByRole("listitem");
    const approveBtn = within(rows[0]).getByText("Approve");
    await user.click(approveBtn);

    expect(mockApprove).toHaveBeenCalledWith("c1", "", "");
  });

  it("calls rejectSubmission when Reject is clicked", async () => {
    const user = userEvent.setup();
    mockList.mockResolvedValueOnce({ approvals: sampleApprovals });
    mockReject.mockResolvedValueOnce({ status: "SUCCESS" });
    mockList.mockResolvedValueOnce({ approvals: [] });

    render(<ApproverQueue />);

    await screen.findByText("Quality Supplier Ltd.");

    const rows = screen.getAllByRole("listitem");
    const rejectBtn = within(rows[0]).getByText("Reject");
    await user.click(rejectBtn);

    expect(mockReject).toHaveBeenCalledWith("c1", "", "");
  });

  it("opens a request form instead of sending an empty send-back (F5)", async () => {
    const user = userEvent.setup();
    mockList.mockResolvedValue({ approvals: sampleApprovals });

    render(<ApproverQueue />);
    await screen.findByText("Quality Supplier Ltd.");

    const rows = screen.getAllByRole("listitem");
    await user.click(within(rows[0]).getByText("Send Back"));

    // Nothing is sent until the approver says what they need.
    expect(mockSendBack).not.toHaveBeenCalled();
    expect(
      screen.getByLabelText("What do you need from the submitter or vendor?")
    ).toBeInTheDocument();
    expect(screen.getByText("Send request")).toBeDisabled();
  });

  it("sends the question and the requested fields", async () => {
    const user = userEvent.setup();
    mockList.mockResolvedValue({ approvals: sampleApprovals });
    mockSendBack.mockResolvedValueOnce({ status: "SUCCESS" });

    render(<ApproverQueue />);
    await screen.findByText("Quality Supplier Ltd.");

    const rows = screen.getAllByRole("listitem");
    await user.click(within(rows[0]).getByText("Send Back"));

    await user.type(
      screen.getByLabelText("What do you need from the submitter or vendor?"),
      "Please attach the receipt"
    );
    await user.click(screen.getByLabelText("Receipt"));
    await user.click(screen.getByText("Send request"));

    expect(mockSendBack).toHaveBeenCalledWith(
      "c1",
      "",
      "Please attach the receipt",
      ["receiptPresent"]
    );
  });

  it("shows the agent rationale so the approver is not rubber-stamping (F4)", async () => {
    mockList.mockResolvedValue({
      approvals: [
        {
          ...sampleApprovals[0],
          agent_recommendation: "human_review",
          agent_confidence: 0.62,
          rule_ids: ["MEAL-02", "AUTONOMY-CEILING"],
          escalation_reason: "Client entertainment over $500 without a named client",
        },
      ],
    });

    render(<ApproverQueue />);

    expect(
      await screen.findByText("Client entertainment over $500 without a named client")
    ).toBeInTheDocument();
    expect(screen.getByText("62%")).toBeInTheDocument();
    expect(screen.getByText("MEAL-02, AUTONOMY-CEILING")).toBeInTheDocument();
  });

  it("shows empty state when no pending approvals", async () => {
    mockList.mockResolvedValue({ approvals: [] });

    render(<ApproverQueue />);

    expect(await screen.findByText("No pending approvals")).toBeInTheDocument();
  });

  it("registers polling with 5-second interval on mount", async () => {
    const setIntervalSpy = vi.spyOn(globalThis, "setInterval");

    mockList.mockResolvedValue({ approvals: sampleApprovals });

    render(<ApproverQueue />);

    expect(await screen.findByText("Quality Supplier Ltd.")).toBeInTheDocument();
    expect(setIntervalSpy).toHaveBeenCalledWith(expect.any(Function), 5000);

    setIntervalSpy.mockRestore();
  });
});
