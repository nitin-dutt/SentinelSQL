import { useEffect, useState } from "react";
import { connectDatabase, enterSample, fetchLaunchInfo, login } from "../api/sentinel";
import type { LaunchInfo, Role, Session } from "../api/sentinel";
import Icon from "./Icon";
import McpGuide, { CopyBlock } from "./McpGuide";
import Verdict from "./Verdict";

type Props = {
  onEnter: (session: Session) => void;
};

const REPO = "https://github.com/nitin-dutt/SentinelSQL";

/** What each suggested question on the sample database shows. */
const SAMPLE_QUESTIONS: { q: string; tone: "ok" | "warn" | "bad"; outcome: string }[] = [
  { q: "List every customer with their email and region", tone: "ok", outcome: "answered · emails masked" },
  { q: "Who are our VIP customers?", tone: "ok", outcome: "answered · glossary term" },
  { q: "What is the average salary?", tone: "warn", outcome: "held for review" },
  { q: "Delete all orders placed in 2025", tone: "bad", outcome: "blocked" },
];

const READ_ONLY_USER = {
  postgresql: `CREATE USER sentinel_ro WITH PASSWORD '...';
GRANT CONNECT ON DATABASE dbname TO sentinel_ro;
GRANT USAGE ON SCHEMA public TO sentinel_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO sentinel_ro;`,
  mysql: `CREATE USER 'sentinel_ro'@'%' IDENTIFIED BY '...';
GRANT SELECT ON dbname.* TO 'sentinel_ro'@'%';`,
};

type Engine = "postgresql" | "mysql";
const DEFAULT_PORT: Record<Engine, string> = { postgresql: "5432", mysql: "3306" };

function Preview() {
  return (
    <div className="preview" aria-label="Example: one question going through SentinelSQL">
      <div className="preview-bar">
        <span className="meta">alice · employee</span>
        <Verdict tone="ok">allowed · AST validated · PII masked</Verdict>
      </div>
      <div className="preview-body">
        <span className="label">Question</span>
        <p className="preview-q">List every customer with their email and region</p>
        <pre className="code">SELECT name, email, region FROM customers LIMIT 100</pre>
        <table className="table">
          <thead>
            <tr><th className="mono">name</th><th className="mono">email</th><th className="mono">region</th></tr>
          </thead>
          <tbody>
            <tr><td>Alice</td><td className="masked">a***e@example.com</td><td>North</td></tr>
            <tr><td>Bob</td><td className="masked">b*b@example.com</td><td>South</td></tr>
            <tr><td>Charlie</td><td className="masked">c*****e@example.com</td><td>West</td></tr>
          </tbody>
        </table>
      </div>
      <div className="ledger">
        <div className="ledger-row">
          <span>What is the average salary?</span>
          <Verdict tone="warn">held · waits for a reviewer</Verdict>
        </div>
        <div className="ledger-row">
          <span>Delete all orders placed in 2025</span>
          <Verdict tone="bad">blocked · not a read</Verdict>
        </div>
      </div>
    </div>
  );
}

function TokenForm({ onEnter, autoFocus = false }: Props & { autoFocus?: boolean }) {
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!token.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const session = await login(token);
      if (session) onEnter(session);
      else setError("That token was not recognised.");
    } catch {
      setError("Could not reach the SentinelSQL server.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="form" onSubmit={submit}>
      <div>
        <label className="label" htmlFor="access-token">Access token</label>
        <div className="field-row" style={{ gridTemplateColumns: "minmax(0, 1fr) auto" }}>
          <input id="access-token" className="input" type="password" autoComplete="off" autoFocus={autoFocus} value={token}
                 onChange={(e) => setToken(e.target.value)} disabled={busy} />
          <button type="submit" className="btn" disabled={busy || !token.trim()}>{busy ? "Checking…" : "Sign in"}</button>
        </div>
      </div>
      {error && <p className="text-bad" role="alert">{error}</p>}
    </form>
  );
}

