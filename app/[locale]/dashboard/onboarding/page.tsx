"use client";

import {
  useEffect,
  useMemo,
  useState,
  type FormEvent,
} from "react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
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
 * Onboarding flow for newly-registered students. Phase 9 + the iteration
 * fixes after first-test feedback.
 *
 * Steps:
 *   0. About you  — display name, age, grade level. Saved to `profiles`.
 *      MUST come first so:
 *        - the personality questions can be age-adapted
 *        - `seedGradePriors()` actually has a grade to seed from
 *        - the placement quiz draws from grade-appropriate topics
 *
 *   1. Personality micro-survey (3 questions). Question phrasing branches
 *      on age (kid-friendly variant for age <= 11). Saved into
 *      `profiles.preferences` JSONB.
 *
 *   2. Optional placement quiz (5 staircase questions). Each answer is a
 *      free-text input judged by the backend's LLM judge — no more
 *      self-grading. Per-answer feedback ("Correct" / "Not quite —
 *      correct answer was X") shown briefly between questions.
 *
 *   3. Completion screen with mastery summary.
 *
 * Reachable from the auth flow (sessions page redirects new users) and
 * from the "Re-take" link in Settings.
 */

const POST_DONE_PATH = "/dashboard/sessions";
const KID_AGE_THRESHOLD = 11; // age <= this -> use the *Kid string variants

type Step =
  | { kind: "aboutYou" }
  | { kind: "personality" }
  | { kind: "placementIntro" }
  | { kind: "placement"; current: PlacementProblem }
  | { kind: "feedback"; result: PlacementAnswerResponse; lastAnswer: string }
  | { kind: "completed"; summary: StudentProgress[] | null };

interface AboutYouAnswers {
  display_name: string;
  age: string; // form field; coerced to int on save
  grade_level: string;
}

interface PersonalityAnswers {
  hint_style: "fast_hints" | "figure_out" | "worked_example" | "";
  math_affect: "curious" | "neutral" | "anxious" | "";
  example_flavor: "story" | "pure" | "visual" | "";
}

const EMPTY_ABOUT: AboutYouAnswers = {
  display_name: "",
  age: "",
  grade_level: "",
};

const EMPTY_PERSONALITY: PersonalityAnswers = {
  hint_style: "",
  math_affect: "",
  example_flavor: "",
};

export default function OnboardingPage() {
  const t = useTranslations("onboarding");
  const router = useRouter();
  const supabase = getSupabaseBrowserClient();

  const [step, setStep] = useState<Step>({ kind: "aboutYou" });
  const [about, setAbout] = useState<AboutYouAnswers>(EMPTY_ABOUT);
  const [personality, setPersonality] =
    useState<PersonalityAnswers>(EMPTY_PERSONALITY);
  const [savingAbout, setSavingAbout] = useState(false);
  const [savingPersonality, setSavingPersonality] = useState(false);
  const [submittingAnswer, setSubmittingAnswer] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const ageNum = parseInt(about.age, 10);
  const isKid = Number.isInteger(ageNum) && ageNum <= KID_AGE_THRESHOLD;

  // Hydrate any partial profile/preferences the user may have already saved
  // (so re-takes via the Settings link prefill correctly).
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
        .select("display_name, age, grade_level, preferences")
        .eq("id", auth.user.id)
        .maybeSingle();
      if (cancelled) return;
      if (data) {
        setAbout({
          display_name: data.display_name ?? "",
          age: data.age != null ? String(data.age) : "",
          grade_level: data.grade_level ?? "",
        });
        const prefs =
          (data.preferences ?? {}) as Partial<PersonalityAnswers>;
        setPersonality({
          hint_style:
            (prefs.hint_style as PersonalityAnswers["hint_style"]) || "",
          math_affect:
            (prefs.math_affect as PersonalityAnswers["math_affect"]) || "",
          example_flavor:
            (prefs.example_flavor as PersonalityAnswers["example_flavor"]) ||
            "",
        });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [supabase, router]);

  // ---- Step 0: save about-you, then seed grade priors ----------------
  async function saveAboutAndContinue() {
    setSavingAbout(true);
    setErrorMessage(null);
    try {
      const { data: auth } = await supabase.auth.getUser();
      if (!auth.user) {
        router.replace("/");
        return;
      }
      const ageParsed = about.age.trim() ? parseInt(about.age, 10) : NaN;
      const ageValue =
        Number.isInteger(ageParsed) && ageParsed >= 5 && ageParsed <= 30
          ? ageParsed
          : null;
      const { error } = await supabase.from("profiles").upsert({
        id: auth.user.id,
        display_name: about.display_name.trim() || null,
        age: ageValue,
        grade_level: about.grade_level.trim() || null,
      });
      if (error) throw new Error(error.message);

      // Seed grade priors NOW that we have a grade level on file. This is
      // what powers grade-appropriate topic variety in the placement
      // quiz that's two steps ahead.
      try {
        await seedGradePriors();
      } catch {
        // non-fatal -- the tutor still works without seeded priors
      }
      setStep({ kind: "personality" });
    } catch {
      setErrorMessage(t("errorSaving"));
    } finally {
      setSavingAbout(false);
    }
  }

  // ---- Step 1: save personality, then either offer placement or finish -
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
        hint_style: personality.hint_style || null,
        math_affect: personality.math_affect || null,
        example_flavor: personality.example_flavor || null,
      };
      const { error } = await supabase
        .from("profiles")
        .upsert({ id: auth.user.id, preferences: merged });
      if (error) throw new Error(error.message);

      try {
        const status = await getPlacementStatus();
        if (status.completed) {
          setStep({ kind: "completed", summary: null });
          return;
        }
      } catch {
        // fall through and show the intro anyway
      }
      setStep({ kind: "placementIntro" });
    } catch {
      setErrorMessage(t("errorSaving"));
    } finally {
      setSavingPersonality(false);
    }
  }

  // ---- Step 2: placement -------------------------------------------------
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

  async function answerPlacement(studentAnswer: string) {
    if (step.kind !== "placement" || submittingAnswer) return;
    setSubmittingAnswer(true);
    setErrorMessage(null);
    try {
      const res = await submitPlacementAnswer({
        problem_id: step.current.problem_id,
        topic: step.current.topic,
        difficulty: step.current.difficulty,
        student_answer: studentAnswer,
        problem_text: step.current.problem_text,
        canonical_answer: step.current.answer,
      });
      // Show feedback for this answer; user clicks through to next.
      setStep({ kind: "feedback", result: res, lastAnswer: studentAnswer });
    } catch {
      setErrorMessage(t("errorSaving"));
    } finally {
      setSubmittingAnswer(false);
    }
  }

  function continueAfterFeedback() {
    if (step.kind !== "feedback") return;
    const { result } = step;
    if (result.completed || !result.next) {
      setStep({ kind: "completed", summary: result.summary });
    } else {
      setStep({ kind: "placement", current: result.next });
    }
  }

  // ---- Render ----------------------------------------------------------
  const aboutComplete = useMemo(
    () =>
      Boolean(
        about.display_name.trim() &&
          about.grade_level.trim() &&
          (about.age.trim() === "" || Number.isInteger(parseInt(about.age, 10))),
      ),
    [about],
  );

  const personalityComplete = useMemo(
    () =>
      Boolean(
        personality.hint_style &&
          personality.math_affect &&
          personality.example_flavor,
      ),
    [personality],
  );

  return (
    <div className="mx-auto flex w-full max-w-2xl flex-col gap-6 px-6 py-10 sm:px-10 sm:py-14">
      <header>
        <h1 className="text-3xl font-serif text-foreground">{t("title")}</h1>
        <p className="mt-2 text-sm text-muted-foreground">{t("description")}</p>
      </header>

      {step.kind === "aboutYou" && (
        <AboutYouCard
          values={about}
          onChange={setAbout}
          onContinue={saveAboutAndContinue}
          onSkip={() => router.replace(POST_DONE_PATH)}
          saving={savingAbout}
          canContinue={aboutComplete}
        />
      )}

      {step.kind === "personality" && (
        <PersonalityCard
          answers={personality}
          onChange={setPersonality}
          onContinue={savePersonalityAndContinue}
          onSkip={() => router.replace(POST_DONE_PATH)}
          saving={savingPersonality}
          canContinue={personalityComplete}
          isKid={isKid}
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

      {step.kind === "feedback" && (
        <PlacementFeedbackCard
          result={step.result}
          lastAnswer={step.lastAnswer}
          onContinue={continueAfterFeedback}
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
// Step 0: about you
// ---------------------------------------------------------------------------
interface AboutYouCardProps {
  values: AboutYouAnswers;
  onChange: (next: AboutYouAnswers) => void;
  onContinue: () => void;
  onSkip: () => void;
  saving: boolean;
  canContinue: boolean;
}

function AboutYouCard({
  values,
  onChange,
  onContinue,
  onSkip,
  saving,
  canContinue,
}: AboutYouCardProps) {
  const t = useTranslations("onboarding.aboutYou");
  const tShared = useTranslations("onboarding");

  function submit(e: FormEvent) {
    e.preventDefault();
    if (!canContinue || saving) return;
    onContinue();
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("title")}</CardTitle>
        <CardDescription>{t("description")}</CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={submit} className="flex flex-col gap-5">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="display_name">{t("displayNameLabel")}</Label>
            <Input
              id="display_name"
              value={values.display_name}
              placeholder={t("displayNamePlaceholder")}
              onChange={(e) =>
                onChange({ ...values, display_name: e.target.value })
              }
              maxLength={80}
              autoFocus
            />
          </div>
          <div className="grid grid-cols-1 gap-5 sm:grid-cols-[120px_1fr]">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="age">{t("ageLabel")}</Label>
              <Input
                id="age"
                type="number"
                inputMode="numeric"
                min={5}
                max={30}
                value={values.age}
                placeholder={t("agePlaceholder")}
                onChange={(e) => onChange({ ...values, age: e.target.value })}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="grade_level">{t("gradeLevelLabel")}</Label>
              <Input
                id="grade_level"
                value={values.grade_level}
                placeholder={t("gradeLevelPlaceholder")}
                onChange={(e) =>
                  onChange({ ...values, grade_level: e.target.value })
                }
                maxLength={80}
                required
              />
              <p className="text-xs text-muted-foreground">
                {t("gradeLevelHelp")}
              </p>
            </div>
          </div>
          <div className="mt-2 flex flex-col gap-3 sm:flex-row sm:items-center sm:gap-4">
            <Button type="submit" disabled={!canContinue || saving}>
              {saving ? tShared("saving") : tShared("savedAndContinue")}
            </Button>
            <Button type="button" variant="ghost" onClick={onSkip}>
              {tShared("skip")}
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Step 1: personality micro-survey (age-aware phrasing)
// ---------------------------------------------------------------------------
interface PersonalityCardProps {
  answers: PersonalityAnswers;
  onChange: (next: PersonalityAnswers) => void;
  onContinue: () => void;
  onSkip: () => void;
  saving: boolean;
  canContinue: boolean;
  isKid: boolean;
}

function PersonalityCard({
  answers,
  onChange,
  onContinue,
  onSkip,
  saving,
  canContinue,
  isKid,
}: PersonalityCardProps) {
  const t = useTranslations("onboarding.personality");
  const tShared = useTranslations("onboarding");

  // Helper: pick the kid-variant key when isKid, else the standard key.
  // Each question has both `prompt` / `promptKid` and per-option pairs.
  const k = (base: string) => (isKid ? `${base}Kid` : base);

  return (
    <Card>
      <CardHeader>
        <CardTitle>{isKid ? t("titleKid") : t("title")}</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-6">
        <ChoiceGroup
          prompt={t(k("q1.prompt"))}
          name="hint_style"
          value={answers.hint_style}
          options={[
            { value: "fast_hints", label: t(k("q1.fastHints")) },
            { value: "figure_out", label: t(k("q1.figureOut")) },
            { value: "worked_example", label: t(k("q1.workedExample")) },
          ]}
          onSelect={(value) =>
            onChange({
              ...answers,
              hint_style: value as PersonalityAnswers["hint_style"],
            })
          }
        />
        <ChoiceGroup
          prompt={t(k("q2.prompt"))}
          name="math_affect"
          value={answers.math_affect}
          options={[
            { value: "curious", label: t(k("q2.curious")) },
            { value: "neutral", label: t(k("q2.neutral")) },
            { value: "anxious", label: t(k("q2.anxious")) },
          ]}
          onSelect={(value) =>
            onChange({
              ...answers,
              math_affect: value as PersonalityAnswers["math_affect"],
            })
          }
        />
        <ChoiceGroup
          prompt={t(k("q3.prompt"))}
          name="example_flavor"
          value={answers.example_flavor}
          options={[
            { value: "story", label: t(k("q3.story")) },
            { value: "pure", label: t(k("q3.pure")) },
            { value: "visual", label: t(k("q3.visual")) },
          ]}
          onSelect={(value) =>
            onChange({
              ...answers,
              example_flavor: value as PersonalityAnswers["example_flavor"],
            })
          }
        />

        <div className="mt-2 flex flex-col gap-3 sm:flex-row sm:items-center sm:gap-4">
          <Button
            type="button"
            onClick={onContinue}
            disabled={!canContinue || saving}
          >
            {saving ? tShared("saving") : tShared("savedAndContinue")}
          </Button>
          <Button type="button" variant="ghost" onClick={onSkip}>
            {tShared("skip")}
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
// Step 2a: placement intro
// ---------------------------------------------------------------------------
interface PlacementIntroCardProps {
  onStart: () => void;
  onSkip: () => void;
}

function PlacementIntroCard({ onStart, onSkip }: PlacementIntroCardProps) {
  const t = useTranslations("onboarding");
  const tP = useTranslations("onboarding.placement");
  return (
    <Card>
      <CardHeader>
        <CardTitle>{tP("title")}</CardTitle>
        <CardDescription>{tP("intro")}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3 sm:flex-row sm:items-center sm:gap-4">
        <Button type="button" onClick={onStart}>
          {tP("start")}
        </Button>
        <Button type="button" variant="ghost" onClick={onSkip}>
          {t("skipPlacement")}
        </Button>
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Step 2b: a single placement question (free-text answer + Submit)
// ---------------------------------------------------------------------------
interface PlacementQuestionCardProps {
  problem: PlacementProblem;
  submitting: boolean;
  onAnswer: (studentAnswer: string) => void;
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
  const tP = useTranslations("onboarding.placement");
  const [answer, setAnswer] = useState("");

  // Reset the input when we move to a new question.
  useEffect(() => {
    setAnswer("");
  }, [problem.problem_id]);

  function submit(e: FormEvent) {
    e.preventDefault();
    if (submitting) return;
    onAnswer(answer);
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <CardTitle className="text-lg">
            {tP("questionLabel")} {problem.question_index} {tP("of")}{" "}
            {PLACEMENT_TOTAL}
          </CardTitle>
          <p className="text-xs text-muted-foreground">
            {tP("topicLabel")}: <strong>{problem.topic}</strong> ·{" "}
            {tP("difficultyLabel")}: <strong>{problem.difficulty}</strong>
          </p>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-5">
        <div className="rounded-md border border-border bg-muted/40 p-4">
          <MarkdownContent>{problem.problem_text}</MarkdownContent>
        </div>

        <form onSubmit={submit} className="flex flex-col gap-2">
          <Label htmlFor="placement_answer">{tP("yourAnswer")}</Label>
          <Input
            id="placement_answer"
            value={answer}
            placeholder={tP("answerPlaceholder")}
            onChange={(e) => setAnswer(e.target.value)}
            disabled={submitting}
            autoFocus
            maxLength={2000}
            autoComplete="off"
            autoCorrect="off"
            spellCheck={false}
            data-form-type="other"
          />
          <div className="mt-2 flex flex-col gap-2 sm:flex-row sm:gap-3">
            <Button type="submit" disabled={submitting}>
              {submitting ? tP("submitting") : tP("submit")}
            </Button>
            <Button
              type="button"
              variant="outline"
              onClick={() => onAnswer("")}
              disabled={submitting}
            >
              {tP("iDontKnow")}
            </Button>
            <div className="flex-1" />
            <Button type="button" variant="ghost" onClick={onSkipQuiz}>
              {t("skipPlacement")}
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Step 2c: feedback after each answer
// ---------------------------------------------------------------------------
interface PlacementFeedbackCardProps {
  result: PlacementAnswerResponse;
  lastAnswer: string;
  onContinue: () => void;
}

function PlacementFeedbackCard({
  result,
  lastAnswer,
  onContinue,
}: PlacementFeedbackCardProps) {
  const tP = useTranslations("onboarding.placement");
  const correct = result.was_correct;
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-lg">
          {correct ? tP("feedbackCorrect") : tP("feedbackIncorrect")}
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="text-sm">
          <p className="text-muted-foreground">
            {tP("yourAnswer")}:{" "}
            <span className="text-foreground">
              {lastAnswer.trim() ? lastAnswer : `(${tP("iDontKnow")})`}
            </span>
          </p>
          {!correct && result.canonical_answer && (
            <div className="mt-2">
              <p className="text-muted-foreground">{tP("correctAnswerLabel")}:</p>
              <div className="text-foreground">
                <MarkdownContent>{result.canonical_answer}</MarkdownContent>
              </div>
            </div>
          )}
        </div>
        <Button type="button" onClick={onContinue}>
          {result.completed ? tP("viewResults") : tP("nextQuestion")}
        </Button>
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
  const tP = useTranslations("onboarding.placement");

  // Split rows by source so the user can tell what came from THEIR
  // answers vs from the grade-priors lookup table. Both are real
  // numbers in `student_progress`, but they have very different
  // confidence -- showing them mixed together led demo testers to
  // assume "decimals 80%" came from the quiz when it didn't.
  const { fromQuiz, fromGrade } = useMemo(() => {
    const rows = summary ?? [];
    return {
      fromQuiz: rows.filter(
        (r) => r.evidence_source !== "prior",
      ),
      fromGrade: rows.filter((r) => r.evidence_source === "prior"),
    };
  }, [summary]);

  return (
    <Card>
      <CardHeader>
        <CardTitle>{tP("completedTitle")}</CardTitle>
        <CardDescription>{tP("completedBody")}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-5">
        {fromQuiz.length > 0 && (
          <ProgressList
            heading={tP("topicsFromQuizHeading")}
            rows={fromQuiz}
          />
        )}
        {fromGrade.length > 0 && (
          <ProgressList
            heading={tP("topicsFromGradeHeading")}
            rows={fromGrade.slice(0, 8)}
            note={tP("topicsFromGradeNote")}
          />
        )}
        <Button type="button" onClick={onContinue}>
          {tP("goToTutor")}
        </Button>
      </CardContent>
    </Card>
  );
}

interface ProgressListProps {
  heading: string;
  rows: StudentProgress[];
  note?: string;
}

function ProgressList({ heading, rows, note }: ProgressListProps) {
  return (
    <div className="rounded-md border border-border bg-muted/30 p-4 text-sm">
      <p className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
        {heading}
      </p>
      <ul className="flex flex-col gap-1">
        {rows.map((row) => (
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
      {note && (
        <p className="mt-3 text-xs text-muted-foreground">{note}</p>
      )}
    </div>
  );
}
