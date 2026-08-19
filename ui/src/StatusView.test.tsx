import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StatusView } from "./StatusView";

vi.mock("./api", () => ({
  getSubmissionStatus: vi.fn(),
}));

import { getSubmissionStatus } from "./api";

const mockGetStatus = getSubmissionStatus as ReturnType<typeof vi.fn>;

beforeEach(() => {
  mockGetStatus.mockReset();
});

describe("StatusView", () => {
  it("renders an input field and search button", () => {
    render(<StatusView />);

    expect(screen.getByPlaceholderText("Enter Correlation ID")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Check Status" })).toBeInTheDocument();
  });

  it("shows validation when searching with empty ID", async () => {
    const user = userEvent.setup();
    render(<StatusView />);

    await user.click(screen.getByRole("button", { name: "Check Status" }));

    expect(screen.getByText("Please enter a Correlation ID")).toBeInTheDocument();
  });

  it("displays submission status on successful lookup", async () => {
    const user = userEvent.setup();
    mockGetStatus.mockResolvedValue({
      correlation_id: "abc-123",
      status: "auto_approved",
      vendor: "Quality Supplier Ltd.",
      amount_usd: "250.00",
      category: "office",
    });

    render(<StatusView />);

    await user.type(screen.getByPlaceholderText("Enter Correlation ID"), "abc-123");
    await user.click(screen.getByRole("button", { name: "Check Status" }));

    expect(await screen.findByText("auto_approved")).toBeInTheDocument();
  });

  it("calls getSubmissionStatus twice via polling when status is non-terminal", async () => {
    const user = userEvent.setup();
    // Spy on setInterval + clearInterval to control polling
    const setIntervalSpy = vi.spyOn(globalThis, "setInterval");
    const clearIntervalSpy = vi.spyOn(globalThis, "clearInterval");

    mockGetStatus.mockResolvedValue({
      correlation_id: "abc-123",
      status: "human_review",
      vendor: "Vendor",
      amount_usd: "100.00",
      category: "office",
    });

    render(<StatusView />);

    await user.type(screen.getByPlaceholderText("Enter Correlation ID"), "abc-123");
    await user.click(screen.getByRole("button", { name: "Check Status" }));

    expect(await screen.findByText("human_review")).toBeInTheDocument();

    // First call happened immediately (before setInterval)
    // Then setInterval was registered for polling
    expect(setIntervalSpy).toHaveBeenCalledWith(expect.any(Function), 5000);

    setIntervalSpy.mockRestore();
    clearIntervalSpy.mockRestore();
  });

  it("displays error when lookup fails", async () => {
    const user = userEvent.setup();
    mockGetStatus.mockRejectedValueOnce(new Error("Submission not found"));

    render(<StatusView />);

    await user.type(screen.getByPlaceholderText("Enter Correlation ID"), "does-not-exist");
    await user.click(screen.getByRole("button", { name: "Check Status" }));

    expect(await screen.findByText("Submission not found")).toBeInTheDocument();
  });
});
