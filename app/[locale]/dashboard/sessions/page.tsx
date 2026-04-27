"use client";

import { useEffect, useState } from "react";
import { useTranslations, useFormatter } from "next-intl";
import {
  MessageSquare,
  MessageSquarePlus,
  Sparkles,
  Trash2
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { StatusMessage } from "@/components/ui/status-message";
import { Link, useRouter } from "@/i18n/navigation";
import { ApiError } from "@/lib/api/config";
import {
  createSession,
  deleteSession,
  listSessions
} from "@/lib/api/sessions";
import { getSupabaseBrowserClient } from "@/lib/supabase/client";
import type { TutorSession } from "@/lib/api/types";
import { cn } from "@/lib/utils";

export default function SessionsPage() {
  const t = useTranslations("sessions");
  const format = useFormatter();
  const router = useRouter();
  const supabase = getSupabaseBrowserClient();

  const [sessions, setSessions] = useState<TutorSession[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const data = await listSessions();
        if (cancelled) return;
        setSessions(data);

        // First-run nudge: a brand-new account has no sessions and no
        // personality preferences yet. Send them through onboarding so
        // their first chat already feels personalized. Users who have
        // any prior session (even if they later deleted preferences)
        // are NOT redirected — that would be annoying.
        if (data.length === 0) {
          const { data: auth } = await supabase.auth.getUser();
          if (cancelled || !auth.user) return;
          const { data: profile } = await supabase
            .from("profiles")
            .select("preferences")
            .eq("id", auth.user.id)
            .maybeSingle();
          if (cancelled) return;
          const prefs = (profile?.preferences ?? {}) as Record<string, unknown>;
          const hasAnyPref = Boolean(
            prefs.hint_style || prefs.math_affect || prefs.example_flavor,
          );
          if (!hasAnyPref) {
            router.replace("/dashboard/onboarding");
          }
        }
      } catch (e) {
        if (!cancelled) {
          setError(e instanceof ApiError ? e.detail : t("loadError"));
          setSessions([]);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [t, supabase, router]);

  async function handleCreate() {
    setCreating(true);
    setError(null);
    try {
      const created = await createSession();
      router.push(`/dashboard/sessions/${created.id}`);
    } catch (e) {
      setCreating(false);
      setError(e instanceof ApiError ? e.detail : t("createError"));
    }
  }

  async function handleDelete(id: string) {
    if (!window.confirm(t("deleteConfirm"))) return;
    setDeletingId(id);
    setError(null);
    try {
      await deleteSession(id);
      setSessions((prev) => (prev ?? []).filter((s) => s.id !== id));
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : t("deleteError"));
    } finally {
      setDeletingId(null);
    }
  }

  return (
    <div className="flex w-full flex-col gap-8 px-6 py-10 sm:px-10 sm:py-14 lg:px-14">
      <header className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-3xl font-serif text-foreground">
            {t("title")}
          </h1>
          <p className="mt-2 text-sm text-muted-foreground">
            {t("description")}
          </p>
        </div>
        <Button
          type="button"
          onClick={handleCreate}
          disabled={creating}
          className="shrink-0"
        >
          <MessageSquarePlus className="h-4 w-4" />
          {creating ? t("creating") : t("newSession")}
        </Button>
      </header>

      {error && <StatusMessage type="error">{error}</StatusMessage>}

      {sessions === null ? (
        <p className="text-sm text-muted-foreground">{t("loading")}</p>
      ) : sessions.length === 0 ? (
        <EmptyState />
      ) : (
        <ul className="flex flex-col gap-2 max-w-3xl">
          {sessions.map((session) => (
            <li key={session.id}>
              <SessionRow
                session={session}
                deleting={deletingId === session.id}
                onDelete={() => handleDelete(session.id)}
                formattedDate={format.dateTime(new Date(session.updated_at), {
                  dateStyle: "medium",
                  timeStyle: "short"
                })}
                t={{
                  untitled: t("untitled"),
                  delete: t("delete")
                }}
              />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function EmptyState() {
  const t = useTranslations("sessions");
  return (
    <div className="flex flex-col items-center justify-center gap-4 rounded-xl border border-dashed border-border bg-card/40 px-6 py-16 text-center">
      <div className="flex h-12 w-12 items-center justify-center rounded-full bg-primary/10 text-primary">
        <Sparkles className="h-5 w-5" aria-hidden />
      </div>
      <div className="max-w-md space-y-1.5">
        <h2 className="text-xl font-serif text-foreground">
          {t("emptyTitle")}
        </h2>
        <p className="text-sm text-muted-foreground leading-relaxed">
          {t("emptyDescription")}
        </p>
      </div>
    </div>
  );
}

interface SessionRowProps {
  session: TutorSession;
  deleting: boolean;
  onDelete: () => void;
  formattedDate: string;
  t: { untitled: string; delete: string };
}

function SessionRow({
  session,
  deleting,
  onDelete,
  formattedDate,
  t
}: SessionRowProps) {
  const title = session.title?.trim() || t.untitled;

  return (
    <div
      className={cn(
        "group flex items-center justify-between gap-3 rounded-lg border border-border bg-card px-4 py-3 transition-colors duration-200 ease-brand hover:bg-accent/40",
        deleting && "opacity-60 pointer-events-none"
      )}
    >
      <Link
        href={`/dashboard/sessions/${session.id}`}
        className="flex min-w-0 flex-1 items-center gap-3"
      >
        <MessageSquare
          className="h-4 w-4 shrink-0 text-muted-foreground"
          aria-hidden
        />
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-medium text-foreground">
            {title}
          </p>
          <p className="truncate text-xs text-muted-foreground">
            {formattedDate}
          </p>
        </div>
      </Link>
      <Button
        type="button"
        variant="ghost"
        size="icon"
        onClick={onDelete}
        disabled={deleting}
        aria-label={t.delete}
        className="text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100 focus-visible:opacity-100"
      >
        <Trash2 className="h-4 w-4" />
      </Button>
    </div>
  );
}
