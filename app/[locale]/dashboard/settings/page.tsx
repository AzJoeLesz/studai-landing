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

export default function SettingsPage() {
  const t = useTranslations("settings");
  const supabase = getSupabaseBrowserClient();

  const [displayName, setDisplayName] = useState("");
  const [initialLoaded, setInitialLoaded] = useState(false);
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState<Status | null>(null);

  useEffect(() => {
    async function load() {
      const { data: auth } = await supabase.auth.getUser();
      if (!auth.user) return;

      const { data, error } = await supabase
        .from("profiles")
        .select("display_name")
        .eq("id", auth.user.id)
        .maybeSingle();

      if (error) {
        setStatus({ message: t("loadError"), type: "error" });
      } else {
        setDisplayName(data?.display_name ?? "");
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

    const { error } = await supabase.from("profiles").upsert({
      id: auth.user.id,
      display_name: displayName
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
        <h1 className="text-3xl font-serif text-foreground">
          {t("title")}
        </h1>
      </header>

      <Card className="max-w-2xl">
        <CardHeader>
          <CardTitle>{t("displayNameLabel")}</CardTitle>
          <CardDescription>{t("description")}</CardDescription>
        </CardHeader>

        <CardContent>
          <form onSubmit={handleSave} className="flex flex-col gap-4">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="display_name">{t("displayNameLabel")}</Label>
              <Input
                id="display_name"
                value={displayName}
                placeholder={t("displayNamePlaceholder")}
                onChange={(e) => setDisplayName(e.target.value)}
                disabled={!initialLoaded}
                maxLength={80}
              />
            </div>

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
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
