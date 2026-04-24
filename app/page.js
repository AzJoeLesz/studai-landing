"use client";

import { useState } from "react";
import { createClient } from "@supabase/supabase-js";

const supabase = createClient(
  process.env.NEXT_PUBLIC_SUPABASE_URL,
  process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY
);

export default function Home() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [message, setMessage] = useState("");

  async function signUp() {
    setMessage("Creating account...");

    const { error } = await supabase.auth.signUp({
      email,
      password
    });

    setMessage(error ? error.message : "Account created. You can now log in.");
  }

  async function signIn() {
    setMessage("Signing in...");

    const { error } = await supabase.auth.signInWithPassword({
      email,
      password
    });

    setMessage(error ? error.message : "Logged in successfully.");
  }

  return (
    <main style={styles.page}>
      <div style={styles.overlay}>
        <section style={styles.card}>
          <h1 style={styles.logo}>
            Stud<span style={{ color: "#8b5cf6" }}>AI</span>
          </h1>

          <h2>Login test</h2>

          <input
            style={styles.input}
            placeholder="Email"
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

          <button style={styles.button} onClick={signUp}>
            Create account
          </button>

          <button style={styles.secondaryButton} onClick={signIn}>
            Log in
          </button>

          <p style={styles.message}>{message}</p>
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
    background: "rgba(5, 10, 20, 0.72)",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    padding: "32px"
  },
  card: {
    width: "100%",
    maxWidth: "420px",
    padding: "32px",
    borderRadius: "20px",
    background: "rgba(0, 0, 0, 0.42)",
    backdropFilter: "blur(10px)",
    boxShadow: "0 20px 60px rgba(0, 0, 0, 0.5)"
  },
  logo: {
    fontSize: "36px",
    marginBottom: "20px"
  },
  input: {
    width: "100%",
    padding: "14px",
    marginBottom: "12px",
    borderRadius: "10px",
    border: "1px solid #333",
    fontSize: "16px"
  },
  button: {
    width: "100%",
    padding: "14px",
    marginTop: "8px",
    borderRadius: "10px",
    border: "none",
    background: "#8b5cf6",
    color: "white",
    fontSize: "16px",
    cursor: "pointer"
  },
  secondaryButton: {
    width: "100%",
    padding: "14px",
    marginTop: "10px",
    borderRadius: "10px",
    border: "1px solid #8b5cf6",
    background: "transparent",
    color: "white",
    fontSize: "16px",
    cursor: "pointer"
  },
  message: {
    marginTop: "16px",
    color: "#cbd5e1"
  }
};
