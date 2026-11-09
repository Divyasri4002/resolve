import type {
  AgentConversationEvent,
  AuthorityDelegationResponse,
  IntentReceipt,
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
