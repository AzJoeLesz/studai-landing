/**
 * Phase 10C — typed client for the /admin/paths verification queue.
 *
 * The backend gates every endpoint here on `profiles.role == 'admin'`.
 * The frontend layout (`app/[locale]/admin/layout.tsx`) also redirects
 * non-admins client-side for UX, but the backend gate is authoritative.
 */

import { apiFetchJson } from "./config";

export type AdminPathStatusFilter = "unverified" | "verified" | "all";

/** Compact list-row shape; matches `AdminPathListItem` on the backend. */
export interface AdminPathListItem {
  id: string;
  problem_id: string;
  name: string;
  rationale: string | null;
  preferred: boolean;
  language: string;
  verified: boolean;
  critic_score: number | null;
  source: string | null;
  model: string | null;
  problem_type: string;
  problem_difficulty: string | null;
  problem_preview: string;
}

export interface AdminPathListResponse {
  items: AdminPathListItem[];
  next_offset: number | null;
}

/** Full detail-view payload; matches `AdminPathDetail` on the backend. */
export interface AdminSolutionPath {
  id: string;
  problem_id: string;
  name: string;
  rationale: string | null;
  preferred: boolean;
  language: string;
  verified: boolean;
  verified_by: string | null;
  verified_at: string | null;
  model: string | null;
  critic_score: number | null;
  source: string | null;
  created_at: string | null;
}

export interface AdminProblem {
  id: string;
  source: string;
  type: string;
  difficulty: string | null;
  problem_en: string;
  solution_en: string;
  answer: string | null;
  source_id: string | null;
  created_at: string;
}

export interface AdminSolutionStep {
  id: string;
  path_id: string;
  step_index: number;
  goal: string;
  expected_action: string | null;
  expected_state: string | null;
  is_terminal: boolean;
  created_at: string | null;
}

export interface AdminStepHint {
  id: string;
  step_id: string;
  hint_index: number;
  body: string;
  created_at: string | null;
}

export interface AdminCommonMistake {
  id: string;
  problem_id: string | null;
  step_id: string | null;
  pattern: string;
  detection_hint: string | null;
  pedagogical_hint: string;
  remediation_topic: string | null;
  created_at: string | null;
}

export interface AdminPathDetail {
  path: AdminSolutionPath;
  problem: AdminProblem;
  steps: AdminSolutionStep[];
  hints_by_step: Record<string, AdminStepHint[]>;
  mistakes_by_step: Record<string, AdminCommonMistake[]>;
  problem_scoped_mistakes: AdminCommonMistake[];
}

export interface AdminPathVerifyResponse {
  id: string;
  verified: boolean;
}

const ADMIN_LIST_LIMIT = 25;

export async function listAdminPaths(opts?: {
  status_filter?: AdminPathStatusFilter;
  limit?: number;
  offset?: number;
}): Promise<AdminPathListResponse> {
  const status_filter = opts?.status_filter ?? "unverified";
  const limit = opts?.limit ?? ADMIN_LIST_LIMIT;
  const offset = opts?.offset ?? 0;
  const qs = new URLSearchParams({
    status_filter,
    limit: String(limit),
    offset: String(offset),
  });
  return apiFetchJson<AdminPathListResponse>(`/admin/paths?${qs.toString()}`);
}

export async function getAdminPath(id: string): Promise<AdminPathDetail> {
  return apiFetchJson<AdminPathDetail>(`/admin/paths/${id}`);
}

export async function verifyAdminPath(
  id: string,
): Promise<AdminPathVerifyResponse> {
  return apiFetchJson<AdminPathVerifyResponse>(`/admin/paths/${id}/verify`, {
    method: "POST",
  });
}

export async function rejectAdminPath(
  id: string,
): Promise<AdminPathVerifyResponse> {
  return apiFetchJson<AdminPathVerifyResponse>(`/admin/paths/${id}/reject`, {
    method: "POST",
  });
}
