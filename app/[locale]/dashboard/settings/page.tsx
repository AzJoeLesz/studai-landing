"use client";

import { useEffect, useState, type FormEvent } from "react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle
} from "@/components/ui/card";
import { StatusMessage } from "@/components/ui/status-message";
import { getSupabaseBrowserClient } from "@/lib/supabase/client";

type StatusType = "info" | "success" | "error";

interface Status {
  message: string;
  type: StatusType;
}

/**
 * Profile / settings page.
 *
 * Reads and writes `public.profiles` directly via the Supabase JS client
 * (no Python backend involvement). RLS policies enforce that a user can
 * only touch their own row.
 *
 * Fields beyond display_name landed in Phase 9 to power the student
 * model: age, grade level, interests, learning goals, free-form notes.
 * The Python tutor agent loads this on every chat turn and weaves it
 * into the system context.
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
          "display_name, age, grade_level, interests, learning_goals, notes"
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
      notes: notes.trim() || null
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

        {/* Save row ------------------------------------------------------- */}
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
          <Button
            type="submit"
            disabled={saving || !initialLoaded}
            className="self-start"
          >
            {saving ? t("saving") : t("save")}
          </Button>
          {status && (
            <StatusMessage type={status.type}>{status.message}</StatusMessage>
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
