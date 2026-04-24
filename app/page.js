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

  const [user, setUser] = useState(null);
  const [visibleUser, setVisibleUser] = useState(null);
  const [isTransitioning, setIsTransitioning] = useState(false);

  function showMessage(text, type = "info") {
    setMessage(text);
    setMessageType(type);

    setTimeout(() => {
      setMessage("");
    }, 3500);
  }

  useEffect(() => {
    supabase.auth.getUser().then(({ data }) => {
      setUser(data.user);
      setVisibleUser(data.user);
    });

    const { data: listener } = supabase.auth.onAuthStateChange(
      (_event, session) => {
        setIsTransitioning(true);

        setTimeout(() => {
          setUser(session?.user ?? null);
          setVisibleUser(session?.user ?? null);
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

    if (error) {
      showMessage(error.message, "error");
    } else {
      showMessage("Account created. Check your email to confirm it.", "success");
    }
  }

  async function signIn() {
    showMessage("Signing you in...", "info");

    const { error } = await supabase.auth.signInWithPassword({
      email,
      password
    });

    if (error) {
      showMessage("Invalid email or password.", "error");
    } else {
      showMessage("Welcome back.", "success");
    }
  }

  async function signOut() {
    await supabase.auth.signOut();
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

            <h2 style={styles.title}>Welcome to your learning space</h2>

            <p style={styles.text}>
              Your account is working. Next we will connect this page to your
              student profile, saved sessions, and AI tutor.
            </p>

            <div style={styles.panel}>
              <strong>Email:</strong>
              <br />
              {visibleUser.email}
            </div>

            <button style={styles.button} onClick={signOut}>
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

          <button style={styles.button} onClick={signIn}>
            Log in
          </button>

          <button style={styles.secondaryButton} onClick={signUp}>
            Create account
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
    maxWidth: "680px",
    padding: "42px",
    borderRadius: "30px",
    background: "rgba(8, 12, 24, 0.76)",
    backdropFilter: "blur(14px)",
    boxShadow: "0 24px 80px rgba(0,0,0,0.55)",
    border: "1px solid rgba(255,255,255,0.12)",
    textAlign: "center",
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
  message: {
    marginTop: "18px",
    padding: "12px 14px",
    borderRadius: "14px",
    lineHeight: 1.4,
    fontSize: "14px",
    animation: "fadeIn 200ms ease"
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
