"""
Preflight check for a running SentinelSQL: runs the whole demo flow that does NOT need the language model and reports PASS / FAIL / WARN.
Run it before recording a demo or showing the project, so nothing surprises you on camera.

    python scripts/demo_preflight.py                       # against http://localhost:8000
    python scripts/demo_preflight.py http://localhost:8010

Tokens are read from AUTH_TOKENS (format token:user:role,...) in the environment or in the project's .env: it needs one employee,
two different admins and one external_ai token. The check CHANGES the demo database a little (it adds audit rows and review
requests), so run it on the demo database, not on one you care about.
"""

import json
import os
import sys
import urllib.error
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000").rstrip("/")


def load_tokens():
    raw = os.getenv("AUTH_TOKENS", "")
    if not raw:
        try:
            from dotenv import dotenv_values
            raw = dotenv_values(os.path.join(ROOT, ".env")).get("AUTH_TOKENS") or ""
        except Exception:
            pass
    by_role = {}
    for item in raw.split(","):
        parts = item.strip().split(":")
        if len(parts) == 3:
            by_role.setdefault(parts[2].strip(), []).append((parts[0].strip(), parts[1].strip()))
    return by_role


def call(method, path, token=None, body=None, timeout=30):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "null")
        except Exception:
            return e.code, None
    except Exception as e:  # connection refused, timeout
        return 0, {"error": f"{type(e).__name__}: {e}"}


results = []


def check(name, ok, detail="", warn=False):
    status = "PASS" if ok else ("WARN" if warn else "FAIL")
    results.append(status)
    print(f"  [{status}] {name}" + (f"  ({detail})" if detail else ""))
    return ok


def main() -> int:
    print(f"SentinelSQL preflight against {BASE}\n")
    code, body = call("GET", "/health")
    if not check("API is up", code == 200 and (body or {}).get("status") == "ok", f"HTTP {code}"):
        print("\nThe API is not reachable; start it first (docker compose up, or uvicorn app.main:app).")
        return 1

    tokens = load_tokens()
    need = {"employee": 1, "admin": 2, "external_ai": 1}
    missing = [f"{n} {r}" for r, n in need.items() if len(tokens.get(r, [])) < n]
    if not check("AUTH_TOKENS has an employee, two admins and an external_ai token", not missing, "missing: " + ", ".join(missing) if missing else ""):
        return 1
    emp, (adm1, adm2), agent = tokens["employee"][0][0], [t[0] for t in tokens["admin"][:2]], tokens["external_ai"][0][0]

    print("\nIdentity and roles")
    code, me = call("GET", "/auth/me", emp)
    check("employee token is recognised", code == 200 and me.get("role") == "employee", f"HTTP {code}")
    code, _ = call("GET", "/auth/me")
    check("a request without a token is refused", code == 401, f"HTTP {code}")
    code, _ = call("GET", "/admin/queue", emp)
    check("an employee cannot open the admin review queue", code == 403, f"HTTP {code}")

    print("\nGovernance through the MCP bridge (no language model involved)")
    code, r = call("POST", "/mcp/submit", agent, {"sql": "SELECT name, email FROM customers LIMIT 3", "rationale": "preflight"})
    emails = [row.get("email", "") for row in ((r or {}).get("results") or {}).get("rows", [])]
    check("a harmless read runs and emails come back masked", code == 200 and r.get("status") == "completed" and emails and all("*" in e for e in emails),
          f"status {r and r.get('status')}")
    code, r = call("POST", "/mcp/submit", agent, {"sql": "SELECT * FROM customers", "rationale": "preflight"})
    check("SELECT * is rejected with an explanation", r and r.get("status") == "rejected" and "explicitly" in (r.get("reason") or ""), (r or {}).get("reason", "")[:60])
    code, r = call("POST", "/mcp/submit", agent, {"sql": "DROP TABLE customers", "rationale": "preflight"})
    check("DROP TABLE is rejected", r and r.get("status") == "rejected", (r or {}).get("reason", "")[:60])
    code, r = call("POST", "/mcp/submit", agent, {"sql": "SELECT salary FROM salaries", "rationale": "preflight"})
    held = r and r.get("status") == "requires_human"
    check("a salary query is held for human review", held, f"status {r and r.get('status')}")

    print("\nHuman approval")
    if held:
        code, d = call("POST", "/hitl/decision", adm2, {"run_id": r["run_id"], "decision": "approve"})
        rows = ((d or {}).get("results") or {}).get("rows", [])
        shown = [row.get("salary") for row in rows]
        check("the approving admin sees the real salary figures", d and d.get("status") == "completed" and rows and all(isinstance(v, (int, float)) for v in shown),
              f"status {d and d.get('status')}, values {shown[:2]}")
        code, st = call("GET", f"/mcp/status/{r['run_id']}", agent)
        seen = [row.get("salary") for row in (((st or {}).get("results") or {}).get("rows") or [])]
        check("the agent that asked still gets the salary redacted", code == 200 and seen and all(v == "[CONFIDENTIAL]" for v in seen), f"HTTP {code}, values {seen[:2]}")
    code, r2 = call("POST", "/mcp/submit", adm1, {"sql": "SELECT salary FROM salaries", "rationale": "preflight: submitted by an admin"})
    if r2 and r2.get("status") == "requires_human":
        code, _ = call("POST", "/hitl/decision", adm1, {"run_id": r2["run_id"], "decision": "approve"})
        check("an admin cannot approve their own request", code == 403, f"HTTP {code}")
        code, d = call("POST", "/hitl/decision", adm2, {"run_id": r2["run_id"], "decision": "reject"})
        check("a different admin can decide it", d and d.get("status") == "rejected", f"status {d and d.get('status')}")
    else:
        check("an admin's own submission is held for review", False, f"status {r2 and r2.get('status')}")

    print("\nAudit trail")
    code, v = call("GET", "/admin/audit-logs/verify", adm1)
    check("the audit hash chain verifies", code == 200 and v.get("valid") is True, f"{(v or {}).get('checked')} rows checked")
    code, rows = call("GET", "/admin/audit-logs?limit=5", adm1)
    check("recent events are recorded with their channel", code == 200 and rows and any(x.get("ingress_channel") == "MCP_AGENT" for x in rows))

    print("\nLanguage model (needed for typing questions in plain English)")
    code, r = call("POST", "/query", emp, {"query": "How many customers are there?"}, timeout=90)
    ok = code == 200 and r.get("status") in ("completed", "requires_human")
    check("an English question is answered", ok, f"status {r and r.get('status')}: {str((r or {}).get('reason', ''))[:70]}", warn=True)
    if not ok:
        print("         Gemini may be unavailable or GEMINI_API_KEY missing. Everything above works without it; record the English-question part when this passes.")

    fails, warns = results.count("FAIL"), results.count("WARN")
    print(f"\n{results.count('PASS')} passed, {warns} warnings, {fails} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
