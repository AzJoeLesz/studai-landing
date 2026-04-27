"use client";

import { useEffect, useState, type FormEvent } from "react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { StatusMessage } from "@/components/ui/status-message";
import { Link } from "@/i18n/navigation";
import { getSupabaseBrowserClient } from "@/lib/supabase/client";

type StatusType = "info" | "success" | "error";

interface Status {
  message: string;
  type: StatusType;
}

type HintStyle = "fast_hints" | "figure_out" | "worked_example" | "";
type MathAffect = "curious" | "neutral" | "anxious" | "";
type ExampleFlavor = "story" | "pure" | "visual" | "";

interface Preferences {
  hint_style: HintStyle;
  math_affect: MathAffect;
  example_flavor: ExampleFlavor;
}

const EMPTY_PREFS: Preferences = {
  hint_style: "",
  math_affect: "",
  example_flavor: "",
};

/**
 * Profile / settings page.
 *
 * Reads and writes `public.profiles` directly via the Supabase JS client
 * (no Python backend involvement). RLS policies enforce that a user can
 * only touch their own row.
 *
 * Phase 9 expanded this page beyond the initial profile fields:
 *   * Personality preferences (jsonb `preferences` column) drive the
 *     tutor's STYLE DIRECTIVES on every chat turn.
 *   * `share_progress_with_parents` is the consent flag the Phase 13
 *     parent dashboard will read.
 *   * A "Re-take the quick check" link routes to /dashboard/onboarding.
 *
 * The Python tutor agent loads `profile + session_state + progress` on
 * every chat turn and weaves them into the system context.
 */
