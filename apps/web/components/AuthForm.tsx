"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";
import { ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";

const MIN_PASSWORD_LENGTH = 10;
const MAX_PASSWORD_LENGTH = 128;
const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

type Props = { mode: "login" | "signup" };
type Errors = { email?: string; password?: string; form?: string };

export function AuthForm({ mode }: Props) {
  const { state, login, signup } = useAuth();
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [inviteCode, setInviteCode] = useState("");
  const [errors, setErrors] = useState<Errors>({});
  const [submitting, setSubmitting] = useState(false);
  const isSignup = mode === "signup";

  useEffect(() => {
    if (state.status === "authenticated") router.replace("/home");
  }, [state.status, router]);

  function validate(): Errors {
    const found: Errors = {};
    if (!EMAIL_PATTERN.test(email.trim())) found.email = "Enter a valid email address.";
    if (isSignup) {
      if (password.length < MIN_PASSWORD_LENGTH)
        found.password = `Password must be at least ${MIN_PASSWORD_LENGTH} characters.`;
      else if (password.length > MAX_PASSWORD_LENGTH)
        found.password = `Password must be at most ${MAX_PASSWORD_LENGTH} characters.`;
    } else if (password.length === 0) {
      found.password = "Enter your password.";
    }
    return found;
  }

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const found = validate();
    setErrors(found);
    if (Object.keys(found).length > 0) return;

    setSubmitting(true);
    try {
      await (isSignup ? signup(email.trim(), password, inviteCode) : login(email.trim(), password));
      router.replace("/home");
    } catch (err) {
      const next: Errors = {};
      if (err instanceof ApiError) {
        for (const d of err.details) {
          if (d.field === "email" || d.field === "password") next[d.field] = d.message;
        }
        if (!next.email && !next.password) next.form = err.message;
      } else {
        next.form = "Something went wrong. Please try again.";
      }
      setErrors(next);
      setSubmitting(false);
    }
  }

  return (
    <main style={styles.main}>
      <form onSubmit={onSubmit} noValidate style={styles.card} aria-labelledby="auth-title">
        <h1 id="auth-title" style={{ margin: 0 }}>
          {isSignup ? "Create account" : "Log in"}
        </h1>
        <p style={styles.muted}>Interview Assistant</p>

        {errors.form && (
          <p role="alert" style={styles.error}>
            {errors.form}
          </p>
        )}

        <label style={styles.label}>
          Email
          <input
            type="email"
            name="email"
            autoComplete="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            aria-invalid={errors.email ? true : undefined}
            style={styles.input}
            required
          />
        </label>
        {errors.email && <p style={styles.fieldError}>{errors.email}</p>}

        <label style={styles.label}>
          Password
          <input
            type="password"
            name="password"
            autoComplete={isSignup ? "new-password" : "current-password"}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            aria-invalid={errors.password ? true : undefined}
            style={styles.input}
            required
          />
        </label>
        {isSignup && !errors.password && (
          <p style={styles.muted}>At least {MIN_PASSWORD_LENGTH} characters.</p>
        )}
        {errors.password && <p style={styles.fieldError}>{errors.password}</p>}

        {isSignup && (
          <label style={styles.label}>
            Invitation code (if you were given one)
            <input
              type="password"
              name="inviteCode"
              autoComplete="off"
              value={inviteCode}
              onChange={(e) => setInviteCode(e.target.value)}
              maxLength={128}
              style={styles.input}
            />
          </label>
        )}

        <button type="submit" disabled={submitting} style={styles.button}>
          {submitting ? "Please wait…" : isSignup ? "Sign up" : "Log in"}
        </button>

        <p style={styles.muted}>
          {isSignup ? (
            <>
              Already have an account? <Link href="/login">Log in</Link>
            </>
          ) : (
            <>
              New here? <Link href="/signup">Create an account</Link>
            </>
          )}
        </p>
      </form>
    </main>
  );
}

const styles = {
  main: { display: "grid", placeItems: "center", minHeight: "100vh", padding: "1rem" },
  card: {
    display: "flex",
    flexDirection: "column",
    gap: "0.5rem",
    width: "100%",
    maxWidth: "22rem",
  },
  label: { display: "flex", flexDirection: "column", gap: "0.25rem", fontWeight: 600 },
  input: { padding: "0.5rem", fontSize: "1rem", fontWeight: 400 },
  button: { padding: "0.6rem", fontSize: "1rem", marginTop: "0.5rem", cursor: "pointer" },
  muted: { color: "#666", fontSize: "0.875rem", margin: 0 },
  error: { color: "#b00020", margin: 0 },
  fieldError: { color: "#b00020", fontSize: "0.875rem", margin: 0 },
} as const satisfies Record<string, React.CSSProperties>;
