import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, fetchEvidenceObjectUrl, supplementIssueMultipart } from "@/lib/api";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

type ProgressEventLike = {
  loaded: number;
  total: number;
  lengthComputable: boolean;
};

class FakeXhr {
  static last: FakeXhr | null = null;
  status = 0;
  response: unknown = null;
  withCredentials = false;
  responseType = "";
  headers = new Map<string, string>();
  upload: { onprogress: ((event: ProgressEventLike) => void) | null } = {
    onprogress: null,
  };
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  method = "";
  url = "";
  sent: FormData | null = null;

  open(method: string, url: string) {
    this.method = method;
    this.url = url;
    FakeXhr.last = this;
  }
  setRequestHeader(name: string, value: string) {
    this.headers.set(name, value);
  }
  send(body: FormData) {
    this.sent = body;
  }
}

function jpegFile(name = "photo.jpg"): File {
  return new File([new Uint8Array(128)], name, { type: "image/jpeg" });
}

beforeEach(() => {
  vi.stubGlobal("XMLHttpRequest", FakeXhr as unknown as typeof XMLHttpRequest);
  FakeXhr.last = null;
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("supplementIssueMultipart", () => {
  it("posts to the supplement endpoint with credentials and the exact multipart field names", async () => {
    const f1 = jpegFile("one.jpg");
    const f2 = jpegFile("two.jpg");
    const pending = supplementIssueMultipart({
      missionId: "mis_1",
      issueId: "iss_1",
      note: "note text",
      description: "description text",
      files: [f1, f2],
    });

    const xhr = FakeXhr.last;
    expect(xhr).not.toBeNull();
    expect(xhr?.method).toBe("POST");
    expect(xhr?.url).toBe(
      `${API_URL}/missions/mis_1/order/issues/iss_1/supplement`
    );
    expect(xhr?.withCredentials).toBe(true);
    expect(xhr?.responseType).toBe("json");
    expect(xhr?.sent).not.toBeNull();
    expect(xhr?.sent?.get("note")).toBe("note text");
    expect(xhr?.sent?.get("description")).toBe("description text");
    expect(xhr?.sent?.getAll("files")).toEqual([f1, f2]);

    xhr!.status = 200;
    xhr!.response = { issue_id: "iss_1" };
    xhr!.onload?.();
    await expect(pending).resolves.toEqual({ issue_id: "iss_1" });
  });

  it("appends each selected file separately under the files field", async () => {
    const files = ["a.jpg", "b.jpg", "c.jpg"].map((n) => jpegFile(n));
    const pending = supplementIssueMultipart({
      missionId: "m",
      issueId: "i",
      note: "",
      description: "",
      files,
    });
    const entries = FakeXhr.last?.sent?.getAll("files");
    expect(entries).toEqual(files);
    FakeXhr.last!.status = 200;
    FakeXhr.last!.response = {};
    FakeXhr.last!.onload?.();
    await pending;
  });

  it("omits empty note and description fields instead of sending blank strings", async () => {
    const pending = supplementIssueMultipart({
      missionId: "m",
      issueId: "i",
      note: "",
      description: "",
      files: [],
    });
    expect(FakeXhr.last?.sent?.has("note")).toBe(false);
    expect(FakeXhr.last?.sent?.has("description")).toBe(false);
    FakeXhr.last!.status = 200;
    FakeXhr.last!.response = {};
    FakeXhr.last!.onload?.();
    await pending;
  });

  it("never sets a Content-Type header — the browser supplies the multipart boundary", async () => {
    const pending = supplementIssueMultipart({
      missionId: "m",
      issueId: "i",
      note: "n",
      description: "d",
      files: [jpegFile()],
    });
    const names = [...FakeXhr.last!.headers.keys()].map((k) => k.toLowerCase());
    expect(names).not.toContain("content-type");
    FakeXhr.last!.status = 200;
    FakeXhr.last!.response = {};
    FakeXhr.last!.onload?.();
    await pending;
  });

  it("reports upload progress as a fraction when the browser reports it", async () => {
    const onProgress = vi.fn();
    const pending = supplementIssueMultipart({
      missionId: "m",
      issueId: "i",
      note: "",
      description: "",
      files: [jpegFile()],
      onProgress,
    });
    const onprogress = FakeXhr.last?.upload.onprogress;
    expect(onprogress).not.toBeNull();
    onprogress?.({ loaded: 40, total: 80, lengthComputable: true });
    expect(onProgress).toHaveBeenCalledWith(0.5);
    onprogress?.({ loaded: 10, total: 80, lengthComputable: false });
    expect(onProgress).toHaveBeenCalledTimes(1);
    FakeXhr.last!.status = 200;
    FakeXhr.last!.response = {};
    FakeXhr.last!.onload?.();
    await pending;
  });

  it("rejects with the server's detail message on a refusal status", async () => {
    const pending = supplementIssueMultipart({
      missionId: "m",
      issueId: "i",
      note: "",
      description: "",
      files: [jpegFile()],
    });
    FakeXhr.last!.status = 413;
    FakeXhr.last!.response = { detail: "Request body too large." };
    FakeXhr.last!.onload?.();
    await expect(pending).rejects.toThrow("Request body too large.");
  });

  it("rejects with the generic refusal text when the detail is not a string", async () => {
    const pending = supplementIssueMultipart({
      missionId: "m",
      issueId: "i",
      note: "",
      description: "",
      files: [],
    });
    FakeXhr.last!.status = 400;
    FakeXhr.last!.response = { detail: { reason: "structured" } };
    FakeXhr.last!.onload?.();
    await expect(pending).rejects.toThrow(
      "The server refused this request (400)."
    );
  });

  it("rejects with a network failure message when the request cannot reach the server", async () => {
    const pending = supplementIssueMultipart({
      missionId: "m",
      issueId: "i",
      note: "",
      description: "",
      files: [],
    });
    FakeXhr.last!.onerror?.();
    await expect(pending).rejects.toThrow(
      "The upload could not reach the server."
    );
  });

  it("carries the refusal status on the ApiError", async () => {
    const pending = supplementIssueMultipart({
      missionId: "m",
      issueId: "i",
      note: "",
      description: "",
      files: [],
    });
    FakeXhr.last!.status = 413;
    FakeXhr.last!.response = { detail: "too large" };
    FakeXhr.last!.onload?.();
    const error = await pending.catch((e) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(413);
    expect((error as ApiError).message).toBe("too large");
  });
});

describe("fetchEvidenceObjectUrl", () => {
  it("fetches through the session and hands back an object URL for the bytes", async () => {
    const blob = new Blob([new Uint8Array(32)], { type: "image/jpeg" });
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      blob: async () => blob,
    }));
    vi.stubGlobal("fetch", fetchMock);

    const url = await fetchEvidenceObjectUrl("mis_1", "iss_1", 2);

    expect(fetchMock).toHaveBeenCalledWith(
      `${API_URL}/missions/mis_1/order/issues/iss_1/evidence/2`,
      { credentials: "include", cache: "no-store" }
    );
    expect(URL.createObjectURL).toHaveBeenCalledWith(blob);
    expect(url).toMatch(/^blob:mock-32-/);
  });

  it("rejects with the server's detail on an authenticated refusal", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: false,
        status: 404,
        json: async () => ({ detail: "No such evidence." }),
      }))
    );
    const error = await fetchEvidenceObjectUrl("m", "i", 0).catch((e) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).message).toBe("No such evidence.");
    expect((error as ApiError).status).toBe(404);
  });

  it("keeps the generic status text when a refusal body is not JSON", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: false,
        status: 403,
        json: async () => {
          throw new SyntaxError("not json");
        },
      }))
    );
    await expect(fetchEvidenceObjectUrl("m", "i", 0)).rejects.toThrow(
      "The server refused this request (403)."
    );
  });
});
