"use client";

import { useTranslations } from "next-intl";
import { MessageSquarePlus, Sparkles } from "lucide-react";

import { Button } from "@/components/ui/button";

export default function SessionsPage() {
  const t = useTranslations("sessions");

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-8 px-6 py-10 sm:px-10 sm:py-14">
      <header className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-3xl font-serif tracking-tight text-foreground">
            {t("title")}
          </h1>
          <p className="mt-2 text-sm text-muted-foreground">
            {t("description")}
          </p>
        </div>
        <Button type="button" disabled className="shrink-0">
          <MessageSquarePlus className="h-4 w-4" />
          {t("newSession")}
        </Button>
      </header>

      <div className="flex flex-col items-center justify-center gap-4 rounded-xl border border-dashed border-border bg-card/40 px-6 py-16 text-center">
        <div className="flex h-12 w-12 items-center justify-center rounded-full bg-primary/10 text-primary">
          <Sparkles className="h-5 w-5" aria-hidden />
        </div>
        <div className="max-w-md space-y-1.5">
          <h2 className="text-xl font-serif tracking-tight text-foreground">
            {t("emptyTitle")}
          </h2>
          <p className="text-sm text-muted-foreground leading-relaxed">
            {t("emptyDescription")}
          </p>
        </div>
      </div>
    </div>
  );
}
