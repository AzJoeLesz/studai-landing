"use client";

import { useEffect, useState, type FormEvent } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { LogOut } from "lucide-react";

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
import { getSupabaseBrowserClient } from "@/lib/supabase/client";

type StatusType = "info" | "success" | "error";

interface Status {
  message: string;
  type: StatusType;
}

interface Profile {
  first_name: string;
  last_name: string;
  display_name: string;
}

const emptyProfile: Profile = {
  first_name: "",
  last_name: "",
  display_name: ""
};

const fadeTransition = { duration: 0.25, ease: [0.22, 1, 0.36, 1] as const };

export default function Home() {
  const supabase = getSupabaseBrowserClient();

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [status, setStatus] = useState<Status | null>(null);
  const [loadingAction, setLoadingAction] = useState<string | null>(null);

  const [user, setUser] = useState<{ id: string; email?: string } | null>(null);
  const [profile, setProfile] = useState<Profile>(emptyProfile);
  const [ready, setReady] = useState(false);

  function showStatus(message: string, type: StatusType = "info") {
    setStatus({ message, type });
    if (type !== "info") {
      window.setTimeout(() => setStatus(null), 3500);
    }
  }

  async function loadProfile(userId: string) {
    const { data, error } = await supabase
      .from("profiles")
      .select("first_name, last_name, display_name")
      .eq("id", userId)
      .single();

    if (error || !data) {
      setProfile(emptyProfile);
      return;
    }

    setProfile({
      first_name: data.first_name ?? "",
      last_name: data.last_name ?? "",
      display_name: data.display_name ?? ""
    });
  }

  useEffect(() => {
    supabase.auth.getUser().then(({ data }) => {
      const currentUser = data.user;
      if (currentUser) {
        setUser({ id: currentUser.id, email: currentUser.email ?? undefined });
        loadProfile(currentUser.id);
      }
      setReady(true);
    });

    const { data: listener } = supabase.auth.onAuthStateChange(
      (_event, session) => {
        const currentUser = session?.user ?? null;
        if (currentUser) {
          setUser({ id: currentUser.id, email: currentUser.email ?? undefined });
          loadProfile(currentUser.id);
        } else {
          setUser(null);
          setProfile(emptyProfile);
        }
      }
    );

    return () => {
      listener.subscription.unsubscribe();
    };
  }, [supabase]);

  async function signIn(e: FormEvent) {
    e.preventDefault();
    setLoadingAction("signIn");
    showStatus("Bejelentkezés...", "info");

    const { error } = await supabase.auth.signInWithPassword({
      email,
      password
    });

    setLoadingAction(null);
    showStatus(
      error ? "Hibás e-mail cím vagy jelszó." : "Üdv újra itt.",
      error ? "error" : "success"
    );
  }

  async function signUp() {
    setLoadingAction("signUp");
    showStatus("Fiók létrehozása...", "info");

    const { error } = await supabase.auth.signUp({
      email,
      password,
      options: { emailRedirectTo: "https://studai.hu" }
    });

    setLoadingAction(null);
    showStatus(
      error
        ? error.message
        : "Fiók létrehozva. Kérlek erősítsd meg az e-mail címed.",
      error ? "error" : "success"
    );
  }

  async function signInWithGoogle() {
    setLoadingAction("google");
    showStatus("Google bejelentkezés megnyitása...", "info");

    const { error } = await supabase.auth.signInWithOAuth({
      provider: "google",
      options: { redirectTo: "https://studai.hu" }
    });

    if (error) {
      setLoadingAction(null);
      showStatus(error.message, "error");
    }
  }

  async function saveProfile() {
    if (!user) return;
    setLoadingAction("save");

    const { error } = await supabase.from("profiles").upsert({
      id: user.id,
      first_name: profile.first_name,
      last_name: profile.last_name,
      display_name: profile.display_name
    });

    setLoadingAction(null);
    showStatus(
      error ? error.message : "Profil elmentve.",
      error ? "error" : "success"
    );
  }

  async function signOut() {
    setLoadingAction("signOut");
    await supabase.auth.signOut();
    setLoadingAction(null);
    showStatus("Sikeres kijelentkezés.", "info");
  }

  if (!ready) {
    return (
      <main className="flex min-h-screen items-center justify-center">
        <Logo size="md" className="opacity-40" />
      </main>
    );
  }

  return (
    <main className="min-h-screen">
      <header className="mx-auto flex w-full max-w-5xl items-center justify-between px-6 py-6">
        <Logo size="sm" />
      </header>

      <div className="mx-auto flex w-full max-w-md flex-col items-stretch px-6 pb-16 pt-8 sm:pt-16">
        <AnimatePresence mode="wait">
          {user ? (
            <motion.div
              key="dashboard"
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -4 }}
              transition={fadeTransition}
            >
              <Card>
                <CardHeader>
                  <CardTitle>Üdv újra, {profile.display_name || "tanuló"}.</CardTitle>
                  <CardDescription>
                    Hamarosan innen érhető el a munkamenetek listája és a beállítások.
                    Addig is frissítheted az alapadataidat.
                  </CardDescription>
                </CardHeader>

                <CardContent className="flex flex-col gap-4">
                  <div className="rounded-md border border-border bg-muted/50 px-3.5 py-3 text-sm text-muted-foreground">
                    <span className="text-foreground/60">E-mail: </span>
                    <span className="text-foreground">{user.email}</span>
                  </div>

                  <div className="flex flex-col gap-1.5">
                    <Label htmlFor="first_name">Keresztnév</Label>
                    <Input
                      id="first_name"
                      value={profile.first_name}
                      onChange={(e) =>
                        setProfile({ ...profile, first_name: e.target.value })
                      }
                    />
                  </div>

                  <div className="flex flex-col gap-1.5">
                    <Label htmlFor="last_name">Vezetéknév</Label>
                    <Input
                      id="last_name"
                      value={profile.last_name}
                      onChange={(e) =>
                        setProfile({ ...profile, last_name: e.target.value })
                      }
                    />
                  </div>

                  <div className="flex flex-col gap-1.5">
                    <Label htmlFor="display_name">Megjelenítendő név</Label>
                    <Input
                      id="display_name"
                      value={profile.display_name}
                      onChange={(e) =>
                        setProfile({ ...profile, display_name: e.target.value })
                      }
                    />
                  </div>

                  <Button
                    onClick={saveProfile}
                    disabled={loadingAction === "save"}
                    className="mt-2"
                  >
                    {loadingAction === "save" ? "Mentés..." : "Profil mentése"}
                  </Button>

                  <Button
                    type="button"
                    variant="ghost"
                    onClick={signOut}
                    disabled={loadingAction === "signOut"}
                    className="text-muted-foreground"
                  >
                    <LogOut className="h-4 w-4" />
                    Kijelentkezés
                  </Button>

                  {status && (
                    <StatusMessage type={status.type}>
                      {status.message}
                    </StatusMessage>
                  )}
                </CardContent>
              </Card>
            </motion.div>
          ) : (
            <motion.div
              key="auth"
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -4 }}
              transition={fadeTransition}
            >
              <Card>
                <CardHeader>
                  <CardTitle>Lépj be a folytatáshoz</CardTitle>
                  <CardDescription>
                    Korai hozzáférés az AI matek korrepetitor prototípushoz.
                  </CardDescription>
                </CardHeader>

                <CardContent>
                  <form onSubmit={signIn} className="flex flex-col gap-4">
                    <div className="flex flex-col gap-1.5">
                      <Label htmlFor="email">E-mail cím</Label>
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
                      <Label htmlFor="password">Jelszó</Label>
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
                      {loadingAction === "signIn" ? "Bejelentkezés..." : "Bejelentkezés"}
                    </Button>

                    <Button
                      type="button"
                      variant="outline"
                      onClick={signUp}
                      disabled={loadingAction === "signUp"}
                    >
                      {loadingAction === "signUp"
                        ? "Fiók létrehozása..."
                        : "Fiók létrehozása"}
                    </Button>

                    <div className="my-1 flex items-center gap-3">
                      <span className="h-px flex-1 bg-border" />
                      <span className="text-xs uppercase tracking-wide text-muted-foreground">
                        vagy
                      </span>
                      <span className="h-px flex-1 bg-border" />
                    </div>

                    <Button
                      type="button"
                      variant="outline"
                      onClick={signInWithGoogle}
                      disabled={loadingAction === "google"}
                    >
                      Folytatás Google-fiókkal
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
          )}
        </AnimatePresence>

        <p className="mt-8 text-center text-xs text-muted-foreground">
          A StudAI hamarosan többet is tud majd — munkamenetek, beállítások és egy
          nyugodt, türelmes AI matek korrepetitor.
        </p>
      </div>
    </main>
  );
}