export default function SettingsPage() {
  const t = useTranslations("settings");
  const supabase = getSupabaseBrowserClient();

  const [displayName, setDisplayName] = useState("");
  const [age, setAge] = useState("");
  const [gradeLevel, setGradeLevel] = useState("");
  const [interests, setInterests] = useState("");
  const [learningGoals, setLearningGoals] = useState("");
  const [notes, setNotes] = useState("");
  const [preferences, setPreferences] = useState<Preferences>(EMPTY_PREFS);
  const [shareProgress, setShareProgress] = useState(false);

  const [initialLoaded, setInitialLoaded] = useState(false);
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState<Status | null>(null);

  useEffect(() => {
    async function load() {
      const { data: auth } = await supabase.auth.getUser();
      if (!auth.user) return;

      const { data, error } = await supabase
        .from("profiles")
        .select(
          "display_name, age, grade_level, interests, learning_goals, notes, preferences, share_progress_with_parents",
        )
        .eq("id", auth.user.id)
        .maybeSingle();

      if (error) {
        setStatus({ message: t("loadError"), type: "error" });
      } else if (data) {
        setDisplayName(data.display_name ?? "");
        setAge(data.age != null ? String(data.age) : "");
        setGradeLevel(data.grade_level ?? "");
        setInterests(data.interests ?? "");
        setLearningGoals(data.learning_goals ?? "");
        setNotes(data.notes ?? "");
        const prefs = (data.preferences ?? {}) as Partial<Preferences>;
        setPreferences({
          hint_style: (prefs.hint_style as HintStyle) || "",
          math_affect: (prefs.math_affect as MathAffect) || "",
          example_flavor: (prefs.example_flavor as ExampleFlavor) || "",
        });
        setShareProgress(Boolean(data.share_progress_with_parents));
      }
      setInitialLoaded(true);
    }

    load();
  }, [supabase, t]);

  async function handleSave(e: FormEvent) {
    e.preventDefault();
    setSaving(true);
    setStatus(null);

    const { data: auth } = await supabase.auth.getUser();
    if (!auth.user) {
      setSaving(false);
      return;
    }

    // Coerce the empty string back to NULL so we don't store empty rows.
    // Numeric coercion for age uses parseInt; out-of-range or non-numeric
    // input becomes NULL silently (the DB constraint also catches it).
    const ageParsed = age.trim() ? parseInt(age, 10) : NaN;
    const ageValue =
      Number.isInteger(ageParsed) && ageParsed >= 5 && ageParsed <= 30
        ? ageParsed
        : null;

    const { error } = await supabase.from("profiles").upsert({
      id: auth.user.id,
      display_name: displayName.trim() || null,
      age: ageValue,
      grade_level: gradeLevel.trim() || null,
      interests: interests.trim() || null,
      learning_goals: learningGoals.trim() || null,
      notes: notes.trim() || null,
      preferences: {
        hint_style: preferences.hint_style || null,
        math_affect: preferences.math_affect || null,
        example_flavor: preferences.example_flavor || null,
      },
      share_progress_with_parents: shareProgress,
    });

    setSaving(false);
    if (error) {
      setStatus({ message: error.message, type: "error" });
    } else {
      setStatus({ message: t("saved"), type: "success" });
      window.setTimeout(() => setStatus(null), 3500);
    }
  }

  return (
    <div className="flex w-full flex-col gap-8 px-6 py-10 sm:px-10 sm:py-14 lg:px-14">
      <header>
        <h1 className="text-3xl font-serif text-foreground">{t("title")}</h1>
        <p className="mt-2 max-w-2xl text-sm text-muted-foreground">
          {t("description")}
        </p>
      </header>

      <form onSubmit={handleSave} className="flex max-w-2xl flex-col gap-6">
        {/* About you ------------------------------------------------------ */}
        <Card>
          <CardHeader>
            <CardTitle>{t("sectionAboutYou")}</CardTitle>
          </CardHeader>

          <CardContent className="flex flex-col gap-5">
            <Field
              id="display_name"
              label={t("displayNameLabel")}
              help={t("displayNameHelp")}
            >
              <Input
                id="display_name"
                value={displayName}
                placeholder={t("displayNamePlaceholder")}
                onChange={(e) => setDisplayName(e.target.value)}
                disabled={!initialLoaded}
                maxLength={80}
              />
            </Field>

            <div className="grid grid-cols-1 gap-5 sm:grid-cols-[120px_1fr]">
              <Field id="age" label={t("ageLabel")}>
                <Input
                  id="age"
                  type="number"
                  inputMode="numeric"
                  min={5}
                  max={30}
                  value={age}
                  placeholder={t("agePlaceholder")}
                  onChange={(e) => setAge(e.target.value)}
                  disabled={!initialLoaded}
                />
              </Field>

              <Field id="grade_level" label={t("gradeLevelLabel")}>
                <Input
                  id="grade_level"
                  value={gradeLevel}
                  placeholder={t("gradeLevelPlaceholder")}
                  onChange={(e) => setGradeLevel(e.target.value)}
                  disabled={!initialLoaded}
                  maxLength={80}
                />
              </Field>
            </div>

            <Field
              id="interests"
              label={t("interestsLabel")}
              help={t("interestsHelp")}
            >
              <Input
                id="interests"
                value={interests}
                placeholder={t("interestsPlaceholder")}
                onChange={(e) => setInterests(e.target.value)}
                disabled={!initialLoaded}
                maxLength={400}
              />
            </Field>
          </CardContent>
        </Card>

        {/* Learning ------------------------------------------------------- */}
        <Card>
          <CardHeader>
            <CardTitle>{t("sectionLearning")}</CardTitle>
          </CardHeader>

          <CardContent className="flex flex-col gap-5">
            <Field id="learning_goals" label={t("learningGoalsLabel")}>
              <Input
                id="learning_goals"
                value={learningGoals}
                placeholder={t("learningGoalsPlaceholder")}
                onChange={(e) => setLearningGoals(e.target.value)}
                disabled={!initialLoaded}
                maxLength={400}
              />
            </Field>

            <Field id="notes" label={t("notesLabel")}>
              <textarea
                id="notes"
                value={notes}
                placeholder={t("notesPlaceholder")}
                onChange={(e) => setNotes(e.target.value)}
                disabled={!initialLoaded}
                maxLength={1000}
                rows={4}
                className="flex w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
              />
            </Field>
          </CardContent>
        </Card>

        {/* Style: how you like to learn (Phase 9C) ------------------------ */}
        <Card>
          <CardHeader>
            <CardTitle>{t("sectionStyle")}</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-6">
            <PreferenceSelect
              id="hint_style"
              namespace="onboarding.personality.q1"
              value={preferences.hint_style}
              options={[
                { value: "fast_hints", labelKey: "fastHints" },
                { value: "figure_out", labelKey: "figureOut" },
                { value: "worked_example", labelKey: "workedExample" },
              ]}
              disabled={!initialLoaded}
              onChange={(v) =>
                setPreferences({ ...preferences, hint_style: v as HintStyle })
              }
            />
            <PreferenceSelect
              id="math_affect"
              namespace="onboarding.personality.q2"
              value={preferences.math_affect}
              options={[
                { value: "curious", labelKey: "curious" },
                { value: "neutral", labelKey: "neutral" },
                { value: "anxious", labelKey: "anxious" },
              ]}
              disabled={!initialLoaded}
              onChange={(v) =>
                setPreferences({
                  ...preferences,
                  math_affect: v as MathAffect,
                })
              }
            />
            <PreferenceSelect
              id="example_flavor"
              namespace="onboarding.personality.q3"
              value={preferences.example_flavor}
              options={[
                { value: "story", labelKey: "story" },
                { value: "pure", labelKey: "pure" },
                { value: "visual", labelKey: "visual" },
              ]}
              disabled={!initialLoaded}
              onChange={(v) =>
                setPreferences({
                  ...preferences,
                  example_flavor: v as ExampleFlavor,
                })
              }
            />

            <div className="rounded-md border border-dashed border-border px-3 py-2 text-xs text-muted-foreground">
              <Link
                href="/dashboard/onboarding"
                className="text-primary underline underline-offset-2 hover:no-underline"
              >
                Re-take the quick check
              </Link>
              {" "}— go through the placement quiz again to recalibrate the tutor.
            </div>
          </CardContent>
        </Card>

        {/* Privacy -------------------------------------------------------- */}
        <Card>
          <CardHeader>
            <CardTitle>{t("sectionPrivacy")}</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            <label className="flex items-start gap-3 text-sm">
              <input
                type="checkbox"
                className="mt-1 h-4 w-4"
                checked={shareProgress}
                onChange={(e) => setShareProgress(e.target.checked)}
                disabled={!initialLoaded}
              />
              <span className="flex flex-col gap-1">
                <span className="font-medium text-foreground">
                  {t("shareProgressLabel")}
                </span>
                <span className="text-xs text-muted-foreground">
                  {t("shareProgressHelp")}
                </span>
              </span>
            </label>
          </CardContent>
        </Card>

        {/* Save row ------------------------------------------------------- */}
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:gap-4">
          <Button type="submit" disabled={saving || !initialLoaded}>
            {saving ? t("saving") : t("save")}
          </Button>
          {status && (
            // StatusMessage has a default `mt-4` for stacking under a form
            // field; override to 0 so it aligns vertically with the button.
            <StatusMessage type={status.type} className="mt-0">
              {status.message}
            </StatusMessage>
          )}
        </div>
      </form>
    </div>
  );
}

