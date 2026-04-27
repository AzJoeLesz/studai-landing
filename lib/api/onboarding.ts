import { apiFetchJson } from "./config";
import type { StudentProgress } from "./types";

/**
 * Onboarding API client. Mirrors `backend/app/api/onboarding.py`.
 *
 * Three endpoints:
 *   - POST /onboarding/seed-priors             — idempotent grade-derived seed
 *   - POST /onboarding/placement/start         — first quiz problem
 *   - POST /onboarding/placement/answer        — record + next
 *   - GET  /onboarding/placement/status        — has the user already completed?
 */

export interface SeedPriorsResponse {
  seeded: number;
  curriculum: string | null;
  band: string | null;
  skipped_existing: boolean;
}

export interface PlacementProblem {
  problem_id: string;
  problem_text: string;
  answer: string | null;
  difficulty: string;
  topic: string;
  question_index: number;
}

export interface PlacementStartResponse {
  next: PlacementProblem | null;
  completed: boolean;
  questions_total: number;
}

export interface PlacementAnswerRequest {
  problem_id: string;
  topic: string;
  difficulty: string;
  /** Free-text answer the student typed. Empty string = "I don't know". */
  student_answer: string;
  /**
   * The exact problem text that was shown. The backend uses this to
   * judge correctness without a second DB lookup.
   */
  problem_text: string;
  /** Canonical answer from the corpus row, if any. */
  canonical_answer: string | null;
}

export interface PlacementAnswerResponse {
  next: PlacementProblem | null;
  completed: boolean;
  /** Backend's verdict on the answer just submitted. */
  was_correct: boolean;
  /** Echoed canonical answer so the UI can reveal it after submission. */
  canonical_answer: string | null;
  summary: StudentProgress[] | null;
}

export interface PlacementStatusResponse {
  completed: boolean;
  attempts_so_far: number;
  questions_total: number;
}

export async function seedGradePriors(): Promise<SeedPriorsResponse> {
  return apiFetchJson<SeedPriorsResponse>("/onboarding/seed-priors", {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export async function startPlacement(): Promise<PlacementStartResponse> {
  return apiFetchJson<PlacementStartResponse>("/onboarding/placement/start", {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export async function submitPlacementAnswer(
  payload: PlacementAnswerRequest,
): Promise<PlacementAnswerResponse> {
  return apiFetchJson<PlacementAnswerResponse>(
    "/onboarding/placement/answer",
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

export async function getPlacementStatus(): Promise<PlacementStatusResponse> {
  return apiFetchJson<PlacementStatusResponse>(
    "/onboarding/placement/status",
  );
}
