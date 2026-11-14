import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { IssuePanel } from "@/components/workspace/IssuePanel";
import type { OrderIssue, ResolutionAssessment } from "@/lib/types";

function makeAssessment(
  overrides: Partial<ResolutionAssessment> = {}
): ResolutionAssessment {
  return {
    assessment_id: "as_1",
    issue_id: "iss_1",
    mission_id: "mis_1",
    order_id: "order_1",
    proposed_action: "request_more_information",
    issue_assessment: "The report needs more evidence.",
    reasoning_summary: "Photos would clarify the damage.",
    needs_escalation: false,
    proposal_source: "reasoning_model",
    authority_decision: "escalate",
    decision_reason: "More information required before any remedy.",
    ...overrides,
  };
}

function makeIssue(overrides: Partial<OrderIssue> = {}): OrderIssue {
  return {
    issue_id: "iss_1",
    order_id: "order_1",
    mission_id: "mis_1",
    issue_type: "damaged",
    description: "The item arrived damaged.",
    requested_resolution: "refund",
    status: "reported",
    manual_review: { requested: false },
    assessment: makeAssessment(),
    ...overrides,
  };
}

type IssuePanelProps = Parameters<typeof IssuePanel>[0];
type IssuePanelOnSupplement = NonNullable<IssuePanelProps["onSupplement"]>;

function makeProps(issue: OrderIssue, onSupplement: IssuePanelOnSupplement) {
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
    onSupplement,
  };
}

function jpegFile(name = "photo.jpg", bytes = 2048): File {
  return new File([new Uint8Array(bytes)], name, { type: "image/jpeg" });
}

function attachFiles(files: File[]) {
  fireEvent.change(screen.getByLabelText("Attach photos"), {
    target: { files },
  });
}

async function renderPanelWithSelection(
  onSupplement: IssuePanelOnSupplement,
  files: File[]
) {
  const user = userEvent.setup();
  const utils = render(<IssuePanel {...makeProps(makeIssue(), onSupplement)} />);
  if (files.length > 0) attachFiles(files);
  return { user, ...utils };
}

