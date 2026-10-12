import { useState } from "react";
import { login } from "../api/sentinel";
import type { Session } from "../api/sentinel";

type Props = {
  onLogin: (session: Session) => void;
};

export default function Login({ onLogin }: Props) {
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!token.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const session = await login(token);
      if (session) {
        onLogin(session);
      } else {
        setError("That token was not recognised.");
      }
    } catch {
      setError("Could not reach the SentinelSQL server.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100vh" }}>
      <form
        onSubmit={handleSubmit}
        className="glass-panel animate-fade-in"
        style={{ padding: "40px", width: "100%", maxWidth: "420px", display: "flex", flexDirection: "column", gap: "16px" }}
      >
        <h1 style={{ fontSize: "28px", fontWeight: 700, margin: 0, letterSpacing: "-0.5px" }}>
          Sentinel<span style={{ color: "var(--accent)" }}>SQL</span>
        </h1>
        <p style={{ color: "var(--text-muted)", margin: 0, fontSize: "14px" }}>
          Enter your access token. Tokens are issued by your administrator (see <code>AUTH_TOKENS</code> in the server's <code>.env</code>).
        </p>
        <input
          type="password"
          autoFocus
          autoComplete="off"
          placeholder="Access token"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          disabled={busy}
        />
        {error && <div style={{ color: "#f87171", fontSize: "14px" }}>{error}</div>}
        <button type="submit" className="primary" disabled={busy || !token.trim()}>
          {busy ? "Checking..." : "Sign in"}
        </button>
      </form>
    </div>
  );
}
