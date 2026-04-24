"use client";

import { useTransition } from "react";
import { useLocale } from "next-intl";
import { Globe } from "lucide-react";

import { cn } from "@/lib/utils";
import { usePathname, useRouter } from "@/i18n/navigation";
import { routing, type Locale } from "@/i18n/routing";

interface LanguageSwitcherProps {
  className?: string;
}

const LOCALE_LABELS: Record<Locale, string> = {
  hu: "HU",
  en: "EN"
};

/**
 * Minimal locale toggle for the header.
 *
 * - Writes the choice to the URL (/hu/... vs /en/...) — single source of truth.
 * - Uses next-intl's locale-aware router so the current path is preserved.
 * - Later, when we have a signed-in user, we'll mirror the choice into
 *   profiles.preferred_language so it sticks across devices.
 */
export function LanguageSwitcher({ className }: LanguageSwitcherProps) {
  const locale = useLocale() as Locale;
  const pathname = usePathname();
  const router = useRouter();
  const [isPending, startTransition] = useTransition();

  function switchTo(next: Locale) {
    if (next === locale) return;
    startTransition(() => {
      router.replace(pathname, { locale: next });
    });
  }

  return (
    <div
      className={cn(
        "inline-flex items-center gap-1 rounded-md border border-border bg-card/60 p-0.5 text-xs font-medium",
        isPending && "opacity-60",
        className
      )}
      aria-label="Language"
    >
      <Globe className="ml-1.5 h-3.5 w-3.5 text-muted-foreground" aria-hidden />
      {routing.locales.map((loc) => {
        const isActive = loc === locale;
        return (
          <button
            key={loc}
            type="button"
            onClick={() => switchTo(loc)}
            className={cn(
              "rounded-sm px-2 py-1 transition-colors duration-200 ease-brand",
              isActive
                ? "bg-primary text-primary-foreground"
                : "text-muted-foreground hover:text-foreground"
            )}
            aria-pressed={isActive}
          >
            {LOCALE_LABELS[loc]}
          </button>
        );
      })}
    </div>
  );
}