describe("SupplementEvidenceForm (via IssuePanel)", () => {
  it("renders the supplement form when a reasoning-model assessment asked for more information", () => {
    render(<IssuePanel {...makeProps(makeIssue(), vi.fn())} />);
    expect(screen.getByText("More evidence requested")).toBeInTheDocument();
    expect(
      screen.getByText(/JPEG, PNG or WEBP · up to 5 MB each · 5 files \/ 25 MB/)
    ).toBeInTheDocument();
  });

  it("does not render the form when the assessment did not ask for more information", () => {
    const issue = makeIssue({
      assessment: makeAssessment({ proposed_action: "request_refund" }),
    });
    render(<IssuePanel {...makeProps(issue, vi.fn())} />);
    expect(screen.queryByText("More evidence requested")).not.toBeInTheDocument();
  });

  it("accepts a JPEG selection and shows filename and size", async () => {
    await renderPanelWithSelection(vi.fn(), [jpegFile()]);
    expect(screen.getByText("photo.jpg")).toBeInTheDocument();
    expect(screen.getByText("2 KB")).toBeInTheDocument();
    expect(screen.getByText("Attach photos (1/5)")).toBeInTheDocument();
    expect(URL.createObjectURL).toHaveBeenCalledWith(expect.any(File));
  });

  it("accepts PNG and WEBP selections", async () => {
    const png = new File([new Uint8Array(64)], "shot.png", { type: "image/png" });
    const webp = new File([new Uint8Array(64)], "shot.webp", { type: "image/webp" });
    await renderPanelWithSelection(vi.fn(), [png, webp]);
    expect(screen.getByText("shot.png")).toBeInTheDocument();
    expect(screen.getByText("shot.webp")).toBeInTheDocument();
    expect(screen.getByText("Attach photos (2/5)")).toBeInTheDocument();
  });

  it("refuses a non-image selection with a validation error and no preview", async () => {
    const txt = new File(["notes"], "notes.txt", { type: "text/plain" });
    await renderPanelWithSelection(vi.fn(), [txt]);
    expect(
      screen.getByText(/"notes.txt" is not a JPEG, PNG or WEBP image\./)
    ).toBeInTheDocument();
    expect(screen.queryByText("notes.txt")).not.toBeInTheDocument();
    expect(screen.getByText("Attach photos (0/5)")).toBeInTheDocument();
  });

  it("refuses a file over 5 MB with a validation error", async () => {
    const big = jpegFile("huge.jpg");
    Object.defineProperty(big, "size", { value: 6 * 1024 * 1024 });
    await renderPanelWithSelection(vi.fn(), [big]);
    expect(
      screen.getByText(/"huge\.jpg" is 6 MB — the per-file limit is 5 MB\./)
    ).toBeInTheDocument();
    expect(screen.queryByText("huge.jpg")).not.toBeInTheDocument();
  });

  it("keeps only the first five files when too many are selected at once", async () => {
    const files = [1, 2, 3, 4, 5, 6].map((n) => jpegFile(`photo${n}.jpg`));
    await renderPanelWithSelection(vi.fn(), files);
    expect(screen.getByText("photo1.jpg")).toBeInTheDocument();
    expect(screen.getByText("photo5.jpg")).toBeInTheDocument();
    expect(screen.queryByText("photo6.jpg")).not.toBeInTheDocument();
    expect(
      screen.getByText(/only the first 5 of these were added\./)
    ).toBeInTheDocument();
    expect(screen.getByText("File limit reached (5)")).toBeInTheDocument();
  });

  it("caps a second batch at the remaining room", async () => {
    const user = userEvent.setup();
    render(<IssuePanel {...makeProps(makeIssue(), vi.fn())} />);
    attachFiles([jpegFile("a.jpg"), jpegFile("b.jpg")]);
    attachFiles([jpegFile("c.jpg"), jpegFile("d.jpg"), jpegFile("e.jpg")]);
    expect(screen.getByText("File limit reached (5)")).toBeInTheDocument();
    expect(screen.getByText("a.jpg")).toBeInTheDocument();
    expect(screen.getByText("c.jpg")).toBeInTheDocument();
    expect(screen.getByText("e.jpg")).toBeInTheDocument();
    expect(screen.queryByText(/only the first/)).not.toBeInTheDocument();
  });

  it("removes an individual selected file and revokes its preview URL", async () => {
    const { user } = await renderPanelWithSelection(vi.fn(), [
      jpegFile("keep.jpg"),
      jpegFile("drop.jpg"),
    ]);
    await user.click(screen.getByRole("button", { name: "Remove drop.jpg" }));
    expect(screen.queryByText("drop.jpg")).not.toBeInTheDocument();
    expect(screen.getByText("keep.jpg")).toBeInTheDocument();
    expect(screen.getByText("Attach photos (1/5)")).toBeInTheDocument();
    expect(URL.revokeObjectURL).toHaveBeenCalled();
  });

  it("submits the note, description, selected files and a progress callback", async () => {
    const onSupplement = vi.fn(
      async (
        _issueId: string,
        _note: string,
        _description: string,
        _files: File[],
        _onUploadProgress?: (fraction: number) => void
      ) => true
    );
    const { user } = await renderPanelWithSelection(onSupplement, [jpegFile()]);
    await user.type(
      screen.getByPlaceholderText("What else can you tell us about this issue?"),
      "Box was crushed"
    );
    await user.type(
      screen.getByLabelText("Evidence description (optional)"),
      "Corner dented"
    );
    await user.click(
      screen.getByRole("button", { name: /Submit evidence & re-assess/ })
    );
    expect(onSupplement).toHaveBeenCalledTimes(1);
    const [issueId, note, description, files, onProgress] =
      onSupplement.mock.calls[0];
    expect(issueId).toBe("iss_1");
    expect(note).toBe("Box was crushed");
    expect(description).toBe("Corner dented");
    expect(files).toHaveLength(1);
    expect(files[0].name).toBe("photo.jpg");
    expect(typeof onProgress).toBe("function");
  });

  it("submits a note-only supplement with no files and no progress callback", async () => {
    const onSupplement = vi.fn(
      async (
        _issueId: string,
        _note: string,
        _description: string,
        _files: File[],
        _onUploadProgress?: (fraction: number) => void
      ) => true
    );
    const { user } = await renderPanelWithSelection(onSupplement, []);
    await user.type(
      screen.getByPlaceholderText("What else can you tell us about this issue?"),
      "It never arrived"
    );
    await user.click(
      screen.getByRole("button", { name: /Submit evidence & re-assess/ })
    );
    expect(onSupplement).toHaveBeenCalledTimes(1);
    const [issueId, note, description, files, onProgress] =
      onSupplement.mock.calls[0];
    expect(issueId).toBe("iss_1");
    expect(note).toBe("It never arrived");
    expect(description).toBe("");
    expect(files).toHaveLength(0);
    expect(onProgress).toBeUndefined();
  });

  it("disables submit until there is content", () => {
    render(<IssuePanel {...makeProps(makeIssue(), vi.fn())} />);
    expect(
      screen.getByRole("button", { name: /Submit evidence & re-assess/ })
    ).toBeDisabled();
  });

  it("clears the form only after the backend confirms success", async () => {
    const onSupplement = vi.fn(async () => true);
    const { user } = await renderPanelWithSelection(onSupplement, [
      jpegFile("done.jpg"),
    ]);
    await user.type(
      screen.getByPlaceholderText("What else can you tell us about this issue?"),
      "Note text"
    );
    await user.click(
      screen.getByRole("button", { name: /Submit evidence & re-assess/ })
    );
    await waitFor(() =>
      expect(
        screen.getByPlaceholderText("What else can you tell us about this issue?")
      ).toHaveValue("")
    );
    expect(screen.queryByText("done.jpg")).not.toBeInTheDocument();
    expect(screen.getByText("Attach photos (0/5)")).toBeInTheDocument();
    expect(URL.revokeObjectURL).toHaveBeenCalled();
  });

  it("keeps the form content when the supplement is refused", async () => {
    const onSupplement = vi.fn(async () => false);
    const { user } = await renderPanelWithSelection(onSupplement, []);
    await user.type(
      screen.getByPlaceholderText("What else can you tell us about this issue?"),
      "Note text"
    );
    await user.click(
      screen.getByRole("button", { name: /Submit evidence & re-assess/ })
    );
    await waitFor(() => expect(onSupplement).toHaveBeenCalled());
    expect(
      screen.getByPlaceholderText("What else can you tell us about this issue?")
    ).toHaveValue("Note text");
  });

  it("shows the staged upload labels: nothing is claimed uploaded until confirmed", async () => {
    let resolveFn: ((ok: boolean) => void) | undefined;
    let progressCb: ((fraction: number) => void) | undefined;
    const onSupplement = vi.fn(
      (
        _issueId: string,
        _note: string,
        _description: string,
        _files: File[],
        onUploadProgress?: (fraction: number) => void
      ) => {
        progressCb = onUploadProgress;
        return new Promise<boolean>((resolve) => {
          resolveFn = resolve;
        });
      }
    );
    const { user, rerender } = await renderPanelWithSelection(onSupplement, [
      jpegFile("staged.jpg"),
    ]);
    await user.click(
      screen.getByRole("button", { name: /Submit evidence & re-assess/ })
    );
    rerender(
      <IssuePanel
        {...makeProps(makeIssue(), onSupplement)}
        isProcessing={true}
      />
    );
    expect(
      screen.getByText("Submitting & re-assessing…")
    ).toBeInTheDocument();
    expect(progressCb).toBeDefined();
    progressCb?.(0.5);
    await waitFor(() =>
      expect(document.querySelector("div[style*='width: 50%']")).not.toBeNull()
    );
    progressCb?.(1);
    await waitFor(() =>
      expect(screen.getByText("Uploaded — re-assessing…")).toBeInTheDocument()
    );
    resolveFn?.(true);
    await waitFor(() =>
      expect(screen.queryByText("staged.jpg")).not.toBeInTheDocument()
    );
  });
});
