import { getSupabaseBrowserClient } from "@/lib/supabase/client";

/**
 * Base URL of the StudAI backend (FastAPI on Railway).
 *
 * Set this in:
 *   - Vercel:  Project Settings → Environment Variables → NEXT_PUBLIC_BACKEND_URL
 *   - Local:   .env.local file at the repo root
 *
 * No trailing slash. Example: "https://studai-backend.up.railway.app"
 */
export const BACKEND_URL = (
  process.env.NEXT_PUBLIC_BACKEND_URL ?? ""
).replace(/\/$/, "");

export class ApiError extends Error {
  constructor(
    public status: number,
    public statusText: string,
    public detail: string,
  ) {
    super(`${status} ${statusText}: ${detail}`);
    this.name = "ApiError";
  }
}

async function getAccessToken(): Promise<string> {
  const supabase = getSupabaseBrowserClient();
  const { data, error } = await supabase.auth.getSession();
  if (error || !data.session?.access_token) {
    throw new ApiError(401, "Unauthorized", "No active Supabase session");
  }
  return data.session.access_token;
}

interface ApiFetchOptions extends RequestInit {
  /** Skip auth header. Default: false. */
  unauthenticated?: boolean;
}

/**
 * Centralized fetch wrapper for talking to the FastAPI backend.
 *
 * Adds the bearer token automatically. Throws `ApiError` on non-2xx.
 * Returns the raw `Response` so callers can decide whether to read JSON,
 * stream, etc.
 */
export async function apiFetch(
  path: string,
  options: ApiFetchOptions = {},
): Promise<Response> {
  if (!BACKEND_URL) {
    throw new ApiError(
      0,
      "ConfigError",
      "NEXT_PUBLIC_BACKEND_URL is not set",
    );
  }

  const { unauthenticated, headers, ...rest } = options;
  const finalHeaders = new Headers(headers);

  if (!unauthenticated) {
    const token = await getAccessToken();
    finalHeaders.set("Authorization", `Bearer ${token}`);
  }

  if (!finalHeaders.has("Content-Type") && rest.body) {
    finalHeaders.set("Content-Type", "application/json");
  }

  const response = await fetch(`${BACKEND_URL}${path}`, {
    ...rest,
    headers: finalHeaders,
  });

  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.clone().json();
      detail = body.detail ?? JSON.stringify(body);
    } catch {
      try {
        detail = await response.clone().text();
      } catch {
        // keep statusText
      }
    }
    throw new ApiError(response.status, response.statusText, detail);
  }

  return response;
}

export async function apiFetchJson<T>(
  path: string,
  options: ApiFetchOptions = {},
): Promise<T> {
  const response = await apiFetch(path, options);
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}
