"""
Runs SentinelSQL in public-demo mode on http://localhost:8020 to try the guardrails before hosting.

Uses throwaway tokens and keeps EVERYTHING it writes (demo database, audit log, review queue, semantic cache, golden vectors) in a
`sentinel-demo-local` folder in the system temp directory, so your .env tokens and your real local data are untouched and nothing
you test here appears in your normal app. Start from an empty audit log and cache with:  python scripts/run_demo_local.py --fresh

 GEMINI_API_KEY is
still read from .env (the model calls are real), and the built UI (sentinel-frontend/dist) is served from the same port.
"""
import os
import shutil
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

work = os.path.join(tempfile.gettempdir(), "sentinel-demo-local")  # fixed, so pending reviews survive a restart
if "--fresh" in sys.argv:
    shutil.rmtree(work, ignore_errors=True)
os.makedirs(work, exist_ok=True)
os.chdir(work)  # the app writes its caches and indexes to relative paths: keep them out of the project folder
os.environ.update({
    "DEMO_MODE": "true",
    "DEMO_PUBLIC_USERS": "demo-employee,demo-reviewer",
    "DEMO_RATE_PER_MINUTE": os.getenv("DEMO_RATE_PER_MINUTE", "6"),
    "DEMO_DAILY_QUERY_CAP": os.getenv("DEMO_DAILY_QUERY_CAP", "150"),
    "SEMANTIC_CACHE_TTL_SECONDS": os.getenv("SEMANTIC_CACHE_TTL_SECONDS", "86400"),  # answers asked before recording stay cached all day
    "AUTH_TOKENS": "local-demo-employee:demo-employee:employee,local-demo-reviewer:demo-reviewer:admin,local-owner-secret:owner:admin",
    "DATABASE_URL": "sqlite:///" + os.path.join(work, "demo.db").replace(os.sep, "/"),
    "ACTIVE_DB": "",
    "AUDIT_DB_PATH": os.path.join(work, "audit.db"),
    "HITL_DB_PATH": os.path.join(work, "hitl_runs.db"),
    "UI_DIST_DIR": os.path.join(ROOT, "sentinel-frontend", "dist"),
})

import uvicorn  # noqa: E402

print(f"Demo mode on http://localhost:8020  (temporary data in {work})")
uvicorn.run("app.main:app", port=8020)