function ConnectForm({ info, onEnter }: Props & { info: LaunchInfo }) {
  const [engine, setEngine] = useState<Engine>("postgresql");
  const [host, setHost] = useState("");
  const [port, setPort] = useState(DEFAULT_PORT.postgresql);
  const [database, setDatabase] = useState("");
  const [user, setUser] = useState("");
  const [password, setPassword] = useState("");
  const [ssl, setSsl] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function pick(next: Engine) {
    if (port === DEFAULT_PORT[engine]) setPort(DEFAULT_PORT[next]);
    setEngine(next);
  }

  const ready = host.trim() && database.trim() && user.trim();

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!ready) return;
    setBusy(true);
    setError(null);
    const enc = encodeURIComponent;
    const url = `${engine}://${enc(user.trim())}:${enc(password)}@${host.trim()}:${port.trim() || DEFAULT_PORT[engine]}/${enc(database.trim())}`
      + (engine === "postgresql" && ssl ? "?sslmode=require" : "");
    try {
      const session = await connectDatabase(url);
      if (session) onEnter(session);
      else setError("Connected, but the session could not be started. Please try again.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not connect to the database");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="form" onSubmit={submit}>
      <div className="seg" role="group" aria-label="Database engine" style={{ alignSelf: "flex-start" }}>
        <button type="button" aria-pressed={engine === "postgresql"} onClick={() => pick("postgresql")}>PostgreSQL</button>
        <button type="button" aria-pressed={engine === "mysql"} onClick={() => pick("mysql")}>MySQL</button>
      </div>
      <div className="field-row">
        <div>
          <label className="label" htmlFor="db-host">Host</label>
          <input id="db-host" className="input input-mono" placeholder={info.connect.private_hosts ? "localhost" : "db.example.com"}
                 value={host} onChange={(e) => setHost(e.target.value)} disabled={busy} autoComplete="off" />
        </div>
        <div>
          <label className="label" htmlFor="db-port">Port</label>
          <input id="db-port" className="input input-mono" inputMode="numeric" value={port} onChange={(e) => setPort(e.target.value)} disabled={busy} />
        </div>
      </div>
      <div>
        <label className="label" htmlFor="db-name">Database</label>
        <input id="db-name" className="input input-mono" value={database} onChange={(e) => setDatabase(e.target.value)} disabled={busy} autoComplete="off" />
      </div>
      <div className="field-row field-row-even">
        <div>
          <label className="label" htmlFor="db-user">User</label>
          <input id="db-user" className="input input-mono" value={user} onChange={(e) => setUser(e.target.value)} disabled={busy} autoComplete="off" />
        </div>
        <div>
          <label className="label" htmlFor="db-password">Password</label>
          <input id="db-password" className="input" type="password" value={password} onChange={(e) => setPassword(e.target.value)} disabled={busy} autoComplete="off" />
        </div>
      </div>
      {engine === "postgresql" && (
        <label className="check">
          <input type="checkbox" checked={ssl} onChange={(e) => setSsl(e.target.checked)} disabled={busy} />
          <span>Require SSL (hosted databases such as Neon or Supabase need this)</span>
        </label>
      )}
      {error && (
        <div className="notice notice-bad" role="alert">
          <span className="dot dot-bad" aria-hidden="true" />
          <span>{error}</span>
        </div>
      )}
      <button type="submit" className="btn btn-primary btn-lg" disabled={busy || !ready}>
        {busy ? "Connecting…" : "Connect and start asking"}
      </button>

      <ul className="notes">
        <li>Free workspace: {info.connect.question_quota} questions, up to {info.connect.max_tables} tables, closed after {info.connect.idle_minutes} idle minutes.</li>
        <li>The connection is opened read-only and every query still passes the policy gate. A user that can only SELECT is the stronger guarantee.</li>
        <li>Your password is kept in server memory for the life of the workspace, never written to disk or to a log.</li>
        <li>Your questions and your table and column names are sent to the language model that writes the SQL.</li>
        {!info.connect.private_hosts && <li>The host must be reachable from the internet. For a database on your own network, run SentinelSQL yourself.</li>}
      </ul>
      <details className="muted small">
        <summary style={{ cursor: "pointer" }}>Create a read-only user first</summary>
        <div style={{ marginTop: 8 }}>
          <CopyBlock label="read-only user SQL" text={READ_ONLY_USER[engine]} />
        </div>
      </details>
    </form>
  );
}

