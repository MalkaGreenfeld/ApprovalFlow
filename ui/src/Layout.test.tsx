import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Layout } from "./Layout";

describe("Layout", () => {
  it("renders the app title", () => {
    render(
      <Layout activeTab="submit" onTabChange={() => {}}>
        <div>content</div>
      </Layout>
    );
    expect(screen.getByText("ApprovalFlow")).toBeInTheDocument();
  });

  it("renders three tab buttons", () => {
    render(
      <Layout activeTab="submit" onTabChange={() => {}}>
        <div>content</div>
      </Layout>
    );
    expect(screen.getByText("New Submission")).toBeInTheDocument();
    expect(screen.getByText("Track Status")).toBeInTheDocument();
    expect(screen.getByText("Approval Queue")).toBeInTheDocument();
  });

  it("renders children content", () => {
    render(
      <Layout activeTab="submit" onTabChange={() => {}}>
        <p>Submission form</p>
      </Layout>
    );
    expect(screen.getByText("Submission form")).toBeInTheDocument();
  });

  it("calls onTabChange when a tab is clicked", async () => {
    const user = userEvent.setup();
    let selected = "submit";
    const handleChange = (tab: string) => {
      selected = tab;
    };

    render(
      <Layout activeTab="submit" onTabChange={handleChange}>
        <div>content</div>
      </Layout>
    );

    await user.click(screen.getByText("Track Status"));
    expect(selected).toBe("status");
  });

  it("highlights the active tab", () => {
    render(
      <Layout activeTab="approvals" onTabChange={() => {}}>
        <div>content</div>
      </Layout>
    );

    const approvalsTab = screen.getByText("Approval Queue");
    expect(approvalsTab.getAttribute("aria-current")).toBe("page");
  });
});
