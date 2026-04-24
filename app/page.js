"use client";

import { useEffect, useState } from "react";
import { createClient } from "@supabase/supabase-js";

const supabase = createClient(
  process.env.NEXT_PUBLIC_SUPABASE_URL,
  process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY
);

export default function Home() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");

  const [message, setMessage] = useState("");
  const [messageType, setMessageType] = useState("info");

  const [visibleUser, setVisibleUser] = useState(null);
  const [isTransitioning, setIsTransitioning] = useState(false);

  const [profile, setProfile] = useState({
    first_name: "",
    last_name: "",
    display_name: ""
  });

  function showMessage(text, type = "info") {
    setMessage(text);
    setMessageType(type);

    setTimeout(() => {
      setMessage("");
    }, 3500);
  }

  async function loadProfile(userId) {
    const { data, error } = await supabase
      .from("profiles")
      .select("first_name, last_name, display_name")
      .eq("id", userId)
      .single();

    if (!error && data) {
      setProfile({
        first_name: data.first_name || "",
        last_name: data.last_name || "",
        display_name: data.display_name || ""
      });
    }
  }

  useEffect(() => {
    supabase.auth.getUser().then(({ data }) => {
      setVisibleUser(data.user);
      if (data.user) loadProfile(data.user.id);
    });

    const { data: listener } = supabase.auth.onAuthStateChange(
      (_event, session) => {
        setIsTransitioning(true);

        setTimeout(() => {
          setVisibleUser(session?.user ?? null);
          if (session?.user) loadProfile(session.user.id);
          setIsTransitioning(false);
        }, 250);
      }
    );

    return () => {
      listener.subscription.unsubscribe();
    };
  }, []);

  async function signUp() {
    showMessage("Creating your account...", "info");

    const { error } = await supabase.auth.signUp({
      email,
      password,
      options: {
        emailRedirectTo: "https://studai.hu"
      }
    });

    showMessage(
      error ? error.message : "Account created. Check your email to confirm it.",
      error ? "error" : "success"
    );
  }

  async function signIn() {
    showMessage("Signing you in...", "info");

    const { error } = await supabase.auth.signInWithPassword({
      email,
      password
    });

    showMessage(error ? "Invalid email or password." : "Welcome back.", error ? "error" : "success");
  }

  async function signInWithGoogle() {
    showMessage("Opening Google sign-in...", "info");

    const { error } = await supabase.auth.signInWithOAuth({
      provider: "google",
      options: {
        redirectTo: "https://studai.hu"
      }
    });

    if (error) showMessage(error.message, "error");
  }

  async function saveProfile() {
    if (!visibleUser) return;

    const { error } = await supabase
      .from("profiles")
      .update({
        first_name: profile.first_name,
        last_name: profile.last_name,
        display_name: profile.display_name
      })
      .eq("id", visibleUser.id);

    showMessage(
      error ? error.message : "Profile saved.",
      error ? "error" : "success"
    );
  }

  async function signOut() {
    await supabase.auth.signOut();
    setProfile({ first_name: "", last_name: "", display_name: "" });
    showMessage("Signed out successfully.", "info");
  }

  if (visibleUser) {
    return (
      <main style={styles.page}>
        <div style={styles.overlay}>
          <section
            style={{
              ...styles.dashboard,
              opacity: isTransitioning ? 0 : 1,
              transform: isTransitioning ? "translateY(10px)" : "translateY(0)"
            }}
          >
            <div style={styles.badge}>Logged in</div>

            <h1 style={styles.logo}>
              Stud<span style={{ color: "#8b5cf6" }}>AI</span>
            </h1>

            <h2 style={styles.title}>Your profile</h2>

            <p style={styles.text}>
              Add your basic information. Later this will help the tutor personalize explanations.
            </p>

            <div style={styles.panel}>
              <strong>Email:</strong>
              <br />
              {visibleUser.email}
            </div>

            <input
              style={styles.input}
              placeholder="First name"
              value={profile.first_name}
              onChange={(e) =>
                setProfile({ ...profile, first_name: e.target.value })
              }
            />

            <input
              style={styles.input}
              placeholder="Last name"
              value={profile.last_name}
              onChange={(e) =>
                setProfile({ ...profile, last_name: e.target.value })
              }
            />

            <input
              style={styles.input}
              placeholder="Display name"
              value={profile.display_name}
              onChange={(e) =>
                setProfile({ ...profile, display_name: e.target.value })
              }
            />

            <button type="button" style={styles.button} onClick={saveProfile}>
              Save profile
            </button>

            <button type="button" style={styles.secondaryButton} onClick={signOut}>
              Log out
            </button>

            {message && (
              <p style={{ ...styles.message, ...styles[messageType] }}>
                {message}
              </p>
            )}
          </section>
        </div>
      </main>
    );
  }

  return (
    <main style={styles.page}>
      <div style={styles.overlay}>
        <section
          style={{
            ...styles.card,
            opacity: isTransitioning ? 0 : 1,
            transform: isTransitioning ? "translateY(10px)" : "translateY(0)"
          }}
        >
          <div style={styles.badge}>Prototype access</div>

          <h1 style={styles.logo}>
            Stud<span style={{ color: "#8b5cf6" }}>AI</span>
          </h1>

          <h2 style={styles.title}>Sign in to continue</h2>

          <p style={styles.text}>Early access to the AI math tutor prototype.</p>

          <form
            onSubmit={(e) => {
              e.preventDefault();
              signIn();
            }}
          >
            <input
              style={styles.input}
              placeholder="Email address"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />

            <input
              style={styles.input}
              placeholder="Password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />

            <button type="submit" style={styles.button}>
              Log in
            </button>

            <button type="button" style={styles.secondaryButton} onClick={signUp}>
              Create account
            </button>

            <div style={styles.divider}>
              <span style={styles.dividerLine}></span>
              <span style={styles.dividerText}>or</span>
              <span style={styles.dividerLine}></span>
            </div>

            <button type="button" style={styles.googleButton} onClick={signInWithGoogle}>
              Continue with Google
            </button>
          </form>

          {message && (
            <p style={{ ...styles.message, ...styles[messageType] }}>
              {message}
            </p>
          )}
        </section>
      </div>
    </main>
  );
}

