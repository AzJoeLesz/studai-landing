import { defineRouting } from "next-intl/routing";

/**
 * Single source of truth for which languages StudAI supports and
 * how they show up in the URL.
 *
 * To add a language later:
 *   1. Add its code here
 *   2. Add messages/<code>.json
 *   3. Add a readable label in components/language-switcher.tsx
 */
export const routing = defineRouting({
  locales: ["hu", "en"],
  defaultLocale: "hu",
  localePrefix: "always"
});

export type Locale = (typeof routing.locales)[number];
