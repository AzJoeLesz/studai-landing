"use client";

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import {
  Check,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  RefreshCw,
  X,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { StatusMessage } from "@/components/ui/status-message";
import { MarkdownContent } from "@/components/chat/markdown-content";
import { ApiError } from "@/lib/api/config";
import {
  getAdminPath,
  listAdminPaths,
  rejectAdminPath,
  verifyAdminPath,
  type AdminCommonMistake,
  type AdminPathDetail,
  type AdminPathListItem,
  type AdminPathStatusFilter,
  type AdminSolutionStep,
  type AdminStepHint,
} from "@/lib/api/admin";
import { cn } from "@/lib/utils";

const PAGE_SIZE = 25;

/**
 * Phase 10C — solution-path verification queue.
 *
 * Layout: list of unverified paths on the left (~1/3), full detail of
 * the currently-selected path on the right (~2/3). Approve/Reject
 * buttons sit at the bottom of the detail pane and immediately advance
 * to the next item in the list, so a focused review session is
 * keystroke-light:
 *   read -> Approve -> read -> Approve -> Reject -> ...
 *
 * Decisions surfaced in this UI (see docs/phase10_solution_graphs.md):
 *  * Decision G: in-app /admin/paths route, not a CLI script.
 *    Pays back ~20s/path vs ~60s in CLI; reuses chrome for Phase 12 + 13.
 *  * Decision N: paths are sorted by `critic_score desc nulls last`
 *    so high-confidence LLM-judge picks bubble to the top of the queue.
 *  * No edit-in-place yet (open question deferred until first ~100
 *    verifications surface friction). Reject is a soft-reject:
 *    `verified=false` keeps the row for audit.
 */
export default function AdminPathsPage() {
  const t = useTranslations("admin.paths");

  const [statusFilter, setStatusFilter] =
    useState<AdminPathStatusFilter>("unverified");
  const [items, setItems] = useState<AdminPathListItem[] | null>(null);
  const [offset, setOffset] = useState(0);
  const [nextOffset, setNextOffset] = useState<number | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [listLoading, setListLoading] = useState(false);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<AdminPathDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [actionPending, setActionPending] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const fetchList = useCallback(
    async (
      nextStatusFilter: AdminPathStatusFilter,
      nextOffsetArg: number,
    ): Promise<AdminPathListItem[] | null> => {
      setListLoading(true);
      setListError(null);
      try {
        const res = await listAdminPaths({
          status_filter: nextStatusFilter,
          limit: PAGE_SIZE,
          offset: nextOffsetArg,
        });
        setItems(res.items);
        setOffset(nextOffsetArg);
        setNextOffset(res.next_offset);
        return res.items;
      } catch (e) {
        setItems([]);
        setListError(e instanceof ApiError ? e.detail : t("listError"));
        return null;
      } finally {
        setListLoading(false);
      }
    },
    [t],
  );

  const fetchDetail = useCallback(
    async (id: string) => {
      setDetail(null);
      setDetailError(null);
      setDetailLoading(true);
      try {
        const res = await getAdminPath(id);
        setDetail(res);
      } catch (e) {
        setDetailError(e instanceof ApiError ? e.detail : t("detailError"));
      } finally {
        setDetailLoading(false);
      }
    },
    [t],
  );

  // Initial fetch + refetch on filter change.
  useEffect(() => {
    void fetchList(statusFilter, 0);
  }, [fetchList, statusFilter]);

  // Auto-select the first item once a list lands -- saves a click and
  // lets the keyboard-driven flow start immediately.
  useEffect(() => {
    if (
      items &&
      items.length > 0 &&
      (!selectedId || !items.find((it) => it.id === selectedId))
    ) {
      setSelectedId(items[0].id);
    }
    if (items && items.length === 0) {
      setSelectedId(null);
      setDetail(null);
    }
  }, [items, selectedId]);

  // Load detail whenever the selection changes.
  useEffect(() => {
    if (!selectedId) return;
    void fetchDetail(selectedId);
  }, [fetchDetail, selectedId]);

  function nextSelection(currentId: string): string | null {
    if (!items) return null;
    const idx = items.findIndex((it) => it.id === currentId);
    if (idx < 0) return items[0]?.id ?? null;
    return items[idx + 1]?.id ?? null;
  }

  async function handleApprove() {
    if (!selectedId || actionPending) return;
    const currentId = selectedId;
    const nextId = nextSelection(currentId);
    setActionPending(true);
    setActionError(null);
    try {
      await verifyAdminPath(currentId);
      // Optimistically remove the verified row from the unverified list.
      setItems((prev) =>
        prev ? prev.filter((it) => it.id !== currentId) : prev,
      );
      if (nextId) {
        setSelectedId(nextId);
      } else {
        // End of page -> reload to see if there's another page.
        await fetchList(statusFilter, offset);
      }
    } catch (e) {
      setActionError(e instanceof ApiError ? e.detail : t("verifyError"));
    } finally {
      setActionPending(false);
    }
  }

  async function handleReject() {
    if (!selectedId || actionPending) return;
    const currentId = selectedId;
    const nextId = nextSelection(currentId);
    setActionPending(true);
    setActionError(null);
    try {
      await rejectAdminPath(currentId);
      // Soft-reject: row stays but the unverified queue no longer
      // shows it (it now has verified=false explicitly stamped, which
      // matches the pre-state, so we still remove it from the queue
      // so the admin doesn't see it twice).
      setItems((prev) =>
        prev ? prev.filter((it) => it.id !== currentId) : prev,
      );
      if (nextId) {
        setSelectedId(nextId);
      } else {
        await fetchList(statusFilter, offset);
      }
    } catch (e) {
      setActionError(e instanceof ApiError ? e.detail : t("rejectError"));
    } finally {
      setActionPending(false);
    }
  }

  return (
    <div className="flex h-[calc(100vh-3.5rem)] w-full">
      {/* List pane */}
      <aside className="flex w-full max-w-sm flex-col border-r border-border bg-card/30">
        <div className="flex items-center justify-between gap-2 border-b border-border px-4 py-3">
          <h2 className="text-sm font-semibold text-foreground">
            {t("queueTitle")}
          </h2>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => void fetchList(statusFilter, offset)}
            disabled={listLoading}
            aria-label={t("refresh")}
          >
            <RefreshCw
              className={cn(
                "h-3.5 w-3.5",
                listLoading && "animate-spin",
              )}
            />
          </Button>
        </div>

        <div className="flex gap-1 border-b border-border px-3 py-2">
          {(["unverified", "verified", "all"] as AdminPathStatusFilter[]).map(
            (sf) => (
              <button
                key={sf}
                type="button"
                onClick={() => setStatusFilter(sf)}
                className={cn(
                  "rounded-md px-2.5 py-1 text-xs font-medium transition-colors",
                  statusFilter === sf
                    ? "bg-primary/10 text-primary"
                    : "text-muted-foreground hover:bg-muted",
                )}
              >
                {t(`filter.${sf}`)}
              </button>
            ),
          )}
        </div>

        {listError && <StatusMessage type="error">{listError}</StatusMessage>}

        <div className="flex-1 overflow-y-auto">
          {items === null ? (
            <p className="p-4 text-sm text-muted-foreground">{t("loading")}</p>
          ) : items.length === 0 ? (
            <p className="p-4 text-sm text-muted-foreground">{t("queueEmpty")}</p>
          ) : (
            <ul className="flex flex-col">
              {items.map((it) => (
                <li key={it.id}>
                  <button
                    type="button"
                    onClick={() => setSelectedId(it.id)}
                    className={cn(
                      "flex w-full flex-col items-start gap-1 border-b border-border px-4 py-3 text-left transition-colors",
                      selectedId === it.id
                        ? "bg-primary/5"
                        : "hover:bg-muted/40",
                    )}
                  >
                    <div className="flex w-full items-center justify-between gap-2">
                      <span className="truncate text-sm font-medium text-foreground">
                        {it.name}
                      </span>
                      <CriticBadge score={it.critic_score} />
                    </div>
                    <span className="text-xs text-muted-foreground">
                      {it.problem_type}
                      {it.problem_difficulty
                        ? ` · ${it.problem_difficulty}`
                        : ""}
                      {it.preferred ? ` · ${t("preferred")}` : ""}
                    </span>
                    <span className="line-clamp-2 text-xs text-muted-foreground">
                      {it.problem_preview}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="flex items-center justify-between gap-2 border-t border-border px-3 py-2 text-xs text-muted-foreground">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            disabled={offset === 0 || listLoading}
            onClick={() =>
              void fetchList(statusFilter, Math.max(0, offset - PAGE_SIZE))
            }
          >
            <ChevronLeft className="h-3.5 w-3.5" />
            {t("prev")}
          </Button>
          <span>
            {items && items.length > 0
              ? t("paginationRange", {
                  start: offset + 1,
                  end: offset + items.length,
                })
              : ""}
          </span>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            disabled={nextOffset === null || listLoading}
            onClick={() =>
              nextOffset !== null && void fetchList(statusFilter, nextOffset)
            }
          >
            {t("next")}
            <ChevronRight className="h-3.5 w-3.5" />
          </Button>
        </div>
      </aside>

      {/* Detail pane */}
      <section className="flex flex-1 flex-col overflow-hidden">
        {detailLoading && (
          <p className="p-6 text-sm text-muted-foreground">{t("loading")}</p>
        )}
        {detailError && <StatusMessage type="error">{detailError}</StatusMessage>}
        {!detail && !detailLoading && !detailError && (
          <div className="flex flex-1 items-center justify-center">
            <p className="text-sm text-muted-foreground">{t("selectPrompt")}</p>
          </div>
        )}
        {detail && (
          <PathDetailView
            detail={detail}
            onApprove={handleApprove}
            onReject={handleReject}
            actionPending={actionPending}
            actionError={actionError}
          />
        )}
      </section>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Critic-score badge
// ---------------------------------------------------------------------------
function CriticBadge({ score }: { score: number | null }) {
  if (score === null) {
    return (
      <span className="shrink-0 rounded-full bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
        — / 5
      </span>
    );
  }
  // Greenish for >= 4, neutral for 3-4, reddish for < 3.
  const tone =
    score >= 4
      ? "bg-success/10 text-success"
      : score >= 3
        ? "bg-muted text-muted-foreground"
        : "bg-destructive/10 text-destructive";
  return (
    <span
      className={cn(
        "shrink-0 rounded-full px-1.5 py-0.5 text-[10px] font-medium",
        tone,
      )}
    >
      {score.toFixed(1)} / 5
    </span>
  );
}

// ---------------------------------------------------------------------------
// Detail pane
// ---------------------------------------------------------------------------
function PathDetailView({
  detail,
  onApprove,
  onReject,
  actionPending,
  actionError,
}: {
  detail: AdminPathDetail;
  onApprove: () => void;
  onReject: () => void;
  actionPending: boolean;
  actionError: string | null;
}) {
  const t = useTranslations("admin.paths");
  const { path, problem, steps, hints_by_step, mistakes_by_step } = detail;

  return (
    <div className="flex h-full flex-col">
      {/* Header */}
      <div className="flex items-start justify-between gap-4 border-b border-border bg-card/40 px-6 py-4">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-lg font-semibold text-foreground">
              {path.name}
            </h2>
            {path.preferred && (
              <span className="rounded-full bg-primary/10 px-2 py-0.5 text-xs font-medium text-primary">
                {t("preferred")}
              </span>
            )}
            {path.verified && (
              <span className="inline-flex items-center gap-1 rounded-full bg-success/10 px-2 py-0.5 text-xs font-medium text-success">
                <CheckCircle2 className="h-3 w-3" aria-hidden />
                {t("verified")}
              </span>
            )}
            <CriticBadge score={path.critic_score} />
          </div>
          {path.rationale && (
            <p className="mt-1 text-sm text-muted-foreground">
              {path.rationale}
            </p>
          )}
          <p className="mt-1 text-xs text-muted-foreground">
            {t("source")}: {path.source ?? "—"} · {t("model")}:{" "}
            {path.model ?? "—"} · {t("problemTypeLabel")}: {problem.type}
            {problem.difficulty ? ` · ${problem.difficulty}` : ""}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <Button
            type="button"
            variant="outline"
            onClick={onReject}
            disabled={actionPending}
          >
            <X className="h-4 w-4" />
            {t("reject")}
          </Button>
          <Button
            type="button"
            onClick={onApprove}
            disabled={actionPending}
          >
            <Check className="h-4 w-4" />
            {t("approve")}
          </Button>
        </div>
      </div>

      {actionError && <StatusMessage type="error">{actionError}</StatusMessage>}

      {/* Body — split: problem / solution on the left, path tree on the right */}
      <div className="grid flex-1 grid-cols-1 gap-0 overflow-hidden lg:grid-cols-2">
        <div className="overflow-y-auto border-b border-border lg:border-b-0 lg:border-r">
          <div className="space-y-6 p-6">
            <section>
              <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                {t("problemHeading")}
              </h3>
              <div className="rounded-md border border-border bg-card/60 p-4 text-sm leading-relaxed">
                <MarkdownContent>{problem.problem_en}</MarkdownContent>
              </div>
            </section>
            <section>
              <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                {t("workedSolutionHeading")}
              </h3>
              <div className="rounded-md border border-border bg-card/60 p-4 text-sm leading-relaxed">
                <MarkdownContent>{problem.solution_en}</MarkdownContent>
              </div>
            </section>
            {problem.answer && (
              <section>
                <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                  {t("answerHeading")}
                </h3>
                <p className="rounded-md border border-border bg-card/60 p-4 text-sm">
                  {problem.answer}
                </p>
              </section>
            )}
            {detail.problem_scoped_mistakes.length > 0 && (
              <section>
                <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                  {t("problemMistakesHeading")}
                </h3>
                <div className="flex flex-col gap-3">
                  {detail.problem_scoped_mistakes.map((m) => (
                    <MistakeCard key={m.id} mistake={m} />
                  ))}
                </div>
              </section>
            )}
          </div>
        </div>

        <div className="overflow-y-auto">
          <div className="space-y-4 p-6">
            <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              {t("stepsHeading", { count: steps.length })}
            </h3>
            {steps.length === 0 ? (
              <p className="text-sm text-muted-foreground">{t("noSteps")}</p>
            ) : (
              <ol className="space-y-4">
                {steps.map((step) => (
                  <li key={step.id}>
                    <StepCard
                      step={step}
                      hints={hints_by_step[step.id] ?? []}
                      mistakes={mistakes_by_step[step.id] ?? []}
                    />
                  </li>
                ))}
              </ol>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Step + hint + mistake cards
// ---------------------------------------------------------------------------
function StepCard({
  step,
  hints,
  mistakes,
}: {
  step: AdminSolutionStep;
  hints: AdminStepHint[];
  mistakes: AdminCommonMistake[];
}) {
  const t = useTranslations("admin.paths");
  return (
    <div className="rounded-md border border-border bg-card p-4">
      <div className="mb-2 flex items-center gap-2">
        <span className="rounded-full bg-primary/10 px-2 py-0.5 text-xs font-medium text-primary">
          {t("stepLabel", { index: step.step_index })}
        </span>
        {step.is_terminal && (
          <span className="rounded-full bg-success/10 px-2 py-0.5 text-xs font-medium text-success">
            {t("terminal")}
          </span>
        )}
      </div>
      <p className="text-sm font-medium text-foreground">{step.goal}</p>
      {step.expected_action && (
        <p className="mt-2 text-xs text-muted-foreground">
          <span className="font-semibold">{t("expectedActionLabel")}: </span>
          {step.expected_action}
        </p>
      )}
      {step.expected_state && (
        <p className="mt-1 text-xs text-muted-foreground">
          <span className="font-semibold">{t("expectedStateLabel")}: </span>
          <code className="rounded bg-muted px-1 py-0.5">
            {step.expected_state}
          </code>
        </p>
      )}

      {hints.length > 0 && (
        <div className="mt-3">
          <p className="mb-1.5 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
            {t("hintsHeading")}
          </p>
          <ol className="flex flex-col gap-1.5">
            {hints
              .slice()
              .sort((a, b) => a.hint_index - b.hint_index)
              .map((h) => (
                <li
                  key={h.id}
                  className="rounded border border-border bg-background px-2.5 py-1.5 text-xs"
                >
                  <span className="mr-2 font-semibold text-muted-foreground">
                    #{h.hint_index}
                  </span>
                  {h.body}
                </li>
              ))}
          </ol>
        </div>
      )}

      {mistakes.length > 0 && (
        <div className="mt-3">
          <p className="mb-1.5 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
            {t("mistakesHeading")}
          </p>
          <div className="flex flex-col gap-2">
            {mistakes.map((m) => (
              <MistakeCard key={m.id} mistake={m} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function MistakeCard({ mistake }: { mistake: AdminCommonMistake }) {
  const t = useTranslations("admin.paths");
  return (
    <div className="rounded border border-destructive/20 bg-destructive/5 px-2.5 py-2 text-xs">
      <p className="font-medium text-foreground">{mistake.pattern}</p>
      {mistake.detection_hint && (
        <p className="mt-1 text-[11px] text-muted-foreground">
          <span className="font-semibold">{t("detectionLabel")}: </span>
          {mistake.detection_hint}
        </p>
      )}
      <p className="mt-1.5 text-[11px] text-muted-foreground">
        <span className="font-semibold">{t("pedagogicalHintLabel")}: </span>
        {mistake.pedagogical_hint}
      </p>
      {mistake.remediation_topic && (
        <p className="mt-1 text-[11px] text-muted-foreground">
          <span className="font-semibold">{t("remediationLabel")}: </span>
          {mistake.remediation_topic}
        </p>
      )}
    </div>
  );
}
