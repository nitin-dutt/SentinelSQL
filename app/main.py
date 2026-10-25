"""
SentinelSQL API entry point.

This module only wires things together: startup/shutdown (lifespan) and the routers. The
endpoints live in app/routers/ (alignment_plan RUN-06).
"""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import demo
from app.db.database import check_connection, init_db
from app.routers import admin, hitl, mcp, query, system
from app.utils.logger import get_logger

logger = get_logger("sentinel.main")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    check_connection()  # fail fast, with a clear message, if the database is unreachable
    init_db()           # seeds the bundled demo database only (see database.seed_demo_data_enabled)
    logger.info("SentinelSQL API ready")
    yield


app = FastAPI(title="SentinelSQL API", lifespan=lifespan)

for _router in (system.router, query.router, hitl.router, admin.router, mcp.router):
    app.include_router(_router)
demo.install(app)  # public-demo limits; inert unless DEMO_MODE=true


def mount_ui(target: FastAPI, directory: str) -> bool:
    """
    Serves the built React UI from the same origin as the API (the container image does this, so one port and no CORS or proxy).
    Mounted last, so every API route keeps priority. Returns False, and mounts nothing, when there is no build to serve.
    """
    if not os.path.isfile(os.path.join(directory, "index.html")):
        return False
    target.mount("/", StaticFiles(directory=directory, html=True), name="ui")
    return True


# UI_DIST_DIR points at the built frontend (set in the Docker image); in development Vite serves the UI instead, so nothing is mounted.
if os.getenv("UI_DIST_DIR"):
    mount_ui(app, os.environ["UI_DIST_DIR"])


# -----------------------
# CLI Entry Point
# -----------------------

def main():
    from app.graph.workflow import build_graph

    init_db()
    user_query = input("Ask a question: ")
    final_state = build_graph().invoke({
        "user_query": user_query,
        "intent": None,
        "sql": None,
        "validation": None,
        "results": None,
    })
    print("\nFinal Results:")
    print(final_state.get("results") or (final_state.get("validation") or {}).get("reason"))


if __name__ == "__main__":
    main()
