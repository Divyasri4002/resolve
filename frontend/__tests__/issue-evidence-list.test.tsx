import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { IssuePanel } from "@/components/workspace/IssuePanel";
import { fetchEvidenceObjectUrl } from "@/lib/api";
import type { OrderIssue, OrderIssueEvidence } from "@/lib/types";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, fetchEvidenceObjectUrl: vi.fn() };
});

const fetchEvidence = vi.mocked(fetchEvidenceObjectUrl);

function makeIssue(overrides: Partial<OrderIssue> = {}): OrderIssue {
  return {
    issue_id: "iss_1",
    order_id: "order_1",
    mission_id: "mis_1",
    issue_type: "damaged",
    description: "The item arrived damaged.",
    requested_resolution: "refund",
    status: "reported",
    ...overrides,
  };
}

function makeProps(issue: OrderIssue) {
  return {
    issues: [issue],
    onReport: vi.fn(async () => true),
    onSubmitAnswers: vi.fn(async () => true),
    onAssess: vi.fn(async () => true),
    onRefund: vi.fn(async () => true),
    onReplace: vi.fn(async () => true),
    onReturn: vi.fn(async () => true),
    onResolve: vi.fn(async () => true),
    onManualReview: vi.fn(async () => true),
    onSupplement: vi.fn(async () => true),
  };
}

function uploadedFile(
  overrides: Partial<OrderIssueEvidence> = {}
): OrderIssueEvidence {
  return {
    kind: "uploaded_file",
    display_name: "crushed-box.jpg",
    mime: "image/jpeg",
    size_bytes: 2048,
    ...overrides,
  };
}

beforeEach(() => {
  fetchEvidence.mockReset();
});

describe("panel-level error banner", () => {
  it("shows a backend supplement error even when the report form is closed", () => {
    render(
      <IssuePanel
        {...makeProps(makeIssue())}
        error="Request body too large — the supplement was refused (413)."
      />
    );
    expect(
      screen.getByText("Request body too large — the supplement was refused (413).")
    ).toBeInTheDocument();
  });

  it("hides the banner while the report form is open — the form shows the error itself", async () => {
    const user = userEvent.setup();
    const message = "Request body too large — the supplement was refused (413).";
    render(<IssuePanel {...makeProps(makeIssue())} error={message} />);
    expect(screen.getAllByText(message)).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: /Report an issue/ }));
    // The panel banner is gone; the report form renders the same error — exactly once.
    expect(screen.getAllByText(message)).toHaveLength(1);
  });
});

describe("IssueEvidenceList", () => {
  it("renders the real number of attached evidence items", async () => {
    fetchEvidence.mockResolvedValue("blob:mock-2048-a");
    const issue = makeIssue({
      evidence: [uploadedFile(), { kind: "photo_url", url: "https://example.com/p.jpg" }],
    });
    render(<IssuePanel {...makeProps(issue)} />);
    expect(screen.getByText("Attached evidence (2)")).toBeInTheDocument();
    await screen.findByAltText("crushed-box.jpg");
  });

  it("renders nothing when the issue carries no evidence", () => {
    render(<IssuePanel {...makeProps(makeIssue())} />);
    expect(screen.queryByText(/Attached evidence/)).not.toBeInTheDocument();
  });

  it("previews an uploaded file through the authenticated retrieval endpoint", async () => {
    fetchEvidence.mockResolvedValue("blob:mock-32-abc");
    const issue = makeIssue({ evidence: [uploadedFile()] });
    render(<IssuePanel {...makeProps(issue)} />);
    await waitFor(() =>
      expect(fetchEvidence).toHaveBeenCalledWith("mis_1", "iss_1", 0)
    );
    const img = await screen.findByAltText("crushed-box.jpg");
    expect(img).toHaveAttribute("src", "blob:mock-32-abc");
    expect(screen.getByText(/2 KB · image\/jpeg/)).toBeInTheDocument();
  });

  it("shows an external photo link without fetching evidence bytes", () => {
    const issue = makeIssue({
      evidence: [{ kind: "photo_url", url: "https://example.com/photo.jpg" }],
    });
    render(<IssuePanel {...makeProps(issue)} />);
    const link = screen.getByRole("link", { name: "https://example.com/photo.jpg" });
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(fetchEvidence).not.toHaveBeenCalled();
  });

  it("degrades to 'preview unavailable' when the retrieval endpoint fails", async () => {
    fetchEvidence.mockRejectedValue(new Error("refused"));
    const issue = makeIssue({ evidence: [uploadedFile({ display_name: null })] });
    render(<IssuePanel {...makeProps(issue)} />);
    expect(await screen.findByText(/preview unavailable/)).toBeInTheDocument();
    expect(screen.getByText("Attached file")).toBeInTheDocument();
  });

  it("updates the count and previews after the issue is refreshed from the backend", async () => {
    fetchEvidence.mockResolvedValue("blob:mock-8-x");
    const initial = makeIssue({ evidence: [uploadedFile()] });
    const refreshed = makeIssue({
      evidence: [uploadedFile(), uploadedFile({ display_name: "second.jpg" })],
    });
    const { rerender } = render(<IssuePanel {...makeProps(initial)} />);
    expect(screen.getByText("Attached evidence (1)")).toBeInTheDocument();
    await screen.findByAltText("crushed-box.jpg");
    rerender(<IssuePanel {...makeProps(refreshed)} />);
    expect(screen.getByText("Attached evidence (2)")).toBeInTheDocument();
    await waitFor(() =>
      expect(fetchEvidence).toHaveBeenNthCalledWith(2, "mis_1", "iss_1", 1)
    );
    await screen.findByAltText("second.jpg");
  });
});
