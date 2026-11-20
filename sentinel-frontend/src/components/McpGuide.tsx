import { useState } from "react";
import Icon from "./Icon";
import Verdict from "./Verdict";

/** A command or config snippet with a copy button. */
export function CopyBlock({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {
      /* clipboard unavailable: the text can still be selected by hand */
    }
  }

  return (
    <div className="copyblock">
      <pre className="code">{text}</pre>
      <button type="button" className="btn btn-sm" onClick={copy} aria-label={`Copy ${label}`}>
        <Icon name={copied ? "check" : "copy"} size={14} />
        {copied ? "Copied" : "Copy"}
      </button>
    </div>
  );
}

const TRIES: { sql: string; tone: "ok" | "warn" | "bad"; outcome: string }[] = [
  { sql: "SELECT name, email FROM customers LIMIT 5", tone: "ok", outcome: "runs · emails masked" },
  { sql: "SELECT AVG(salary) FROM salaries", tone: "warn", outcome: "held for a reviewer" },
  { sql: "DROP TABLE orders", tone: "bad", outcome: "blocked" },
];

const STDIO_CONFIG = `{
  "mcpServers": {
    "sentinelsql": {
      "command": "python",
      "args": ["-m", "app.mcp_server"],
      "cwd": "/path/to/SentinelSQL"
    }
  }
}`;

/**
 * How to drive SentinelSQL as an AI agent. With `token` (the signed-in tester's own agent token) the commands are ready
 * to paste; without it they show a $TOKEN placeholder. `sampleTables` is false on a visitor's own database, where the
 * example statements name tables that do not exist there.
 */
export default function McpGuide({ token, sampleTables = true }: { token?: string; sampleTables?: boolean }) {
  const origin = window.location.origin;
  const bearer = token ?? "$TOKEN";
  const submit = (sql: string) =>
    `curl -s -X POST ${origin}/mcp/submit \\\n  -H "Authorization: Bearer ${bearer}" \\\n  -H "Content-Type: application/json" \\\n  -d '{"sql": "${sql}", "rationale": "testing the gateway"}'`;

  return (
    <div className="split">
      <div className="guide">
        <h3>Over HTTP, from any agent or a terminal</h3>
        <p className="muted">
          An agent does not ask in English: it submits SQL it wrote itself. The gateway judges that SQL with the same policy, masking,
          review queue and audit log as a person's question.
          {!token && <> Enter the app first: the Agent access tab shows these commands with your own token filled in.</>}
        </p>
        <div>
          <span className="label">1. Read the schema</span>
          <CopyBlock label="schema command" text={`curl -s ${origin}/mcp/schema \\\n  -H "Authorization: Bearer ${bearer}"`} />
        </div>
        <div>
          <span className="label">2. Submit a query</span>
          <CopyBlock label="submit command" text={submit(sampleTables ? TRIES[0].sql : "SELECT 1")} />
        </div>
        <div>
          <span className="label">3. If it was held, poll for the reviewer's decision</span>
          <CopyBlock label="status command" text={`curl -s ${origin}/mcp/status/RUN_ID \\\n  -H "Authorization: Bearer ${bearer}"`} />
        </div>
        {sampleTables && (
          <div>
            <span className="label">Statements worth submitting on the sample database</span>
            <ul className="plain-list">
              {TRIES.map((t) => (
                <li key={t.sql}>
                  <code>{t.sql}</code>
                  <Verdict tone={t.tone}>{t.outcome}</Verdict>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>

      <div className="guide">
        <h3>Over stdio, from Claude Desktop or Cursor</h3>
        <p className="muted">
          The MCP server runs on your own machine next to the client, so this needs a local copy of SentinelSQL (see Run it yourself).
          It exposes three tools: <code>get_database_schema</code>, <code>submit_sql_for_execution</code> and <code>check_query_status</code>.
        </p>
        <div>
          <span className="label">Add to the client's MCP configuration</span>
          <CopyBlock label="MCP configuration" text={STDIO_CONFIG} />
        </div>
        <div>
          <span className="label">Or inspect the tools by hand</span>
          <CopyBlock label="inspector command" text="npx @modelcontextprotocol/inspector python -m app.mcp_server" />
        </div>
        <p className="muted small">
          The stdio transport trusts the local process and has no token. Set <code>MCP_CLIENT_ID</code> to name the client in the audit log.
        </p>
      </div>
    </div>
  );
}
