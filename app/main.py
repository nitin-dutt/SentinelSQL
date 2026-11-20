"""
SentinelSQL API entry point.

This module only wires things together: startup/shutdown (lifespan) and the routers. The
endpoints live in app/routers/ (alignment_plan RUN-06).
"""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import demo, workspaces
from app.db.database import check_connection, init_db
from app.routers import admin, chat, hitl, mcp, query, system
from app.utils.logger import get_logger
from app.settings import settings

logger = get_logger("sentinel.main")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    check_connection()  # fail fast, with a clear message, if the database is unreachable
    init_db()           # seeds the bundled demo database only (see database.seed_demo_data_enabled)
    workspaces.startup()  # clears folders left by an earlier process; warns when APP_ENV=dev
    logger.info("SentinelSQL API ready")
    yield


app = FastAPI(title="SentinelSQL API", lifespan=lifespan)

for _router in (system.router, query.router, chat.router, hitl.router, admin.router, mcp.router):
    app.include_router(_router)
demo.install(app)  # public-demo limits; inert unless DEMO_MODE=true
workspaces.install(app)  # launch page: sample sessions and visitors' own databases (see app/workspaces.py)


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
if settings.stores.ui_dist_dir:
    mount_ui(app, settings.stores.ui_dist_dir)

