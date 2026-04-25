/**
 * Mirrors the Pydantic models in backend/app/db/schemas.py.
 *
 * If the backend shape changes, update these by hand. Down the line we
 * could codegen these from the FastAPI OpenAPI schema, but it's not
 * worth the complexity yet.
 */

export type MessageRole = "user" | "assistant" | "system" | "tool";

export interface TutorSession {
  id: string;
  user_id: string;
  title: string | null;
  created_at: string;
  updated_at: string;
}

export interface Message {
  id: string;
  session_id: string;
  role: MessageRole;
  content: string;
  created_at: string;
}

export interface SessionWithMessages {
  session: TutorSession;
  messages: Message[];
}
