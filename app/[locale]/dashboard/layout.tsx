"use client";

import { useEffect, useState } from "react";

import { Sidebar } from "@/components/dashboard/sidebar";
import { Logo } from "@/components/brand/logo";
import { getSupabaseBrowserClient } from "@/lib/supabase/client";
import { useRouter } from "@/i18n/navigation";

interface AuthedUser {
  id: string;
  email?: string;
  displayName?: string;
}

/**
 * Dashboard shell.
 *
 * Client-side auth guard for now. When someone hits a /dashboard/* route:
 *   1. We ask Supabase who they are.
 *   2. If no user → redirect to "/".
 *   3. If there is a user → load their display_name and render the shell.
 *
 * Keeping this on the client is simpler than @supabase/ssr for now and means
 * we don't need server-side Supabase plumbing yet. The brief loading state
 * below is the tradeoff. We'll revisit once the Python backend forces the
 * token-refresh question.
 */
export default function DashboardLayout({
  children
}: {
  children: React.ReactNode;
}) {
  const supabase = getSupabaseBrowserClient();
  const router = useRouter();
  const [user, setUser] = useState<AuthedUser | null>(null);
  const [checking, setChecking] = useState(true);

  useEffect(() => {
    let cancelled = false;

    async function check() {
      const { data } = await supabase.auth.getUser();
      if (cancelled) return;

      if (!data.user) {
        router.replace("/");
        return;
      }

      const { data: profile } = await supabase
        .from("profiles")
        .select("display_name")
        .eq("id", data.user.id)
        .maybeSingle();

      if (cancelled) return;

      setUser({
        id: data.user.id,
        email: data.user.email ?? undefined,
        displayName: profile?.display_name ?? undefined
      });
      setChecking(false);
    }

    check();

    const { data: listener } = supabase.auth.onAuthStateChange(
      (_event, session) => {
        if (!session?.user) {
          router.replace("/");
        }
      }
    );

    return () => {
      cancelled = true;
      listener.subscription.unsubscribe();
    };
  }, [supabase, router]);

  if (checking || !user) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Logo size="md" className="animate-pulse opacity-40" />
      </div>
    );
  }

  return (
    <div className="flex min-h-screen flex-col md:flex-row">
      <Sidebar user={{ email: user.email, displayName: user.displayName }} />
      <main className="flex-1 overflow-x-hidden">{children}</main>
    </div>
  );
}
