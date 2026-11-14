import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

afterEach(() => {
  cleanup();
});

// jsdom does not implement the object-URL API. The supplement form and the evidence
// previews both depend on it, so a deterministic stub stands in; tests can assert calls.
URL.createObjectURL = vi.fn(
  (obj: Blob) => `blob:mock-${obj.size}-${Math.random().toString(36).slice(2)}`
);
URL.revokeObjectURL = vi.fn();
