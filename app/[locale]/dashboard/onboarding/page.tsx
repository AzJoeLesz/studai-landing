"use client";

import { useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { StatusMessage } from "@/components/ui/status-message";
import { MarkdownContent } from "@/components/chat/markdown-content";
import { useRouter } from "@/i18n/navigation";
import {
  getPlacementStatus,
  seedGradePriors,
  startPlacement,
  submitPlacementAnswer,
  type PlacementAnswerResponse,
  type PlacementProblem,
} from "@/lib/api/onboarding";
import { getSupabaseBrowserClient } from "@/lib/supabase/client";
import type { StudentProgress } from "@/lib/api/types";

/**
 * Onboarding flow for newly-registered students.
 *
 * Steps:
 *   1. Personality micro-survey (3 questions). Required to proceed.
 *      Saved into `profiles.preferences` JSONB. After save, also calls
 *      `/onboarding/seed-priors` so the tutor's first chat already has
 *      grade-derived mastery priors.
 *
 *   2. Optional placement quiz (5 adaptive questions). Skippable. Each
 *      answer feeds the BKT-IDEM mastery model. The user self-grades
 *      ("I got it right" / "I don't know") for the MVP — automatic
 *      correctness checking arrives with the Phase 10 step-graphs.
 *
 *   3. Completion. Routes to /dashboard/sessions.
 *
 * The route is reachable from the auth flow (post-signup redirect — see
 * `app/[locale]/page.tsx`) and from a "Re-take" link in Settings.
 */

const POST_DONE_PATH = "/dashboard/sessions";

type Step =
  | { kind: "personality" }
  | { kind: "placementIntro" }
  | { kind: "placement"; current: PlacementProblem }
  | { kind: "completed"; summary: StudentProgress[] | null };

interface PersonalityAnswers {
  hint_style: "fast_hints" | "figure_out" | "worked_example" | "";
  math_affect: "curious" | "neutral" | "anxious" | "";
  example_flavor: "story" | "pure" | "visual" | "";
}

const EMPTY_ANSWERS: PersonalityAnswers = {
  hint_style: "",
  math_affect: "",
  example_flavor: "",
};

export default function OnboardingPage() {
  const t = useTranslations("onboarding");
  const router = useRouter();
  const supabase = getSupabaseBrowserClient();

  const [step, setStep] = useState<Step>({ kind: "personality" });
  const [answers, setAnswers] = useState<PersonalityAnswers>(EMPTY_ANSWERS);
  const [savingPersonality, setSavingPersonality] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [submittingAnswer, setSubmittingAnswer] = useState(false);

  // Hydrate any partial preferences the user may have already saved.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const { data: auth } = await supabase.auth.getUser();
      if (!auth.user) {
        router.replace("/");
        return;
      }
      const { data } = await supabase
        .from("profiles")
        .select("preferences")
        .eq("id", auth.user.id)
        .maybeSingle();
      if (cancelled) return;
      const prefs = (data?.preferences ?? {}) as Partial<PersonalityAnswers>;
      setAnswers({
        hint_style: (prefs.hint_style as PersonalityAnswers["hint_style"]) || "",
        math_affect:
          (prefs.math_affect as PersonalityAnswers["math_affect"]) || "",
        example_flavor:
          (prefs.example_flavor as PersonalityAnswers["example_flavor"]) || "",
      });
    })();
    return () => {
      cancelled = true;
    };
  }, [supabase, router]);

  async function savePersonalityAndContinue() {
    setSavingPersonality(true);
    setErrorMessage(null);
    try {
      const { data: auth } = await supabase.auth.getUser();
      if (!auth.user) {
        router.replace("/");
        return;
      }
      const merged = {
        hint_style: answers.hint_style || null,
        math_affect: answers.math_affect || null,
        example_flavor: answers.example_flavor || null,
      };
      const { error } = await supabase
        .from("profiles")
        .upsert({ id: auth.user.id, preferences: merged });
      if (error) {
        throw new Error(error.message);
      }
      // Seed grade priors in the background — failures here are not
      // fatal, the tutor still works without seeded priors.
      try {
        await seedGradePriors();
      } catch {
        // ignore
      }
      // Decide whether to offer placement: skip if already completed.
      try {
        const status = await getPlacementStatus();
        if (status.completed) {
          setStep({ kind: "completed", summary: null });
          return;
        }
      } catch {
        // If we can't check status, still let the user opt in.
      }
      setStep({ kind: "placementIntro" });
    } catch {
      setErrorMessage(t("errorSaving"));
    } finally {
      setSavingPersonality(false);
    }
  }

  async function beginPlacement() {
    setErrorMessage(null);
    try {
      const res = await startPlacement();
      if (res.completed || !res.next) {
        setStep({ kind: "completed", summary: null });
      } else {
        setStep({ kind: "placement", current: res.next });
      }
    } catch {
      setErrorMessage(t("errorLoading"));
    }
  }

  async function answerPlacement(correct: boolean) {
    if (step.kind !== "placement" || submittingAnswer) return;
    setSubmittingAnswer(true);
    setErrorMessage(null);
    try {
      const res: PlacementAnswerResponse = await submitPlacementAnswer({
        problem_id: step.current.problem_id,
        topic: step.current.topic,
        difficulty: step.current.difficulty,
        correct,
      });
      if (res.completed || !res.next) {
        setStep({ kind: "completed", summary: res.summary ?? null });
      } else {
        setStep({ kind: "placement", current: res.next });
      }
    } catch {
      setErrorMessage(t("errorSaving"));
    } finally {
      setSubmittingAnswer(false);
    }
  }

  const personalityComplete = useMemo(
    () =>
      Boolean(
        answers.hint_style && answers.math_affect && answers.example_flavor,
      ),
    [answers],
  );

  return (
    <div className="mx-auto flex w-full max-w-2xl flex-col gap-6 px-6 py-10 sm:px-10 sm:py-14">
      <header>
        <h1 className="text-3xl font-serif text-foreground">{t("title")}</h1>
        <p className="mt-2 text-sm text-muted-foreground">{t("description")}</p>
      </header>

      {step.kind === "personality" && (
        <PersonalityCard
          answers={answers}
          onChange={setAnswers}
          onContinue={savePersonalityAndContinue}
          onSkip={() => router.replace(POST_DONE_PATH)}
          saving={savingPersonality}
          canContinue={personalityComplete}
        />
      )}

      {step.kind === "placementIntro" && (
        <PlacementIntroCard
          onStart={beginPlacement}
          onSkip={() => setStep({ kind: "completed", summary: null })}
        />
      )}

      {step.kind === "placement" && (
        <PlacementQuestionCard
          problem={step.current}
          submitting={submittingAnswer}
          onAnswer={answerPlacement}
          onSkipQuiz={() => setStep({ kind: "completed", summary: null })}
        />
      )}

      {step.kind === "completed" && (
        <CompletedCard
          summary={step.summary}
          onContinue={() => router.replace(POST_DONE_PATH)}
        />
      )}

      {errorMessage && (
        <StatusMessage type="error">{errorMessage}</StatusMessage>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Step 1: personality micro-survey
// ---------------------------------------------------------------------------
interface PersonalityCardProps {
  answers: PersonalityAnswers;
  onChange: (next: PersonalityAnswers) => void;
  onContinue: () => void;
  onSkip: () => void;
  saving: boolean;
  canContinue: boolean;
}

function PersonalityCard({
  answers,
  onChange,
  onContinue,
  onSkip,
  saving,
  canContinue,
}: PersonalityCardProps) {
  const t = useTranslations("onboarding");
  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("personality.title")}</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-6">
        <ChoiceGroup
          prompt={t("personality.q1.prompt")}
          name="hint_style"
          value={answers.hint_style}
          options={[
            { value: "fast_hints", label: t("personality.q1.fastHints") },
            { value: "figure_out", label: t("personality.q1.figureOut") },
            {
              value: "worked_example",
              label: t("personality.q1.workedExample"),
            },
          ]}
          onSelect={(value) =>
            onChange({
              ...answers,
              hint_style: value as PersonalityAnswers["hint_style"],
            })
          }
        />
        <ChoiceGroup
          prompt={t("personality.q2.prompt")}
          name="math_affect"
          value={answers.math_affect}
          options={[
            { value: "curious", label: t("personality.q2.curious") },
            { value: "neutral", label: t("personality.q2.neutral") },
            { value: "anxious", label: t("personality.q2.anxious") },
          ]}
          onSelect={(value) =>
            onChange({
              ...answers,
              math_affect: value as PersonalityAnswers["math_affect"],
            })
          }
        />
        <ChoiceGroup
          prompt={t("personality.q3.prompt")}
          name="example_flavor"
          value={answers.example_flavor}
          options={[
            { value: "story", label: t("personality.q3.story") },
            { value: "pure", label: t("personality.q3.pure") },
            { value: "visual", label: t("personality.q3.visual") },
          ]}
          onSelect={(value) =>
            onChange({
              ...answers,
              example_flavor:
                value as PersonalityAnswers["example_flavor"],
            })
          }
        />

        <div className="mt-2 flex flex-col gap-3 sm:flex-row sm:items-center sm:gap-4">
          <Button
            type="button"
            onClick={onContinue}
            disabled={!canContinue || saving}
          >
            {saving ? t("saving") : t("savedAndContinue")}
          </Button>
          <Button type="button" variant="ghost" onClick={onSkip}>
            {t("skip")}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

interface ChoiceOption {
  value: string;
  label: string;
}

interface ChoiceGroupProps {
  prompt: string;
  name: string;
  value: string;
  options: ChoiceOption[];
  onSelect: (value: string) => void;
}

function ChoiceGroup({
  prompt,
  name,
  value,
  options,
  onSelect,
}: ChoiceGroupProps) {
  return (
    <fieldset className="flex flex-col gap-3">
      <legend className="text-sm font-medium text-foreground">{prompt}</legend>
      <div className="flex flex-col gap-2">
        {options.map((opt) => {
          const selected = value === opt.value;
          return (
            <label
              key={opt.value}
              className={
                "flex cursor-pointer items-center gap-3 rounded-md border px-3 py-2 text-sm transition-colors " +
                (selected
                  ? "border-primary bg-primary/5 text-foreground"
                  : "border-border hover:border-foreground/30")
              }
            >
              <input
                type="radio"
                name={name}
                value={opt.value}
                checked={selected}
                onChange={() => onSelect(opt.value)}
                className="h-3.5 w-3.5"
              />
              <span>{opt.label}</span>
            </label>
          );
        })}
      </div>
    </fieldset>
  );
}

// ---------------------------------------------------------------------------
// Step 2a: placement quiz intro
// ---------------------------------------------------------------------------
interface PlacementIntroCardProps {
  onStart: () => void;
  onSkip: () => void;
}

function PlacementIntroCard({ onStart, onSkip }: PlacementIntroCardProps) {
  const t = useTranslations("onboarding");
  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("placement.title")}</CardTitle>
        <CardDescription>{t("placement.intro")}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3 sm:flex-row sm:items-center sm:gap-4">
        <Button type="button" onClick={onStart}>
          {t("placement.start")}
        </Button>
        <Button type="button" variant="ghost" onClick={onSkip}>
          {t("skipPlacement")}
        </Button>
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Step 2b: a single placement question
// ---------------------------------------------------------------------------
interface PlacementQuestionCardProps {
  problem: PlacementProblem;
  submitting: boolean;
  onAnswer: (correct: boolean) => void;
  onSkipQuiz: () => void;
}

const PLACEMENT_TOTAL = 5;

function PlacementQuestionCard({
  problem,
  submitting,
  onAnswer,
  onSkipQuiz,
}: PlacementQuestionCardProps) {
  const t = useTranslations("onboarding");
  const [reveal, setReveal] = useState(false);

  // Reset the "show me the answer" toggle when we move to a new question.
  useEffect(() => {
    setReveal(false);
  }, [problem.problem_id]);

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <CardTitle className="text-lg">
            {t("placement.questionLabel")} {problem.question_index} {t("placement.of")}{" "}
            {PLACEMENT_TOTAL}
          </CardTitle>
          <p className="text-xs text-muted-foreground">
            {t("placement.topicLabel")}: <strong>{problem.topic}</strong> ·{" "}
            {t("placement.difficultyLabel")}: <strong>{problem.difficulty}</strong>
          </p>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-5">
        <div className="rounded-md border border-border bg-muted/40 p-4">
          <MarkdownContent>{problem.problem_text}</MarkdownContent>
        </div>

        <div className="flex flex-col gap-2 text-sm text-muted-foreground">
          <p>{t("placement.yourAnswer")}:</p>
          <div className="flex flex-col gap-2 sm:flex-row sm:gap-3">
            <Button
              type="button"
              onClick={() => onAnswer(true)}
              disabled={submitting}
            >
              {submitting ? t("placement.submitting") : "✓"}
            </Button>
            <Button
              type="button"
              variant="outline"
              onClick={() => onAnswer(false)}
              disabled={submitting}
            >
              {t("placement.iDontKnow")}
            </Button>
          </div>
          <p className="text-xs text-muted-foreground">
            Tap ✓ if you got it right, or “{t("placement.iDontKnow")}” if not.
          </p>
        </div>

        {problem.answer && (
          <details
            className="rounded-md border border-border bg-background p-3"
            open={reveal}
            onToggle={(e) => setReveal((e.target as HTMLDetailsElement).open)}
          >
            <summary className="cursor-pointer text-sm font-medium">
              Show answer
            </summary>
            <div className="mt-2 text-sm">
              <MarkdownContent>{problem.answer}</MarkdownContent>
            </div>
          </details>
        )}

        <div className="flex justify-end">
          <Button type="button" variant="ghost" onClick={onSkipQuiz}>
            {t("skipPlacement")}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Step 3: completed
// ---------------------------------------------------------------------------
interface CompletedCardProps {
  summary: StudentProgress[] | null;
  onContinue: () => void;
}

function CompletedCard({ summary, onContinue }: CompletedCardProps) {
  const t = useTranslations("onboarding");
  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("placement.completedTitle")}</CardTitle>
        <CardDescription>{t("placement.completedBody")}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {summary && summary.length > 0 && (
          <div className="rounded-md border border-border bg-muted/30 p-4 text-sm">
            <p className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
              Topics
            </p>
            <ul className="flex flex-col gap-1">
              {summary.slice(0, 6).map((row) => (
                <li
                  key={`${row.user_id}-${row.topic}`}
                  className="flex items-baseline justify-between gap-3"
                >
                  <span className="text-foreground">{row.topic}</span>
                  <span className="font-mono text-xs text-muted-foreground">
                    {(row.mastery_score * 100).toFixed(0)}%
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}
        <Button type="button" onClick={onContinue}>
          {t("placement.goToTutor")}
        </Button>
      </CardContent>
    </Card>
  );
}