interface FieldProps {
  id: string;
  label: string;
  help?: string;
  children: React.ReactNode;
}

function Field({ id, label, help, children }: FieldProps) {
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={id}>{label}</Label>
      {children}
      {help && <p className="text-xs text-muted-foreground">{help}</p>}
    </div>
  );
}

interface PreferenceSelectProps {
  id: string;
  namespace: string;
  value: string;
  options: { value: string; labelKey: string }[];
  disabled?: boolean;
  onChange: (value: string) => void;
}

/**
 * A radio group bound to the same i18n keys the onboarding flow uses,
 * so the same labels appear on the survey and in Settings without
 * duplicating strings.
 */
function PreferenceSelect({
  id,
  namespace,
  value,
  options,
  disabled,
  onChange,
}: PreferenceSelectProps) {
  const t = useTranslations(namespace);
  return (
    <fieldset className="flex flex-col gap-2">
      <legend className="text-sm font-medium text-foreground">
        {t("prompt")}
      </legend>
      <div className="flex flex-col gap-2">
        {options.map((opt) => {
          const selected = value === opt.value;
          return (
            <label
              key={`${id}-${opt.value}`}
              className={
                "flex cursor-pointer items-center gap-3 rounded-md border px-3 py-2 text-sm transition-colors " +
                (selected
                  ? "border-primary bg-primary/5 text-foreground"
                  : "border-border hover:border-foreground/30") +
                (disabled ? " opacity-60" : "")
              }
            >
              <input
                type="radio"
                name={id}
                value={opt.value}
                checked={selected}
                onChange={() => onChange(opt.value)}
                disabled={disabled}
                className="h-3.5 w-3.5"
              />
              <span>{t(opt.labelKey)}</span>
            </label>
          );
        })}
      </div>
    </fieldset>
  );
}
