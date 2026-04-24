"use client";

import { useEffect, useState, type FormEvent } from "react";
import { motion } from "framer-motion";
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
import { Logo } from "@/components/brand/logo";
import { LanguageSwitcher } from "@/components/language-switcher";
import { getSupabaseBrowserClient } from "@/lib/supabase/client";
import { useRouter } from "@/i18n/navigation";

type StatusType = "info" | "success" | "error";

interface Status {
  message: string;
  type: StatusType;
}

const fadeTransition = { duration: 0.25, ease: [0.22, 1, 0.36, 1] as const };

const POST_LOGIN_PATH = "/dashboard/sessions";

export default function Home() {
  const t = useTranslations();
  const supabase = getSupabaseBrowserClient();
  const router = useRouter();

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [status, setStatus] = useState<Status | null>(null);
  const [loadingAction, setLoadingAction] = useState<string | null>(null);
  const [checkingSession, setCheckingSession] = useState(true);

  function showStatus(message: string, type: StatusType = "info") {
    setStatus({ message, type });
    if (type !== "info") {
      window.setTimeout(() => setStatus(null), 3500);
    }
  }

  useEffect(() => {
    let cancelled = false;

    supabase.auth.getUser().then(({ data }) => {
      if (cancelled) return;
      if (data.user) {
        router.replace(POST_LOGIN_PATH);
      } else {
        setCheckingSession(false);
      }
    });

    const { data: listener } = supabase.auth.onAuthStateChange(
      (_event, session) => {
        if (session?.user) {
          router.replace(POST_LOGIN_PATH);
        }
      }
    );

    return () => {
      cancelled = true;
      listener.subscription.unsubscribe();
    };
  }, [supabase, router]);

  async function signIn(e: FormEvent) {
    e.preventDefault();
    setLoadingAction("signIn");
    showStatus(t("auth.status.signingIn"), "info");

    const { error } = await supabase.auth.signInWithPassword({
      email,
      password
    });

    if (error) {
      setLoadingAction(null);
      showStatus(t("auth.status.invalidCredentials"), "error");
      return;
    }

    showStatus(t("auth.status.welcomeBack"), "success");
    // onAuthStateChange will redirect us.
  }

  async function signUp() {
    setLoadingAction("signUp");
    showStatus(t("auth.status.signingUp"), "info");

    const { error } = await supabase.auth.signUp({
      email,
      password,
      options: { emailRedirectTo: "https://studai.hu" }
    });

    setLoadingAction(null);
    showStatus(
      error ? error.message : t("auth.status.accountCreated"),
      error ? "error" : "success"
    );
  }

  async function signInWithGoogle() {
    setLoadingAction("google");
    showStatus(t("auth.status.googleOpening"), "info");

    const { error } = await supabase.auth.signInWithOAuth({
      provider: "google",
      options: { redirectTo: "https://studai.hu" }
    });

    if (error) {
      setLoadingAction(null);
      showStatus(error.message, "error");
    }
  }

  if (checkingSession) {
    return (
      <main className="flex min-h-screen items-center justify-center">
        <Logo size="md" className="animate-pulse opacity-40" />
      </main>
    );
  }

  return (
    <main className="min-h-screen">
      <header className="flex w-full items-center justify-between px-6 py-6 sm:px-8 lg:px-12">
        <Logo size="md" />
        <LanguageSwitcher />
      </header>

      <div className="mx-auto flex w-full max-w-md flex-col items-stretch px-6 pb-16 pt-8 sm:pt-16">
        <motion.div
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          transition={fadeTransition}
        >
          <Card>
            <CardHeader>
              <CardTitle>{t("auth.title")}</CardTitle>
              <CardDescription>{t("auth.description")}</CardDescription>
            </CardHeader>

            <CardContent>
              <form onSubmit={signIn} className="flex flex-col gap-4">
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor="email">{t("auth.emailLabel")}</Label>
                  <Input
                    id="email"
                    type="email"
                    autoComplete="email"
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    required
                  />
                </div>

                <div className="flex flex-col gap-1.5">
                  <Label htmlFor="password">{t("auth.passwordLabel")}</Label>
                  <Input
                    id="password"
                    type="password"
                    autoComplete="current-password"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    required
                  />
                </div>

                <Button
                  type="submit"
                  disabled={loadingAction === "signIn"}
                  className="mt-2"
                >
                  {loadingAction === "signIn"
                    ? t("auth.signingIn")
                    : t("auth.signIn")}
                </Button>

                <Button
                  type="button"
                  variant="outline"
                  onClick={signUp}
                  disabled={loadingAction === "signUp"}
                >
                  {loadingAction === "signUp"
                    ? t("auth.signingUp")
                    : t("auth.signUp")}
                </Button>

                <div className="my-1 flex items-center gap-3">
                  <span className="h-px flex-1 bg-border" />
                  <span className="text-xs uppercase tracking-wide text-muted-foreground">
                    {t("auth.or")}
                  </span>
                  <span className="h-px flex-1 bg-border" />
                </div>

                <Button
                  type="button"
                  variant="outline"
                  onClick={signInWithGoogle}
                  disabled={loadingAction === "google"}
                >
                  {t("auth.google")}
                </Button>
              </form>

              {status && (
                <StatusMessage type={status.type}>
                  {status.message}
                </StatusMessage>
              )}
            </CardContent>
          </Card>
        </motion.div>

        <p className="mt-8 text-center text-xs text-muted-foreground">
          {t("footer.soon")}
        </p>
      </div>
    </main>
  );
}