const styles = {
  page: {
    minHeight: "100vh",
    backgroundImage: "url('/background.png')",
    backgroundSize: "cover",
    backgroundPosition: "center",
    backgroundRepeat: "no-repeat",
    color: "white",
    fontFamily: "Arial, sans-serif"
  },
  overlay: {
    minHeight: "100vh",
    width: "100%",
    background:
      "linear-gradient(135deg, rgba(5,10,20,0.82), rgba(20,10,45,0.68))",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    padding: "24px"
  },
  card: {
    width: "100%",
    maxWidth: "430px",
    padding: "36px",
    borderRadius: "28px",
    background: "rgba(8, 12, 24, 0.74)",
    backdropFilter: "blur(14px)",
    boxShadow: "0 24px 80px rgba(0,0,0,0.55)",
    border: "1px solid rgba(255,255,255,0.12)",
    transition: "opacity 250ms ease, transform 250ms ease"
  },
  dashboard: {
    width: "100%",
    maxWidth: "560px",
    padding: "42px",
    borderRadius: "30px",
    background: "rgba(8, 12, 24, 0.76)",
    backdropFilter: "blur(14px)",
    boxShadow: "0 24px 80px rgba(0,0,0,0.55)",
    border: "1px solid rgba(255,255,255,0.12)",
    transition: "opacity 250ms ease, transform 250ms ease"
  },
  badge: {
    display: "inline-block",
    padding: "8px 14px",
    borderRadius: "999px",
    background: "rgba(139,92,246,0.18)",
    border: "1px solid rgba(139,92,246,0.45)",
    color: "#ddd6fe",
    fontSize: "14px",
    marginBottom: "18px"
  },
  logo: {
    fontSize: "44px",
    margin: "0 0 14px"
  },
  title: {
    fontSize: "26px",
    margin: "0 0 10px"
  },
  text: {
    color: "#cbd5e1",
    lineHeight: 1.5,
    marginBottom: "24px"
  },
  input: {
    display: "block",
    width: "100%",
    boxSizing: "border-box",
    padding: "15px 16px",
    marginBottom: "13px",
    borderRadius: "14px",
    border: "1px solid rgba(255,255,255,0.18)",
    fontSize: "16px",
    outline: "none"
  },
  button: {
    display: "block",
    width: "100%",
    boxSizing: "border-box",
    padding: "15px",
    marginTop: "8px",
    borderRadius: "14px",
    border: "none",
    background: "linear-gradient(135deg, #7c3aed, #a855f7)",
    color: "white",
    fontSize: "16px",
    fontWeight: "700",
    cursor: "pointer"
  },
  secondaryButton: {
    display: "block",
    width: "100%",
    boxSizing: "border-box",
    padding: "15px",
    marginTop: "12px",
    borderRadius: "14px",
    border: "1px solid rgba(168,85,247,0.8)",
    background: "rgba(255,255,255,0.04)",
    color: "white",
    fontSize: "16px",
    fontWeight: "700",
    cursor: "pointer"
  },
  googleButton: {
    display: "block",
    width: "100%",
    boxSizing: "border-box",
    padding: "15px",
    marginTop: "12px",
    borderRadius: "14px",
    border: "1px solid rgba(255,255,255,0.2)",
    background: "white",
    color: "#111827",
    fontSize: "16px",
    fontWeight: "700",
    cursor: "pointer"
  },
  divider: {
    display: "flex",
    alignItems: "center",
    gap: "12px",
    margin: "18px 0 6px"
  },
  dividerLine: {
    flex: 1,
    height: "1px",
    background: "rgba(255,255,255,0.18)"
  },
  dividerText: {
    color: "#94a3b8",
    fontSize: "14px"
  },
  message: {
    marginTop: "18px",
    padding: "12px 14px",
    borderRadius: "14px",
    lineHeight: 1.4,
    fontSize: "14px"
  },
  success: {
    color: "#bbf7d0",
    background: "rgba(22, 163, 74, 0.18)",
    border: "1px solid rgba(34, 197, 94, 0.35)"
  },
  error: {
    color: "#fecaca",
    background: "rgba(220, 38, 38, 0.18)",
    border: "1px solid rgba(248, 113, 113, 0.35)"
  },
  info: {
    color: "#dbeafe",
    background: "rgba(37, 99, 235, 0.16)",
    border: "1px solid rgba(96, 165, 250, 0.35)"
  },
  panel: {
    padding: "18px",
    borderRadius: "18px",
    background: "rgba(255,255,255,0.08)",
    marginBottom: "22px",
    color: "#e5e7eb"
  }
};
