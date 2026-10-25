import { useEffect, useState } from "react";
import { login } from "../api/sentinel";
import type { Session } from "../api/sentinel";

type Props = {
  onLogin: (session: Session) => void;
};

type DemoInfo = {
  demo: boolean;
  notice?: string;
  logins?: { user_id: string; role: string; token: string }[];
  questions_remaining_today?: number;
  daily_cap?: number;
  rate_per_minute?: number;
};

const ROLE_LABELS: Record<string, string> = { employee: "Employee (asks questions)", admin: "Reviewer (approves held queries)" };

export default function Login({ onLogin }: Props) {
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [demo, setDemo] = useState<DemoInfo | null>(null);

  // On a public demo the server lists shared logins; on any other deployment this returns {demo:false} and nothing is shown.
  useEffect(() => {
    fetch("/demo/info")
      .then((res) => (res.ok ? res.json() : null))
      .then((info) => setDemo(info?.demo ? info : null))
      .catch(() => setDemo(null));
  }, []);

  async function signIn(value: string) {
    setBusy(true);
    setError(null);
    try {
      const session = await login(value);
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

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (token.trim()) void signIn(token);
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
          {demo
            ? "Pick a demo account below, or paste your own access token."
            : <>Enter your access token. Tokens are issued by your administrator (see <code>AUTH_TOKENS</code> in the server's <code>.env</code>).</>}
        </p>
        <input
          type="password"
          className="token-input"
          autoFocus
          autoComplete="off"
          placeholder="Paste your access token"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          disabled={busy}
        />
        {demo && (
          <div style={{ display: "flex", flexDirection: "column", gap: "8px", fontSize: "13px" }}>
            <div style={{ color: "var(--text-muted)" }}>
              {demo.notice} Questions left today: {demo.questions_remaining_today} of {demo.daily_cap}; {demo.rate_per_minute} per minute each.
            </div>
            {(demo.logins ?? []).map((item) => (
              <button type="button" key={item.user_id} disabled={busy} onClick={() => signIn(item.token)}>
                Try the demo as: {ROLE_LABELS[item.role] ?? item.role}
              </button>
            ))}
          </div>
        )}
        {error && <div style={{ color: "#f87171", fontSize: "14px" }}>{error}</div>}
        <button type="submit" className="primary" disabled={busy || !token.trim()}>
          {busy ? "Checking..." : "Sign in"}
        </button>
      </form>
    </div>
  );
}