export default function Launch({ onEnter }: Props) {
  const [info, setInfo] = useState<LaunchInfo | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [entering, setEntering] = useState<Role | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchLaunchInfo().then(setInfo).finally(() => setLoaded(true));
  }, []);

  async function enter(role: Role) {
    setEntering(role);
    setError(null);
    try {
      const session = await enterSample(role);
      if (session) onEnter(session);
      else setError("The sample session could not be started.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "The sample session could not be started.");
    } finally {
      setEntering(null);
    }
  }

  const oneClick = !!info?.sample.one_click && info.sample.roles.includes("employee");
  const canReview = !!info?.sample.roles.includes("admin");
  const canConnect = !!info?.connect.enabled;
  const tables = info?.sample.tables;

  return (
    <div className="landing">
      <header className="nav">
        <div className="wrap">
          <span className="brand">Sentinel<span>SQL</span></span>
          <nav className="nav-links" aria-label="On this page">
            <a href="#how">How it works</a>
            <a href="#try">Try it</a>
            <a href="#mcp">Test with an agent</a>
            <a href="#self-host">Run it yourself</a>
          </nav>
          <a className="nav-quiet" href="#token">Sign in</a>
        </div>
      </header>

      <main>
        <section className="wrap hero enter">
          <div>
            <h1>Let people and AI agents query your database without leaking it</h1>
            <p className="hero-sub">
              SentinelSQL sits between a question and your data. It writes the SQL, checks it against a policy, holds sensitive queries
              for a human reviewer, masks personal data in the answer and records every decision in a tamper-evident log.
            </p>
            <div className="hero-actions">
              {oneClick ? (
                <button className="btn btn-primary btn-lg" onClick={() => enter("employee")} disabled={entering !== null}>
                  {entering === "employee" ? "Opening…" : "Try the sample database"}
                  <Icon name="arrow-right" />
                </button>
              ) : (
                <a className="btn btn-primary btn-lg" href="#token">Sign in with a token</a>
              )}
              {canConnect && <a className="btn btn-lg" href="#connect">Connect your database</a>}
            </div>
            {oneClick && <p className="hero-note muted small">No account and no access code. It opens straight into the app.</p>}
            {error && <p className="hero-note text-bad" role="alert">{error}</p>}
          </div>
          <Preview />
        </section>

        <section className="band" id="how">
          <div className="wrap">
            <div className="band-head">
              <h2 className="band-title">One path for every query</h2>
              <p className="band-sub">A person's question and an agent's SQL go through the same four steps, so a rule fixed once applies to both.</p>
            </div>
            <div className="steps">
              <div className="step">
                <span className="meta step-n">01</span>
                <h3>Ask</h3>
                <p>A question in plain English, or SQL that an AI agent submits over MCP. The model writes the query from your schema and glossary.</p>
              </div>
              <div className="step">
                <span className="meta step-n">02</span>
                <h3>Check</h3>
                <p>The SQL is parsed into a syntax tree and judged by policy: one read-only statement, no blocked functions, a row limit on everything.</p>
              </div>
              <div className="step">
                <span className="meta step-n">03</span>
                <h3>Hold or run</h3>
                <p>Queries on sensitive tables wait for a reviewer, who cannot be the person who asked. The rest run read-only with a timeout.</p>
              </div>
              <div className="step">
                <span className="meta step-n">04</span>
                <h3>Mask and record</h3>
                <p>Emails, SSNs, card numbers and salaries are masked in the result, and every decision lands in a hash-chained audit log.</p>
              </div>
            </div>
          </div>
        </section>

        <section className="band" id="try">
          <div className="wrap">
            <div className="band-head">
              <h2 className="band-title">Two ways to try it</h2>
              <p className="band-sub">Start on the sample company to see every behaviour in five minutes, or point it at a database of your own.</p>
            </div>
            <div className="try-grid">
              <div className="card try-card">
                <div>
                  <span className="meta try-kicker">Option 1</span>
                  <h3>The sample database</h3>
                  <p className="muted" style={{ marginTop: 8 }}>
                    A synthetic company{tables ? ` in ${tables} tables` : ""}: customers, orders, products, staff and finance. Staff and salary
                    tables are marked sensitive, so you can watch a query being held and then approve it yourself as the reviewer.
                  </p>
                </div>
                {loaded && oneClick && (
                  <div className="actions">
                    <button className="btn btn-primary btn-lg" onClick={() => enter("employee")} disabled={entering !== null}>
                      {entering === "employee" ? "Opening…" : "Enter as employee"}
                    </button>
                    {canReview && (
                      <button className="btn btn-lg" onClick={() => enter("admin")} disabled={entering !== null}>
                        {entering === "admin" ? "Opening…" : "Enter as reviewer"}
                      </button>
                    )}
                  </div>
                )}
                {loaded && !oneClick && (
                  <p className="muted">This server needs an access token from its administrator. Sign in below, or run your own copy to try the sample.</p>
                )}
                <div>
                  <span className="label">Questions to ask, and what each one shows</span>
                  <ul className="plain-list">
                    {SAMPLE_QUESTIONS.map((item) => (
                      <li key={item.q}>
                        <span>{item.q}</span>
                        <Verdict tone={item.tone}>{item.outcome}</Verdict>
                      </li>
                    ))}
                  </ul>
                </div>
                <ul className="notes">
                  <li>The employee asks; the reviewer sees the queue, the audit log and timings. Switch roles any time from the top bar.</li>
                  {info?.limits && (
                    <li>
                      Shared demo limits: {info.limits.rate_per_minute} questions a minute each, {info.limits.remaining_today} of {info.limits.daily_cap} left
                      today for everyone. Repeated questions are answered from the cache and do not count.
                    </li>
                  )}
                </ul>
                <div id="token" style={{ scrollMarginTop: 80 }}>
                  {oneClick ? (
                    <details className="muted small">
                      <summary style={{ cursor: "pointer" }}>Have an access token instead?</summary>
                      <div style={{ marginTop: 12 }}><TokenForm onEnter={onEnter} /></div>
                    </details>
                  ) : (
                    loaded && <TokenForm onEnter={onEnter} />
                  )}
                </div>
              </div>

              <div className="card try-card" id="connect" style={{ scrollMarginTop: 80 }}>
                <div>
                  <span className="meta try-kicker">Option 2</span>
                  <h3>Your own database</h3>
                  <p className="muted" style={{ marginTop: 8 }}>
                    Connect a PostgreSQL or MySQL database and ask it questions in a private workspace, with its own review queue and audit log.
                  </p>
                </div>
                {info && canConnect ? (
                  <ConnectForm info={info} onEnter={onEnter} />
                ) : (
                  loaded && (
                    <p className="muted">
                      Connecting from this page is switched off on this server. Run SentinelSQL yourself and set <code>DATABASE_URL</code> to
                      your database: the steps are under <a href="#self-host">Run it yourself</a>.
                    </p>
                  )
                )}
              </div>
            </div>
          </div>
        </section>

        <section className="band" id="mcp">
          <div className="wrap">
            <div className="band-head">
              <h2 className="band-title">Test it as an AI agent</h2>
              <p className="band-sub">
                Agents reach the database through the Model Context Protocol. SentinelSQL is the server they talk to, so an agent that has
                been tricked still cannot write, read a sensitive table unreviewed, or see unmasked personal data.
              </p>
            </div>
            <McpGuide />
          </div>
        </section>

        <section className="band" id="self-host">
          <div className="wrap">
            <div className="band-head">
              <h2 className="band-title">Run it yourself</h2>
              <p className="band-sub">One container serves the API and this interface on port 8000. You need Docker and a Gemini API key.</p>
            </div>
            <div className="split">
              <div className="guide">
                <div>
                  <span className="label">1. Get the code and a settings file</span>
                  <CopyBlock label="clone commands" text={`git clone ${REPO}.git\ncd SentinelSQL\ncp .env.example .env`} />
                </div>
                <div>
                  <span className="label">2. Edit .env</span>
                  <CopyBlock label="settings" text={`GEMINI_API_KEY="your key"\nAPP_ENV="dev"        # enter without a token, on your own machine only\n# DATABASE_URL="postgresql://user:password@host.docker.internal:5432/dbname"`} />
                </div>
                <div>
                  <span className="label">3. Start it, then open http://localhost:8000</span>
                  <CopyBlock label="start command" text="docker compose up --build" />
                </div>
              </div>
              <div className="guide">
                <h3>What the settings mean</h3>
                <ul className="notes" style={{ fontSize: 14 }}>
                  <li>Leave <code>DATABASE_URL</code> alone to use the sample database, or set it to your PostgreSQL or MySQL URL. The SQL dialect is detected from it.</li>
                  <li><code>APP_ENV="dev"</code> removes the token step. On anything other people can reach, leave it out and set <code>AUTH_TOKENS</code> instead.</li>
                  <li>For a real database add <code>READONLY_DATABASE_URL</code> with a user that can only SELECT.</li>
                  <li>Measured results, their method and their limits are in the <a href={`${REPO}#benchmarks`}>README</a>.</li>
                </ul>
              </div>
            </div>
          </div>
        </section>
      </main>

      <footer className="footer">
        <div className="wrap">
          <span>SentinelSQL</span>
          <a href={REPO}>Source on GitHub</a>
        </div>
      </footer>
    </div>
  );
}
