import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SubmitForm } from "./SubmitForm";

vi.mock("./api", () => ({
  submitInvoice: vi.fn(),
}));

import { submitInvoice } from "./api";

const mockSubmitInvoice = submitInvoice as ReturnType<typeof vi.fn>;

beforeEach(() => {
  mockSubmitInvoice.mockReset();
});

describe("SubmitForm", () => {
  it("renders all required fields with labels", () => {
    render(<SubmitForm />);

    expect(screen.getByLabelText("Submitter Email*")).toBeInTheDocument();
    expect(screen.getByLabelText("Department*")).toBeInTheDocument();
    expect(screen.getByLabelText("Vendor*")).toBeInTheDocument();
    expect(screen.getByLabelText("Invoice Number*")).toBeInTheDocument();
    expect(screen.getByLabelText("Amount*")).toBeInTheDocument();
    expect(screen.getByLabelText("Category*")).toBeInTheDocument();
  });

  it("renders a submit button", () => {
    render(<SubmitForm />);
    expect(screen.getByRole("button", { name: "Submit for Approval" })).toBeInTheDocument();
  });

  it("shows validation errors when submitting empty form", async () => {
    const user = userEvent.setup();
    render(<SubmitForm />);

    await user.click(screen.getByRole("button", { name: "Submit for Approval" }));

    expect(screen.getByText("Please fill in all required fields")).toBeInTheDocument();
  });

  it("calls submitInvoice on valid submission and shows success", async () => {
    const user = userEvent.setup();
    mockSubmitInvoice.mockResolvedValueOnce({
      correlation_id: "cid-007",
      status: "received",
    });

    render(<SubmitForm />);

    await user.type(screen.getByLabelText("Submitter Email*"), "user@example.com");
    await user.type(screen.getByLabelText("Department*"), "Engineering");
    await user.type(screen.getByLabelText("Vendor*"), "Quality Supplier Ltd.");
    await user.type(screen.getByLabelText("Invoice Number*"), "INV-005");
    await user.type(screen.getByLabelText("Amount*"), "1500");
    await user.type(screen.getByLabelText("Tax"), "255");
    await user.type(screen.getByLabelText("Category*"), "office");

    await user.click(screen.getByRole("button", { name: "Submit for Approval" }));

    expect(mockSubmitInvoice).toHaveBeenCalledTimes(1);
    expect(mockSubmitInvoice.mock.calls[0][0]).toMatchObject({
      submitter: "user@example.com",
      department: "Engineering",
      vendor: "Quality Supplier Ltd.",
      invoiceNumber: "INV-005",
      currency: "USD",
      total: 1500,
      taxAmount: 255,
      category: "office",
    });

    expect(await screen.findByText(/Submission received/)).toBeInTheDocument();
    expect(await screen.findByText(/cid-007/)).toBeInTheDocument();
  });

  it("shows error message when API fails", async () => {
    const user = userEvent.setup();
    mockSubmitInvoice.mockRejectedValueOnce(new Error("Invalid payload"));

    render(<SubmitForm />);

    await user.type(screen.getByLabelText("Submitter Email*"), "user@example.com");
    await user.type(screen.getByLabelText("Department*"), "Engineering");
    await user.type(screen.getByLabelText("Vendor*"), "Quality Supplier Ltd.");
    await user.type(screen.getByLabelText("Invoice Number*"), "INV-005");
    await user.type(screen.getByLabelText("Amount*"), "1500");
    await user.type(screen.getByLabelText("Tax"), "255");
    await user.type(screen.getByLabelText("Category*"), "office");

    await user.click(screen.getByRole("button", { name: "Submit for Approval" }));

    expect(await screen.findByText("Invalid payload")).toBeInTheDocument();
  });
});
