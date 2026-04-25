import { apiFetch, apiFetchJson } from "./config";
import type { SessionWithMessages, TutorSession } from "./types";

export async function listSessions(): Promise<TutorSession[]> {
  return apiFetchJson<TutorSession[]>("/sessions");
}

export async function createSession(title?: string): Promise<TutorSession> {
  return apiFetchJson<TutorSession>("/sessions", {
    method: "POST",
    body: JSON.stringify({ title: title ?? null }),
  });
}

export async function getSession(id: string): Promise<SessionWithMessages> {
  return apiFetchJson<SessionWithMessages>(`/sessions/${id}`);
}

export async function renameSession(
  id: string,
  title: string,
): Promise<TutorSession> {
  return apiFetchJson<TutorSession>(`/sessions/${id}`, {
    method: "PATCH",
    body: JSON.stringify({ title }),
  });
}

export async function deleteSession(id: string): Promise<void> {
  await apiFetch(`/sessions/${id}`, { method: "DELETE" });
}
