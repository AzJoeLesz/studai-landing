import { ApiError, BACKEND_URL } from "./config";
import { getSupabaseBrowserClient } from "@/lib/supabase/client";

/**
 * Thin streaming-chat client.
 *
 * The backend sends Server-Sent Events with these event names:
 *   - "guided_active" — Phase 10B: emitted at the START of a turn when
 *                       guided mode is active. JSON payload (see
 *                       GuidedActivePayload). The frontend renders a
 *                       small badge on the next assistant bubble.
 *   - "token"  — a chunk of the assistant reply (concat them in order)
 *   - "title"  — auto-generated session title (only on the first turn)
 *   - "done"   — clean end of stream
 *   - "error"  — terminal error; data is the message
 *
 * We can't use the browser's native EventSource because EventSource is
 * GET-only — we need POST to send the user's message, so we read the
 * response body manually with a stream reader and parse SSE frames.
 */

/**
 * Phase 10B `guided_active` SSE payload, mirrored from
 * `agents/tutor.py::run_tutor_turn`. Keep this shape in sync with the
 * backend dict serialization there.
 */
export interface GuidedActivePayload {
  active: boolean;
  path_name: string;
  current_step: number;
  total_steps: number;
  is_activation_turn: boolean;
}

export interface StreamChatHandlers {
  onToken: (token: string) => void;
  onTitle?: (title: string) => void;
  onDone?: () => void;
  onError?: (message: string) => void;
  /**
   * Called once per turn when guided mode applies, BEFORE the first
   * token streams. Use to render the small "guided mode" badge on
   * the assistant bubble. When undefined, the event is silently
   * ignored — the chat works fine either way.
   */
  onGuidedActive?: (payload: GuidedActivePayload) => void;
}

interface StreamChatOptions extends StreamChatHandlers {
  sessionId: string;
  message: string;
  signal?: AbortSignal;
}

export async function streamChat({
  sessionId,
  message,
  signal,
  onToken,
  onTitle,
  onDone,
  onError,
  onGuidedActive,
}: StreamChatOptions): Promise<void> {
  if (!BACKEND_URL) {
    throw new ApiError(0, "ConfigError", "NEXT_PUBLIC_BACKEND_URL is not set");
  }

  const supabase = getSupabaseBrowserClient();
  const { data, error } = await supabase.auth.getSession();
  if (error || !data.session?.access_token) {
    throw new ApiError(401, "Unauthorized", "No active Supabase session");
  }

  const response = await fetch(`${BACKEND_URL}/chat`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${data.session.access_token}`,
      Accept: "text/event-stream",
    },
    body: JSON.stringify({ session_id: sessionId, message }),
    signal,
  });

  if (!response.ok || !response.body) {
    let detail = response.statusText;
    try {
      const body = await response.clone().json();
      detail = body.detail ?? JSON.stringify(body);
    } catch {
      // ignore
    }
    throw new ApiError(response.status, response.statusText, detail);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      // SSE frames are separated by a blank line ("\n\n").
      let separatorIndex: number;
      while ((separatorIndex = buffer.indexOf("\n\n")) !== -1) {
        const rawFrame = buffer.slice(0, separatorIndex);
        buffer = buffer.slice(separatorIndex + 2);
        dispatchFrame(rawFrame, {
          onToken,
          onTitle,
          onDone,
          onError,
          onGuidedActive,
        });
      }
    }
  } finally {
    reader.releaseLock();
  }
}

function dispatchFrame(
  frame: string,
  handlers: StreamChatHandlers,
): void {
  let event = "message";
  let data = "";

  for (const line of frame.split("\n")) {
    if (line.startsWith("event: ")) {
      event = line.slice(7).trim();
    } else if (line.startsWith("data: ")) {
      // The backend escapes literal newlines as "\n" before sending.
      data = line.slice(6).replace(/\\n/g, "\n");
    }
  }

  switch (event) {
    case "token":
      handlers.onToken(data);
      break;
    case "title":
      handlers.onTitle?.(data);
      break;
    case "done":
      handlers.onDone?.();
      break;
    case "error":
      handlers.onError?.(data);
      break;
    case "guided_active":
      if (handlers.onGuidedActive) {
        try {
          const parsed = JSON.parse(data) as GuidedActivePayload;
          handlers.onGuidedActive(parsed);
        } catch {
          // Malformed payload — ignore; guided badge just won't show.
        }
      }
      break;
    default:
      // Unknown event — ignore for forward compatibility.
      break;
  }
}
