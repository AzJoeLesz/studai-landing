"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";

import { Logo } from "@/components/brand/logo";
import { Link, useRouter } from "@/i18n/navigation";
import { getSupabaseBrowserClient } from "@/lib/supabase/client";

/**
 * Phase 10C — admin shell.
 *
 * Client-side role gate (mirrors the dashboard layout pattern). The
 * authoritative gate is server-side: every /admin/* backend endpoint
 * checks `profiles.role == 'admin'` and 403s otherwise. This client
 * gate is just for UX -- non-admins see a redirect, not a flash of
 * admin chrome followed by an API error.
 *
 * To grant yourself admin: in the Supabase SQL editor, run
 *   update profiles set role = 'admin' where id = '<your-user-uuid>';
 *
 * The dashboard sidebar does NOT link to /admin -- it's intentionally
 * unlisted in the regular navigation so non-admins don't get a
 * confusing dead-end link.
 */
export default function AdminLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const t = useTranslations("admin");
  const supabase = getSupabaseBrowserClient();
  const router = useRouter();
  const [status, setStatus] = useState<"checking" | "ok" | "denied">(
    "checking",
  );

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const { data: auth } = await supabase.auth.getUser();
      if (cancelled) return;
      if (!auth.user) {
        router.replace("/");
        return;
      }
      const { data: profile } = await supabase
        .from("profiles")
        .select("role")
        .eq("id", auth.user.id)
        .maybeSingle();
      if (cancelled) return;
      if (profile?.role !== "admin") {
        // No flash, no toast -- just send them back to the dashboard.
        router.replace("/dashboard/sessions");
        setStatus("denied");
        return;
      }
      setStatus("ok");
    })();
    return () => {
      cancelled = true;
    };
  }, [supabase, router]);

  if (status === "checking" || status === "denied") {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Logo size="md" className="animate-pulse opacity-40" />
      </div>
    );
  }

  return (
    <div className="flex min-h-screen flex-col">
      <header className="flex items-center gap-4 border-b border-border bg-background/95 px-4 py-3 backdrop-blur sm:px-6">
        <Link
          href="/dashboard/sessions"
          className="text-sm font-medium text-muted-foreground hover:text-foreground"
        >
          ← {t("backToDashboard")}
        </Link>
        <span className="text-sm font-semibold text-foreground">
          {t("title")}
        </span>
      </header>
      <main className="flex-1 overflow-x-hidden">{children}</main>
    </div>
  );
}
