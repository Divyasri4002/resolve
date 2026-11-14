import type {
  AgentConversationEvent,
  AuthorityDelegationResponse,
  IntentReceipt,
  OrderIssue,
  User,
} from "./types";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

/**
 * A refused request, in the shape the caller can act on.
 *
 * The status code is carried because "no order exists yet" (404) and "the order could not be made"
 * (400/502/503) are different facts to show the user, and only the server's status says which.
 */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/**
 * One authenticated call to RESOLVE, with the backend's own refusal reason.
 *
 * Every write in the workspace goes through this so the browser never invents a message for a
 * failure and never reads a body it was refused: the cookie is always sent, a non-2xx always throws,
 * and the thrown text is the server's `detail` when it returned one.
 */
export async function apiRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    credentials: "include",
    cache: "no-store",
    ...init,
  });
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = (data as { detail?: unknown } | null)?.detail;
    throw new ApiError(
      typeof detail === "string" ? detail : `The server refused this request (${res.status}).`,
      res.status
    );
  }
  return data as T;
}

export async function listMissions(userId?: string): Promise<IntentReceipt[]> {
  const url = userId
    ? `${API_URL}/missions?user_id=${encodeURIComponent(userId)}`
    : `${API_URL}/missions`;
  const res = await fetch(url, {
    cache: "no-store",
    credentials: "include",
  });
  if (!res.ok) throw new Error("Failed to load missions");
  return res.json();
}

export async function getMission(missionId: string): Promise<IntentReceipt> {
  return apiRequest<IntentReceipt>(`/missions/${missionId}`);
}

/**
 * The conversation RESOLVE kept for a mission, oldest first (Task CORE-2, Phase 6/16).
 *
 * An empty list means the mission has genuinely never been talked through — the caller then falls
 * back to a state-derived opening line instead of inventing a transcript.
 */
export async function getAgentConversation(
  missionId: string
): Promise<AgentConversationEvent[]> {
  const res = await fetch(
    `${API_URL}/missions/${missionId}/agent/conversation?limit=200`,
    { cache: "no-store", credentials: "include" }
  );
  if (res.status === 404) return [];
  if (!res.ok) throw new Error("Failed to load conversation");
  const data = await res.json();
  return (data.events ?? []) as AgentConversationEvent[];
}

/**
 * Hand the agent a named remedy for this mission (Phase 10). The delegation is the user's, the
 * decision stays the Authority Engine's, and this deployment's switches still bound both.
 */
export async function delegateAuthority(
  missionId: string,
  allowedActions: string[]
): Promise<AuthorityDelegationResponse> {
  const res = await fetch(`${API_URL}/missions/${missionId}/authority/delegate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ allowed_actions: allowedActions }),
  });
  const data = await res.json().catch(() => null);
  if (!res.ok) throw new Error(data?.detail ?? "Could not record that delegation.");
  return data as AuthorityDelegationResponse;
}

export async function revokeAuthority(
  missionId: string
): Promise<AuthorityDelegationResponse> {
  const res = await fetch(`${API_URL}/missions/${missionId}/authority/revoke`, {
    method: "POST",
    credentials: "include",
  });
  const data = await res.json().catch(() => null);
  if (!res.ok) throw new Error(data?.detail ?? "Could not withdraw that delegation.");
  return data as AuthorityDelegationResponse;
}

export function missionSocketUrl(missionId: string): string {
  const wsBase = API_URL.replace(/^http/, "ws");
  return `${wsBase}/ws/missions/${missionId}`;
}

export async function createMission(payload: unknown): Promise<IntentReceipt> {
  const res = await fetch(`${API_URL}/missions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error("Failed to create mission");
  return res.json();
}

export async function createShoppingMission(prompt: string): Promise<IntentReceipt> {
  const res = await fetch(`${API_URL}/missions/shopping`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ prompt }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => null);
    throw new Error(err?.detail ?? "Failed to create shopping mission");
  }
  return res.json();
}

export async function registerUser(name: string, email: string, password: string): Promise<User> {
  const res = await fetch(`${API_URL}/auth/register`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ name, email, password }),
  });
  if (!res.ok) {
    const errorData = await res.json().catch(() => null);
    throw new Error(errorData?.detail ?? "Registration failed");
  }
  return res.json();
}

export async function loginUser(email: string, password: string): Promise<User> {
  const res = await fetch(`${API_URL}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ email, password }),
  });
  if (!res.ok) {
    const errorData = await res.json().catch(() => null);
    throw new Error(errorData?.detail ?? "Invalid email or password");
  }
  return res.json();
}

export async function logoutUser(): Promise<void> {
  const res = await fetch(`${API_URL}/auth/logout`, {
    method: "POST",
    credentials: "include",
  });
  if (!res.ok) throw new Error("Logout failed");
}

export async function getMe(): Promise<User | null> {
  try {
    const res = await fetch(`${API_URL}/auth/me`, {
      cache: "no-store",
      credentials: "include",
    });
    if (!res.ok) return null;
    return res.json();
  } catch {
    return null;
  }
}

/**
 * Send the buyer's supplement as multipart/form-data — `note`, `description` and one
 * `files` part per selected image — to the existing supplement endpoint.
 *
 * XMLHttpRequest is used instead of fetch for the one thing fetch cannot do: real
 * upload progress. The multipart Content-Type header is deliberately NEVER set here —
 * the browser writes it, including the boundary; a hand-set header would break parsing.
 * The response and the refusal shape (`detail`) are handled exactly like apiRequest.
 */
export function supplementIssueMultipart(options: {
  missionId: string;
  issueId: string;
  note: string;
  description: string;
  files: File[];
  onProgress?: (fraction: number) => void;
}): Promise<OrderIssue> {
  const { missionId, issueId, note, description, files, onProgress } = options;
  const form = new FormData();
  if (note) form.append("note", note);
  if (description) form.append("description", description);
  for (const file of files) form.append("files", file);

  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open(
      "POST",
      `${API_URL}/missions/${missionId}/order/issues/${issueId}/supplement`
    );
    xhr.withCredentials = true;
    xhr.responseType = "json";
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && onProgress && event.total > 0) {
        onProgress(event.loaded / event.total);
      }
    };
    xhr.onload = () => {
      const data = xhr.response as { detail?: unknown } | null;
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(data as unknown as OrderIssue);
        return;
      }
      reject(
        new ApiError(
          typeof data?.detail === "string"
            ? data.detail
            : `The server refused this request (${xhr.status}).`,
          xhr.status
        )
      );
    };
    xhr.onerror = () =>
      reject(new ApiError("The upload could not reach the server.", 0));
    xhr.send(form);
  });
}

/**
 * Fetch one stored evidence file through the authenticated retrieval endpoint and
 * hand back a local object URL for an <img>. The bytes pass through the browser's
 * session — no storage key, bucket or credential is ever involved, and the caller
 * revokes the URL when the preview goes away.
 */
export async function fetchEvidenceObjectUrl(
  missionId: string,
  issueId: string,
  index: number
): Promise<string> {
  const res = await fetch(
    `${API_URL}/missions/${missionId}/order/issues/${issueId}/evidence/${index}`,
    { credentials: "include", cache: "no-store" }
  );
  if (!res.ok) {
    let detail = `The server refused this request (${res.status}).`;
    try {
      const data = (await res.json()) as { detail?: unknown } | null;
      if (typeof data?.detail === "string") detail = data.detail;
    } catch {
      // non-JSON refusal body — keep the generic status text
    }
    throw new ApiError(detail, res.status);
  }
  const blob = await res.blob();
  return URL.createObjectURL(blob);
}
